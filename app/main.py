"""
main.py
FastAPI app + FastMCP server.

Transports:
    stdio  — local use (Claude Desktop, Claude Code); auth disabled by design
    SSE    — network clients (BizMind AI, EduIntel LK); auth ALWAYS required

Startup order:
    1. Init asyncpg pool (postgis.init_pool)
    2. Init Redis client (redis_cache.init_redis)
    3. Init Qdrant client + configure Gemini (qdrant_client.init_qdrant)
    4. Mount MCP SSE handler on /sse
    5. Serve

Auth rules:
    - SSE endpoint ALWAYS requires X-API-Key header — no env flag disables this
    - stdio transport has no auth (local process, not network)
    - REQUIRE_AUTH=false is only for local stdio testing; never in production .env
"""

from __future__ import annotations

import secrets
import sys
from contextlib import asynccontextmanager

import anyio
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport

import redis.asyncio as aioredis

from app.cache import redis_cache
from app.config import settings
from app.db import postgis
from app.embeddings import qdrant_client as qdrant_mod
from app.tools import register_tools

log = structlog.get_logger()

# ── FastMCP instance ─────────────────────────────────────────────────────────
mcp = FastMCP(
    name="MCP Sri Lanka Geo",
    version=settings.app_version,
)

register_tools(mcp)


# ── Lifespan (replaces deprecated @app.on_event) ─────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await postgis.init_pool()
    await redis_cache.init_redis()
    await qdrant_mod.init_qdrant()   # also configures Gemini API key
    log.info("app_startup_complete", version=settings.app_version)
    yield
    await postgis.close_pool()
    await redis_cache.close_redis()
    await qdrant_mod.close_qdrant()
    log.info("app_shutdown_complete")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="MCP Sri Lanka Geo",
    version=settings.app_version,
    docs_url=None,   # disable in prod — no public swagger
    redoc_url=None,
    lifespan=lifespan,
)

# ── Body size limit middleware ────────────────────────────────────────────────
# MCP JSON-RPC messages are small. 1MB is generous; blocks oversized abuse before
# the body is read into memory.
_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1MB


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "Request body too large"}, status_code=413)
    return await call_next(request)


# ── Health endpoint ───────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    """
    Detailed dependency health check.
    Returns 200 if all required dependencies are ok (or degraded for Redis).
    Returns 503 if PostGIS or Qdrant are unreachable.

    Redis is "degraded" when down — cache is optional, tools still function.
    PostGIS and Qdrant are "error" when down — tools cannot function.
    """
    deps: dict[str, str] = {}

    # PostGIS — required
    try:
        pool = postgis.get_pool()
        await pool.fetchval("SELECT 1")
        deps["postgis"] = "ok"
    except Exception:
        deps["postgis"] = "error"

    # Qdrant — required for search_pois
    try:
        client = qdrant_mod.get_qdrant()
        await client.get_collections()
        deps["qdrant"] = "ok"
    except Exception:
        deps["qdrant"] = "error"

    # Redis — optional (cache degrades gracefully)
    try:
        r = redis_cache.get_redis()
        await r.ping()
        deps["redis"] = "ok"
    except Exception:
        deps["redis"] = "degraded"  # degraded, not error — cache is optional

    all_ok = all(v in ("ok", "degraded") for v in deps.values())
    status_code = 200 if all_ok else 503

    return JSONResponse(
        {"version": settings.app_version, "dependencies": deps},
        status_code=status_code,
    )


# ── API key verification (shared by SSE, pipeline endpoints) ─────────────────

def _verify_api_key(provided: str) -> bool:
    return any(
        secrets.compare_digest(provided, valid)
        for valid in settings.api_keys_list
    )


# ── Pipeline status + trigger endpoints ──────────────────────────────────────

_PIPELINE_TRIGGER_KEY = "pipeline:manual_trigger"


@app.get("/pipeline/status")
async def pipeline_status(request: Request) -> JSONResponse:
    """
    Returns the last 5 pipeline runs from pipeline_runs table.
    Requires X-API-Key header (same keys as SSE).
    """
    api_key = request.headers.get("X-API-Key", "")
    if not _verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    try:
        pool = postgis.get_pool()
        rows = await pool.fetch("""
            SELECT id, run_type, started_at, completed_at, status,
                   stats, error_message
            FROM pipeline_runs
            ORDER BY started_at DESC
            LIMIT 5
        """)
        runs = [
            {
                "id": r["id"],
                "run_type": r["run_type"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
                "status": r["status"],
                "stats": r["stats"],
                "error_message": r["error_message"],
            }
            for r in rows
        ]
        return JSONResponse({"runs": runs})
    except Exception:
        log.error("pipeline_status_error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch pipeline status")


@app.post("/pipeline/trigger")
async def pipeline_trigger(request: Request) -> JSONResponse:
    """
    Signal the scheduler to run the pipeline immediately.
    Sets Redis key  pipeline:manual_trigger = 1  — the scheduler wakes within 60s.
    Requires X-API-Key header.
    """
    api_key = request.headers.get("X-API-Key", "")
    if not _verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    try:
        r: aioredis.Redis = redis_cache.get_redis()
        await r.set(_PIPELINE_TRIGGER_KEY, "1", ex=3600)  # expires in 1h if not consumed
        log.info("pipeline_trigger_requested")
        return JSONResponse({"triggered": True, "message": "Scheduler will start pipeline within 60s"})
    except Exception:
        log.error("pipeline_trigger_error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to set trigger (Redis unavailable?)")


# ── SSE transport — ALWAYS requires auth ─────────────────────────────────────

# SseServerTransport handles the SSE <-> MCP protocol bridge.
# Messages are posted to /messages/ (trailing slash is part of the mount path).
_sse_transport = SseServerTransport("/messages/")


@app.get("/sse")
async def sse_endpoint(request: Request):
    """SSE transport for BizMind AI and other network clients."""
    api_key = request.headers.get("X-API-Key", "")
    if not _verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    async with _sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp._mcp_server.run(
            streams[0],
            streams[1],
            mcp._mcp_server.create_initialization_options(),
        )


async def _messages_asgi(scope, receive, send):
    """Thin ASGI shim around handle_post_message.

    handle_post_message is an ASGI app that sends its own HTTP responses, so it
    cannot be wrapped in a FastAPI route (would cause double-response errors).

    BrokenResourceError means the SSE connection already closed while a valid
    session exists — return 410 Gone rather than letting uvicorn log a 500.
    """
    try:
        await _sse_transport.handle_post_message(scope, receive, send)
    except anyio.BrokenResourceError:
        await send({
            "type": "http.response.start",
            "status": 410,
            "headers": [[b"content-type", b"text/plain"]],
        })
        await send({
            "type": "http.response.body",
            "body": b"SSE session closed",
            "more_body": False,
        })


app.mount("/messages", _messages_asgi)


# ── Entry points ──────────────────────────────────────────────────────────────

def run_http() -> None:
    """Run as HTTP server (SSE transport) — for BizMind AI integration."""
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )


def run_stdio() -> None:
    """Run as stdio MCP server — for Claude Desktop / Claude Code integration."""
    import asyncio
    from mcp.server.stdio import stdio_server

    async def _run():
        await postgis.init_pool()
        await redis_cache.init_redis()
        await qdrant_mod.init_qdrant()   # needed for search_pois
        log.info("stdio_server_starting")
        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "http"
    if mode == "stdio":
        run_stdio()
    else:
        run_http()
