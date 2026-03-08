FROM python:3.12-slim

WORKDIR /app

# System deps for osmium (pyosmium C++ bindings)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libosmium-dev \
    libprotozero-dev \
    libboost-program-options-dev \
    libexpat1-dev \
    zlib1g-dev \
    libbz2-dev \
    libprotobuf-dev \
    protobuf-compiler \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies before copying app code
# (layer cached unless requirements.txt changes)
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
