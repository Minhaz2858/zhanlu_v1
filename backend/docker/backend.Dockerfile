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

# ── Theme fonts for deck rendering ──────────────────────────────────
# The 12 HTML-design ThemePresets declare Google Fonts family names
# (Archivo Black, Space Grotesk, Manrope, Fraunces, ...) that the base
# image does NOT ship — without them Chromium/LibreOffice fall back to a
# generic sans and every deck loses its intended typeface ("looks like a
# plain template" complaint).  We bundle free Google Fonts (the same set
# the canvas-design skill ships) + a fontconfig alias table that maps each
# declared theme font to an installed family, so the image-fill pipeline
# renders the intended look.  CJK: Noto Sans SC (sans) + Source Han Serif
# CN (serif) give Chinese decks real glyphs (fonts-noto in Debian does not
# include CJK — :lang=zh was empty before this step).
COPY docker/fonts/zhanlu-fonts.conf /etc/fonts/conf.d/99-zhanlu-theme-fonts.conf
RUN mkdir -p /usr/share/fonts/truetype/zhanlu \
    && cp skills/canvas-design/canvas-fonts/*.ttf /usr/share/fonts/truetype/zhanlu/ \
    && cp docker/fonts/NotoSansSC-Regular.ttf /usr/share/fonts/truetype/zhanlu/ \
    && cp docker/fonts/SourceHanSerif-Regular.ttf /usr/share/fonts/truetype/zhanlu/ \
    && fc-cache -f >/dev/null 2>&1 \
    && fc-match "Manrope" | grep -qi "outfit" \
    && fc-match "Microsoft YaHei" | grep -qiE "noto|arial" \
    || echo "WARNING: theme font alias verification failed (non-fatal)"

# ── slide-skill: native-editable PPTX tier ──────────────────────────
# slide-skill (bundled in skills/ppt_skills) is the SVG-first pipeline that
# produces FULLY-EDITABLE native .pptx (real text frames) — used when the
# user asks for an editable/tweakable deck (HTML_DESIGN_EDITABLE_ENABLED).
# The HTML image-fill path bakes PNGs per slide; this tier is the editable
# alternative.  Also patches its chromium invocations with --no-sandbox so
# the render-convergence QA gate works for the non-root zhanlu user.
RUN /usr/local/bin/python -m pip install -e /app/skills/ppt_skills/slide-skill-master --no-cache-dir -q \
    && grep -q "no-sandbox" /app/skills/ppt_skills/slide-skill-master/tools/slide/src/slide_skill/measurement_contracts.py \
    || (sed -i 's/"--disable-gpu",$/&\\n                "--no-sandbox",/' /app/skills/ppt_skills/slide-skill-master/tools/slide/src/slide_skill/measurement_contracts.py \
        && sed -i 's/            "--disable-gpu",$/            &\\n            "--no-sandbox",/' /app/skills/ppt_skills/slide-skill-master/tools/slide/src/slide_skill/measurement_contracts.py)

# ── Permissions ─────────────────────────────────────────────────────
RUN chown -R zhanlu:zhanlu /app
# FIX 2026-08-29: pre-create the user-skills dir with the right owner so a
# FRESH named volume mounted at ~/.zhanlu/skills inherits zhanlu ownership
# (Docker copies image dir ownership into empty named volumes). Without
# this, create_skill's write_skill_md hits PermissionError on a fresh
# volume and the created skill silently stays DB-only/invisible.
RUN mkdir -p /home/zhanlu/.zhanlu/skills && chown -R zhanlu:zhanlu /home/zhanlu/.zhanlu

USER zhanlu

# ── Port ─────────────────────────────────────────────────────────────
EXPOSE 5002

# ── Entrypoint ───────────────────────────────────────────────────────
# prestart.sh handles: wait-for-pg + wait-for-redis + wait-for-minio
# + alembic upgrade head + idempotent seed → then starts uvicorn.
# In local dev with bind-mount, changes are auto-reloaded.
ENTRYPOINT ["/app/scripts/prestart.sh"]
