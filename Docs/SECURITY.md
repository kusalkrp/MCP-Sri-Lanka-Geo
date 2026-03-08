# Security Documentation — MCP Sri Lanka Geo

**Version:** 1.0.0
**Author:** Kusal
**Date:** 2026-03-09

---

## Related Documentation

- [SYSTEM_SPEC.md](./SYSTEM_SPEC.md) for the production system architecture and dependency model this security posture protects
- [API_REFERENCE.md](./API_REFERENCE.md) for externally visible endpoint and transport behavior
- [DATA_PIPELINE_GUIDE.md](./DATA_PIPELINE_GUIDE.md) for operational pipeline steps that must follow the documented credential and backup controls
- [MCP_SRILANKA_GEO.md](./MCP_SRILANKA_GEO.md) for the original requirements and deployment design context

---

## 1. Threat Model

### 1.1 Assets

| Asset | Sensitivity | Risk if Compromised |
|-------|------------|---------------------|
| API keys | High | Unauthorized tool access, quota exhaustion |
| Database credentials | Critical | Full data exfiltration or deletion |
| Redis password | High | Cache poisoning, data exposure |
| Gemini API key | High | Financial cost, quota exhaustion |
| POI dataset | Medium | Competitive intelligence leak |

### 1.2 Attack Surface

| Surface | Exposure | Mitigation |
|---------|----------|-----------|
| `/sse` SSE endpoint | Public internet | API key auth, HTTPS |
| `/health` endpoint | Public internet | Read-only, no sensitive data |
| `/messages` message endpoint | Public internet | Session-bound, API key enforced at SSE |
| PostgreSQL port | Internal Docker network only (prod) | Not exposed externally |
| Qdrant port | Internal Docker network only (prod) | Not exposed externally |
| Redis port | Internal Docker network only (prod) | Password-protected, not exposed externally |
| stdio transport | Local process only | No network, no auth needed |

---

## 2. Authentication

### 2.1 SSE Transport — Always Required

The SSE endpoint enforces API key authentication unconditionally. There is no environment variable or feature flag that can disable SSE auth. This is by design.

```python
# app/main.py — auth enforced before SSE handshake
@app.get("/sse")
async def sse_endpoint(request: Request):
    api_key = request.headers.get("X-API-Key", "")
    if not _verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    # ... proceed with SSE connection
```

### 2.2 API Key Verification — Constant-Time Comparison

All API key comparisons use `secrets.compare_digest()` to prevent timing-based side-channel attacks. Standard `==` string comparison is explicitly prohibited.

```python
def _verify_api_key(provided: str) -> bool:
    return any(
        secrets.compare_digest(provided, valid)
        for valid in settings.api_keys_list
    )
```

`secrets.compare_digest()` takes the same amount of time regardless of how many characters match, making it immune to timing attacks that could allow an attacker to guess keys character by character.

### 2.3 API Key Requirements

Keys are validated at startup by Pydantic:
- **Minimum length:** 32 characters
- **Format:** Arbitrary string, typically `secrets.token_hex(32)` (64 hex chars)
- **Storage:** `.env` file only, never in code or version control
- **Rotation:** Replace in `.env`, restart server — no zero-downtime rotation in v1

Generating a strong API key:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
# Example: bizmind-cdb359897ff6120459c9fe5b0cb9c547a9c4ce2121f99fc9d5e54f9b
```

### 2.4 stdio Transport — Intentionally No Auth

The stdio transport is used by local clients (Claude Desktop, Claude Code) running as the same OS user. Network authentication is not applicable to an IPC pipe. `REQUIRE_AUTH` is a stdio-only gate, not an SSE gate.

```
REQUIRE_AUTH=true  → stdio: no auth check (always)
                   → SSE:   auth always enforced (REQUIRE_AUTH has no effect here)

REQUIRE_AUTH=false → stdio: no auth check (same as above)
                   → SSE:   auth still enforced (REQUIRE_AUTH has no effect here)
```

**Never** set `REQUIRE_AUTH=false` in a production `.env` — it provides no benefit and could confuse auditors.

### 2.5 SSE Message Endpoint Security

The `/messages` endpoint is not independently authenticated. It is session-bound: the SseServerTransport library validates that each POST references a session established through the authenticated `/sse` endpoint. Requests with invalid or stale session IDs receive a 404 or 410 response.

---

## 3. Transport Security

### 3.1 HTTPS (Production)

All external traffic is terminated at Caddy. The app container binds to `127.0.0.1:8080` and is never directly reachable.

```
Client → HTTPS (TLS 1.2+) → Caddy → HTTP → app:8080
```

Caddy handles:
- Auto-HTTPS via Let's Encrypt ACME challenge
- HTTP → HTTPS redirect
- TLS certificate renewal
- SSE `flush_interval -1` (required for streaming)
- Security headers (see `Caddyfile`)

### 3.2 Security Headers

Set by Caddy on all responses:

```
Strict-Transport-Security: max-age=31536000; includeSubDomains  (HSTS — 1 year)
X-Content-Type-Options: nosniff                                 (MIME sniffing)
X-Frame-Options: DENY                                           (clickjacking)
Referrer-Policy: strict-origin-when-cross-origin
```

### 3.3 Internal Network Isolation

In production (`docker-compose.prod.yml`), infrastructure services are not accessible from the host:

```yaml
postgres:
  ports: []  # no host binding — internal Docker network only

