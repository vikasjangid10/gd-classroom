FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# aiortc, pylibsrtp and av all ship manylinux wheels that bundle their native
# dependencies (libsrtp, libopus, ffmpeg), so only curl is needed for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install -e ".[dev,voice]"

COPY . .
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/v1/healthz || exit 1

CMD ["/app/entrypoint.sh"]
