"""
main.py
FastAPI app + FastMCP server.

Transports:
    stdio  — local use (Claude Desktop, Claude Code); auth disabled by design
    SSE    — network clients (BizMind AI, EduIntel LK); auth ALWAYS required

Startup order:
    1. Init asyncpg pool (postgis.init_pool)
    2. Init Redis client (redis_cache.init_redis)
    3. Register MCP tools
    4. Mount MCP SSE handler on /sse
    5. Serve

Auth rules:
    - SSE endpoint ALWAYS requires X-API-Key header — no env flag disables this
    - stdio transport has no auth (local process, not network)
    - REQUIRE_AUTH=false is only for local stdio testing; never in production .env
"""

from __future__ import annotations

import sys

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

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

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="MCP Sri Lanka Geo",
    version=settings.app_version,
    docs_url=None,   # disable in prod — no public swagger
    redoc_url=None,
)


@app.on_event("startup")
async def startup() -> None:
    await postgis.init_pool()
    await redis_cache.init_redis()
    await qdrant_mod.init_qdrant()
    log.info("app_startup_complete", version=settings.app_version)


@app.on_event("shutdown")
async def shutdown() -> None:
    await postgis.close_pool()
    await redis_cache.close_redis()
    await qdrant_mod.close_qdrant()
    log.info("app_shutdown_complete")


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


# ── SSE transport — ALWAYS requires auth ─────────────────────────────────────

def _verify_api_key(provided: str) -> bool:
    import secrets
    return any(
        secrets.compare_digest(provided, valid)
        for valid in settings.api_keys_list
    )


@app.get("/sse")
async def sse_endpoint(request: Request):
    """SSE transport for BizMind AI and other network clients."""
    api_key = request.headers.get("X-API-Key", "")
    if not _verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    # Delegate to FastMCP's SSE handler
    return await mcp.sse_app()(request.scope, request.receive, request.send)


@app.post("/messages")
async def messages_endpoint(request: Request):
    """SSE message posting endpoint — auth checked via session established at /sse."""
    return await mcp.sse_app()(request.scope, request.receive, request.send)


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
