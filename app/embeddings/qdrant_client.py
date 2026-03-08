"""
qdrant_client.py
Qdrant async client + Gemini text-embedding-004 helpers.

Responsibilities:
    - Idempotent collection creation with payload indexes
    - Batch embed (Gemini, 100 texts/call, exponential backoff)
    - Semantic search with optional spatial ID filter
    - Query embedding cache via Redis (1h TTL, sha256[:16] key)

Collection: srilanka_pois
Dimensions:  768 (text-embedding-004)
Distance:    COSINE
Payload fields indexed: category, subcategory, district, province
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from typing import Any

import google.generativeai as genai
import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.config import settings

log = structlog.get_logger()

COLLECTION_NAME = settings.qdrant_collection
EMBED_MODEL     = "models/gemini-embedding-001"  # text-embedding-004 not in v1beta
EMBED_DIM       = 768  # set via output_dimensionality (default would be 3072)
EMBED_BATCH     = 100   # Gemini API limit per call
QDRANT_BATCH    = 100   # points per upsert batch

# ── Module-level Qdrant client singleton ────────────────────────────────────

_qdrant: AsyncQdrantClient | None = None


async def init_qdrant() -> AsyncQdrantClient:
    global _qdrant
    if _qdrant is not None:
        return _qdrant

    kwargs: dict[str, Any] = {"url": settings.qdrant_url}
    if settings.qdrant_api_key:
        kwargs["api_key"] = settings.qdrant_api_key

    _qdrant = AsyncQdrantClient(**kwargs)

    # Configure Gemini once at startup — not on every embed call
    _configure_gemini()

    log.info("qdrant_client_ready", url=settings.qdrant_url, collection=COLLECTION_NAME)
    return _qdrant


async def close_qdrant() -> None:
    global _qdrant
    client, _qdrant = _qdrant, None
    if client:
        try:
            await client.close()
        except Exception:
            pass


def get_qdrant() -> AsyncQdrantClient:
    if _qdrant is None:
        raise RuntimeError("Qdrant client not initialised — call init_qdrant() first")
    return _qdrant


# ── Collection management ────────────────────────────────────────────────────

async def ensure_collection() -> None:
    """Create the collection + payload indexes if they don't exist. Idempotent."""
    client = get_qdrant()

    existing = await client.get_collections()
    existing_names = {c.name for c in existing.collections}

    if COLLECTION_NAME not in existing_names:
        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        log.info("qdrant_collection_created", collection=COLLECTION_NAME)

        # Payload indexes — required for filter performance on search_pois
        for field in ("category", "subcategory", "district", "province"):
            await client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        log.info("qdrant_payload_indexes_created")
    else:
        log.info("qdrant_collection_exists", collection=COLLECTION_NAME)


# ── Gemini embedding ─────────────────────────────────────────────────────────

def _configure_gemini() -> None:
    """Configure Gemini API key. Safe to call multiple times."""
    if settings.gemini_api_key:
        genai.configure(api_key=settings.gemini_api_key)


