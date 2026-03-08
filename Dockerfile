FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies before copying app code
# (layer cached unless requirements.txt changes)
# requirements.txt is production-only — no osmium, no pytest, no ingestion deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Non-root user — never run as root in production
RUN addgroup --system mcp && adduser --system --ingroup mcp mcp \
    && chown -R mcp:mcp /app
USER mcp

EXPOSE 8080

# Use uvicorn directly for production (predictable process management)
# --no-access-log: access logging handled by Caddy
# --workers 1: single worker — asyncpg pool is process-local, multi-worker
#              would create separate pools (wastes connections)
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--no-access-log", \
     "--workers", "1"]
