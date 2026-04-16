# ─── Stage 1: base with unrar + Python deps ───────────────────────────────
FROM python:3.12-slim-bookworm AS base

# Add Debian non-free for the official unrar binary (supports RAR5 + split)
RUN echo "deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware" \
      > /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      unrar \
      curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies separately so Docker caches them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── Stage 2: final image ──────────────────────────────────────────────────
FROM base AS final

# Build-time args — GitHub Actions injects these automatically via
# docker/metadata-action. Unraid reads the resulting OCI labels to display
# version info and to know whether a newer image is available.
ARG BUILD_DATE
ARG VERSION
ARG VCS_REF

LABEL org.opencontainers.image.title="UnrarTool" \
      org.opencontainers.image.description="Automated split-RAR extractor with a modern web UI for Unraid" \
      org.opencontainers.image.url="https://github.com/hythamjurdi/unrartool" \
      org.opencontainers.image.source="https://github.com/hythamjurdi/unrartool" \
      org.opencontainers.image.documentation="https://github.com/hythamjurdi/unrartool/blob/main/README.md" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="hythamjurdi"

COPY app/ ./app/

# Default paths (override via environment in docker-compose / Unraid template)
ENV DATA_PATH=/data \
    CONFIG_PATH=/config \
    PORT=8080

EXPOSE 8080

VOLUME ["/data", "/config"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8080/ || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