def _as_dict(value) -> dict:
    """Coerce asyncpg JSONB value (may be str or dict) to dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            import json
            return json.loads(value)
        except Exception:
            return {}
    return {}


def build_embed_text(poi: dict) -> str:
    """
    Null-safe embedding text builder.
    Never pass None values into the embedding — "None" corrupts vectors.
    asyncpg may return JSONB as a string or a dict — handled via _as_dict().
    """
    address    = _as_dict(poi.get("address"))
    enrichment = _as_dict(poi.get("enrichment"))

    parts = [
        poi.get("name"),
        poi.get("name_si"),
        poi.get("subcategory"),
        poi.get("category"),
        address.get("district"),
        address.get("province"),
        enrichment.get("description"),
    ]
    return " | ".join(p for p in parts if p and str(p).strip())


def _embed_sync(texts: list[str], task_type: str) -> list[list[float]]:
    """Synchronous Gemini embed call — always run via run_in_executor."""
    result = genai.embed_content(
        model=EMBED_MODEL,
        content=texts,
        task_type=task_type,
        output_dimensionality=EMBED_DIM,
    )
    embeddings = result["embedding"]
    if isinstance(embeddings[0], float):
        return [embeddings]
    return embeddings


async def embed_with_retry(
    texts: list[str],
    max_retries: int = 5,
    task_type: str = "RETRIEVAL_DOCUMENT",
) -> list[list[float]]:
    """
    Embed a batch of texts using Gemini text-embedding-004.
    Runs the synchronous Gemini SDK call in a thread pool executor so it never
    blocks the asyncio event loop.
    Exponential backoff on 429 (rate limit) and transient errors.
    texts must be <= EMBED_BATCH (100) in length.
    """
    loop = asyncio.get_event_loop()

    for attempt in range(max_retries):
        try:
            fn = functools.partial(_embed_sync, texts, task_type)
            return await loop.run_in_executor(None, fn)

        except Exception as exc:
            err_str = str(exc).lower()
            is_rate_limit = "429" in err_str or "quota" in err_str or "rate" in err_str
            wait = 2 ** attempt
            if attempt < max_retries - 1:
                log.warning("embed_retry",
                            attempt=attempt + 1,
                            wait_sec=wait,
                            rate_limit=is_rate_limit,
                            error=repr(exc))
                await asyncio.sleep(wait)
            else:
                log.error("embed_failed_after_retries", attempts=max_retries, error=repr(exc))
                raise

    raise RuntimeError("embed_with_retry called with max_retries <= 0")


async def embed_query_cached(query: str) -> list[float]:
    """
    Embed a query string with Redis caching (1h TTL).
    Identical queries (hospital, bank, restaurant) hit cache.
    Falls back to direct embed on Redis miss/failure.
    """
    from app.cache.redis_cache import get_redis

    key = f"embed:{hashlib.sha256(query.encode()).hexdigest()[:16]}"
    r = get_redis()

    try:
        cached = await r.get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass  # Redis down — fall through to Gemini

    vectors = await embed_with_retry([query], task_type="RETRIEVAL_QUERY")
    vector = vectors[0]

    try:
        await r.setex(key, 3600, json.dumps(vector))
    except Exception:
        pass

    return vector


# ── Semantic search ───────────────────────────────────────────────────────────

async def search_collection(
    vector: list[float],
    filter_ids: list[str] | None = None,
    category: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Vector search against the collection.

    filter_ids: if given, restrict to these POI IDs (spatial pre-filter results).
    category: optional category filter applied via payload index.

    Returns list of {poi_id, name, category, subcategory, district, province,
                      lat, lng, score}.
    """
    client = get_qdrant()

    # Build filter
    conditions = []
    if filter_ids is not None:
        # MatchAny on poi_id payload field
        conditions.append(
            FieldCondition(key="poi_id", match=MatchAny(any=filter_ids))
        )
    if category:
        conditions.append(
            FieldCondition(key="category", match=MatchAny(any=[category]))
        )

    query_filter = Filter(must=conditions) if conditions else None

    response = await client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )

    return [
        {
            **r.payload,
            "score": r.score,
        }
        for r in response.points
    ]


# ── Upsert helpers (used by generate_embeddings.py) ──────────────────────────

async def upsert_points(points: list[PointStruct]) -> None:
    """Upsert a batch of points into the collection."""
    client = get_qdrant()
    await client.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)


def make_point(poi_id: str, qdrant_id: str, vector: list[float],
               poi: dict) -> PointStruct:
    """Build a Qdrant PointStruct from a POI dict + pre-generated UUID."""
    address = _as_dict(poi.get("address"))
    return PointStruct(
        id=qdrant_id,
        vector=vector,
        payload={
            "poi_id":      poi_id,
            "name":        poi.get("name"),
            "name_si":     poi.get("name_si"),
            "category":    poi.get("category"),
            "subcategory": poi.get("subcategory"),
            "district":    address.get("district"),
            "province":    address.get("province"),
            "lat":         poi.get("lat"),
            "lng":         poi.get("lng"),
            "quality_score": poi.get("quality_score"),
        },
    )