qdrant:
  ports: []  # internal only

redis:
  ports: []  # internal only
```

The app communicates with these services via the Docker internal network using service names (`postgres`, `qdrant`, `redis`).

---

## 4. Request Security

### 4.1 Body Size Limit

A FastAPI middleware limits request bodies to 1MB. This prevents denial-of-service attacks where a client sends large payloads to exhaust memory before the request is processed.

```python
_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1MB

@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "Request body too large"}, status_code=413)
    return await call_next(request)
```

MCP JSON-RPC messages are small by design. 1MB is generous for protocol messages but prevents abuse.

### 4.2 SQL Injection Prevention

All database queries use asyncpg parameterized statements with `$1`, `$2`, ... placeholders. String interpolation into SQL is explicitly prohibited.

```python
# CORRECT — parameterized
await conn.fetch("SELECT * FROM pois WHERE id = $1", poi_id)

# PROHIBITED — SQL injection risk
await conn.fetch(f"SELECT * FROM pois WHERE id = '{poi_id}'")
```

asyncpg never interprets user input as SQL syntax when using parameterized queries.

### 4.3 Coordinate Validation

All spatial queries validate coordinates against Sri Lanka's bounding box before touching the database. This prevents:
- NULL island (0, 0) — common OSM data bug
- Out-of-country queries that waste DB resources
- Edge cases in PostGIS spatial functions

```python
SL_BOUNDS = {"lat_min": 5.85, "lat_max": 9.9, "lng_min": 79.5, "lng_max": 81.9}

def _validate_coords(lat: float, lng: float) -> str | None:
    if lat == 0.0 and lng == 0.0:
        return "Null island coordinates (0, 0)"
    if not (5.85 <= lat <= 9.9 and 79.5 <= lng <= 81.9):
        return f"Coordinates outside Sri Lanka bounds"
    return None
```

### 4.4 Error Handling — No Stack Trace Leakage

All MCP tool handlers catch exceptions and return structured error objects. Internal error details (stack traces, query text, file paths) are never returned to clients — only logged internally.

```python
try:
    # ... tool logic
except Exception as exc:
    log.error("tool_failed", tool="find_nearby", error=repr(exc))  # logged
    return {"error": "Internal error — try again"}  # opaque to client
