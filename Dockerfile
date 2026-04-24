FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ripgrep zstd \
    && rm -rf /var/lib/apt/lists/*

# Install Codex CLI (Linux).
ARG CODEX_VERSION=0.124.0
ARG TARGETARCH
RUN set -eu; \
    ARCH_RAW="${TARGETARCH:-$(uname -m)}"; \
    case "${ARCH_RAW}" in \
      amd64|x86_64) CODEX_ASSET_ARCH="x86_64" ;; \
      arm64|aarch64) CODEX_ASSET_ARCH="aarch64" ;; \
      *) echo "Unsupported architecture: ${ARCH_RAW} (expected: amd64/x86_64 or arm64/aarch64)"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/codex.zst "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${CODEX_ASSET_ARCH}-unknown-linux-musl.zst"; \
    zstd -d /tmp/codex.zst -o /usr/local/bin/codex; \
    rm -f /tmp/codex.zst; \
    chmod +x /usr/local/bin/codex; \
    /usr/local/bin/codex --version

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN useradd -m -u 10001 app
USER app
ENV HOME=/home/app

COPY --chown=app:app app.py /app/app.py

ENV PORT=8100
EXPOSE 8100

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
