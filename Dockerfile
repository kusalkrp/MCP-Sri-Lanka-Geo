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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "-m", "app.main"]