```

### 4.5 Input Sanitization

- `poi_id` inputs are stripped of whitespace
- `query` strings are stripped before embedding
- Empty string inputs return structured errors, not exceptions
- `limit` and `radius_km` are clamped to safe maximums (100 results, 100km radius)

---

## 5. Data Security

### 5.1 Soft Deletes Only

Records in the `pois` table are never hard-deleted. Removed POIs have `deleted_at` set to the current timestamp. This:
- Provides an audit trail for all data changes
- Allows recovery from accidental ingest errors
- Prevents timing attacks where an attacker rapidly queries to detect deletions

Every single query against `pois` filters `WHERE deleted_at IS NULL`. This is enforced inside every helper function in `postgis.py`, never left to the caller.

### 5.2 Restricted Database User

The `app/db/migrations/002_app_user.sql` migration creates a `srilanka_app` role with minimal privileges:

```sql
-- Read + update pois (for last_embed_sync writes)
GRANT SELECT, UPDATE ON pois TO srilanka_app;
-- Read-only admin boundaries
GRANT SELECT ON admin_boundaries TO srilanka_app;
-- Full access to stats and run logs
GRANT SELECT, INSERT, UPDATE ON category_stats, pipeline_runs TO srilanka_app;
```

The app cannot DROP tables, TRUNCATE, or access other databases. This limits the blast radius if the app is compromised.

### 5.3 Credential Management

- All secrets in `.env` — never in code or version control
- `.gitignore` includes `.env`, `data/*.pbf`, `data/backups/`
- Redis URL format: `redis://:password@host:port` (password in URL, logged as host-only)
- Database URL: password included in URL, log lines strip to `host:port/db` only

```python
# redis_cache.py — safe logging
parsed = urlparse(settings.redis_url)
log.info("redis_client_ready", host=parsed.hostname, port=parsed.port)
# Never: log.info("redis_ready", url=settings.redis_url)
```

### 5.4 Redis Security

Redis is configured with:
- `requirepass` — authentication required for all operations
- `maxmemory 256mb` + `allkeys-lru` — prevents unbounded memory growth
- `appendonly yes` — AOF persistence for crash recovery
- No external port binding in production

---

## 6. Rate Limiting

### 6.1 v1: Soft Circuit Breaker (Warning Only)

v1 implements a soft per-API-key rate counter in Redis. It warns when a key exceeds 100 requests per minute but does not reject requests. This is designed to detect runaway agent loops consuming Gemini quota.

```python
async def check_rate_soft(api_key_hash: str, window_sec=60, warn_threshold=100):
    key = f"rate:{api_key_hash}:{int(time.time() // window_sec)}"
    count = await redis.incr(key)
    await redis.expire(key, window_sec * 2)
    if count > warn_threshold:
        log.warning("rate_threshold_exceeded", key_hash=api_key_hash, count=count)
    # v1: warn only — do not reject
```

The `api_key_hash` is a SHA256 hash of the API key — the raw key is never stored in Redis.

### 6.2 v1.1 Planned: Hard Rate Limiting

A future version will add a hard cap with `429 Too Many Requests` responses for keys exceeding the limit.

---

## 7. Container Security

### 7.1 Non-Root User

The app container runs as a dedicated non-root user `mcp`:

```dockerfile
RUN addgroup --system mcp && adduser --system --ingroup mcp mcp
USER mcp
```

If the app process is compromised, the attacker cannot write to the host filesystem, install system packages, or escalate privileges.

### 7.2 Read-Only Data Mount

The data directory is mounted read-only:

```yaml
volumes:
  - ./data:/app/data:ro
```

The app cannot modify the source data files even if exploited.

### 7.3 No Swagger/ReDoc UI

Swagger and ReDoc are disabled in production:

```python
app = FastAPI(
    docs_url=None,   # disabled
    redoc_url=None,  # disabled
)
```

This eliminates an attack vector where API schemas could be automatically enumerated.

### 7.4 Health Endpoint — No Sensitive Data

The `/health` endpoint returns only operational status, never credentials, configuration, or internal details:

```json
{
  "version": "1.0.0",
  "dependencies": {
    "postgis": "ok",
    "qdrant": "ok",
    "redis": "ok"
  }
}
```

---

## 8. Operational Security

### 8.1 Pre-Ingest Backup

A `pg_dump` backup is required before every full data ingest. This provides a recovery point if the ingest produces anomalous results (> 5% POI count drop, > 1000 unexpected deletions).

```bash
pg_dump $DATABASE_URL --format=custom \
  --file=backups/pois_$(date +%Y%m%d_%H%M%S).dump \
  --table=pois --table=admin_boundaries --table=category_stats
```

### 8.2 Structured Logging

All tool calls are logged with `structlog` including:
- `tool` name
- `duration_ms`
- `result_count`
- `cache_hit` status

All errors are logged with `exc_info` (stack trace to log, not to client).

Logs never include: raw API keys, database credentials, or user PII.

### 8.3 Gemini API Key Protection

The Gemini API key is loaded from environment only. The `embed_with_retry` function wraps the synchronous Gemini SDK in `run_in_executor` — the key is never serialized, logged, or passed over the network.

### 8.4 Dependency Versions

Key security-relevant dependencies are pinned:

```
mcp==1.3.0          # never auto-upgrade — breaking changes are frequent
qdrant-client       # pin after first run
postgis/postgis:16-3.4  # specific image version
qdrant/qdrant:v1.13.0   # pinned
redis:7-alpine          # major version pinned
```

---

## 9. Security Checklist

### Deployment Checklist

- [ ] `API_KEYS` set in `.env` — each key ≥ 32 characters
- [ ] `DB_PASSWORD` is a random 32+ character string — not a word
- [ ] `REDIS_PASSWORD` is a random 32+ character string
- [ ] `GEMINI_API_KEY` loaded from environment, not hardcoded
- [ ] `.env` is in `.gitignore` — never committed
- [ ] `data/*.pbf` is in `.gitignore` — never committed
- [ ] Production uses `docker-compose.prod.yml` — infra ports not exposed
- [ ] Caddy configured with valid domain for auto-HTTPS
- [ ] `REQUIRE_AUTH=true` in production `.env`
- [ ] `docs_url=None` and `redoc_url=None` in FastAPI (already set)
- [ ] Backup script tested before first ingest

### Code Review Checklist

- [ ] All `pois` queries include `WHERE deleted_at IS NULL`
- [ ] All SQL uses `$1` parameterized placeholders — no f-strings in SQL
- [ ] API key comparison uses `secrets.compare_digest()` — never `==`
- [ ] Tool handlers catch all exceptions — no stack trace returned to client
- [ ] Coordinate validation called before every spatial query
- [ ] No secrets in logs — URL credentials stripped before logging

---

## 10. Incident Response

### 10.1 Compromised API Key

1. Remove the key from `API_KEYS` in `.env`
2. `docker compose restart app`
3. Generate new key: `python -c "import secrets; print(secrets.token_hex(32))"`
4. Add to `.env` and restart
5. Distribute new key to affected consumer

### 10.2 Suspected Data Breach

1. Stop the server: `docker compose stop app`
2. Review structured logs for anomalous access patterns
3. Check Redis for unusual keys: `redis-cli -a $REDIS_PASSWORD keys '*'`
4. Rotate all credentials (DB, Redis, Gemini, API keys)
5. Review PostgreSQL access logs

### 10.3 Anomalous Ingest Results

If `validate_dataset.py` reports > 5% POI drop or > 1000 unexpected deletions:

```bash
# Restore from most recent backup
pg_restore --clean --if-exists -d $DATABASE_URL /tmp/backups/pre_pipeline.dump
docker compose restart app
```
