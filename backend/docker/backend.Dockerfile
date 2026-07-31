# ── Zhanlu Backend ────────────────────────────────────────────────────
# Local dev hot-reload image.  NOT for production.
# Build: docker compose build backend
#
# NOTE: Uses python:3.11-slim (Debian) instead of python:3.11-alpine because
# PyTorch (required by sentence-transformers for document RAG) does not
# publish musl/Alpine wheels. Debian/glibc has pre-built wheels for torch,
# chromadb, onnxruntime, and all other ML dependencies.

FROM python:3.11-slim

# ── APT mirror (Aliyun) ───────────────────────────────────────────────
# deb.debian.org is slow/unreliable from CN; mirror it like pip/npm.
# Handles both legacy sources.list and the new deb822 .sources format.
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    && sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list 2>/dev/null \
    || true

# ── System dependencies ──────────────────────────────────────────────
# Runtime deps only. nodejs/npm for agent-browser, chromium for browser
# automation, unixodbc for the MSSQL pyodbc connector.
# LibreOffice is installed in a separate layer below (after pip).
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    bash \
    nodejs \
    npm \
    chromium \
    libnss3 \
    libfreetype6 \
    libharfbuzz0b \
    ca-certificates \
    fonts-freefont-ttf \
    unixodbc \
    && rm -rf /var/lib/apt/lists/*

# ── agent-browser CLI (browser automation) ──────────────────────────
# Installed globally so the `agent_browser` tool wrapper can shell out.
# `agent-browser install` downloads the matching Chromium / CDP driver
# the CLI manages. We also use the system chromium for a faster first boot.
RUN npm config set registry https://registry.npmmirror.com
ARG AGENT_BROWSER_VERSION=latest
RUN npm install -g agent-browser@${AGENT_BROWSER_VERSION} \
    && agent-browser install --no-shell \
    || echo "agent-browser install failed (non-fatal; tool will report missing binary at runtime)"

# ── Create non-root user ─────────────────────────────────────────────
RUN useradd -m -u 10001 zhanlu

# ── App directory ────────────────────────────────────────────────────
WORKDIR /app

# ── Dependencies layer (cached) ──────────────────────────────────────
COPY requirements.txt .
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# ── LibreOffice (faithful document preview) ──────────────────────────
# Used by preview_builder.convert_to_preview() to convert docx/pptx/xlsx
# → PDF so the inline preview matches the downloaded file byte-for-byte.
# Installed as a separate layer AFTER pip so requirements.txt changes
# don't invalidate the (large) LibreOffice install cache.
# fonts-liberation provides metric-compatible Arial/Times/Courier fallbacks.
# fonts-noto provides broad Unicode coverage (CJK, symbols, emoji).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-liberation \
    fonts-noto \
    && rm -rf /var/lib/apt/lists/*

# ── Source (bind-mounted in dev, copied in build) ────────────────────
# In local dev, the source is bind-mounted via docker compose.
# The COPY is here so the image is self-contained if used standalone.
COPY . .

# ── Permissions ─────────────────────────────────────────────────────
RUN chown -R zhanlu:zhanlu /app

USER zhanlu

# ── Port ─────────────────────────────────────────────────────────────
EXPOSE 5002

# ── Entrypoint ───────────────────────────────────────────────────────
# prestart.sh handles: wait-for-pg + wait-for-redis + wait-for-minio
# + alembic upgrade head + idempotent seed → then starts uvicorn.
# In local dev with bind-mount, changes are auto-reloaded.
ENTRYPOINT ["/app/scripts/prestart.sh"]
