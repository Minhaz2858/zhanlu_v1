#!/usr/bin/env bash
# ── Zhanlu Prestart ────────────────────────────────────────────────────
# Runs inside the backend container before uvicorn starts.
# 1. Wait for PostgreSQL, Redis, MinIO to be ready
# 2. Run Alembic migrations
# 3. Idempotently seed the database
# 4. Start the FastAPI server
#
# Usage: ./scripts/prestart.sh
set -euo pipefail

echo "=== Zhanlu Prestart ==="

# ── Wait for PostgreSQL ───────────────────────────────────────────────
echo "[prestart] Waiting for PostgreSQL..."
for i in $(seq 1 30); do
  if pg_isready -h "${POSTGRES_HOST:-postgres}" -U "${POSTGRES_USER:-zhanlu}" -d "${POSTGRES_DB:-zhanlu}" -q 2>/dev/null; then
    echo "[prestart] PostgreSQL is ready."
    break
  fi
  echo "[prestart] PostgreSQL not ready (attempt $i/30)..."
  sleep 2
done

# ── Wait for Redis ────────────────────────────────────────────────────
echo "[prestart] Waiting for Redis..."
for i in $(seq 1 30); do
  if python -c "
import os, sys
try:
    import redis
    r = redis.from_url(os.environ.get('REDIS_URL', 'redis://redis:6379/0'))
    r.ping()
    print('OK')
except Exception as e:
    sys.exit(1)
" 2>/dev/null; then
    echo "[prestart] Redis is ready."
    break
  fi
  echo "[prestart] Redis not ready (attempt $i/30)..."
  sleep 2
done

# ── Wait for MinIO ────────────────────────────────────────────────────
MINIO_HOST="${MINIO_ENDPOINT:-minio:9000}"
echo "[prestart] Waiting for MinIO at ${MINIO_HOST}..."
for i in $(seq 1 30); do
  if curl -sf "http://${MINIO_HOST}/minio/health/live" >/dev/null 2>&1; then
    echo "[prestart] MinIO is ready."
    break
  fi
  echo "[prestart] MinIO not ready (attempt $i/30)..."
  sleep 2
done

# ── Alembic Migrations ────────────────────────────────────────────────
echo "[prestart] Running Alembic migrations..."
python -m alembic upgrade head
echo "[prestart] Migrations complete."

# ── Seed Database ─────────────────────────────────────────────────────
echo "[prestart] Seeding database (idempotent)..."
python seed.py
echo "[prestart] Seed complete."

# ── Start Application ─────────────────────────────────────────────────
# --reload is disabled by default to save RAM (~200-500 MB per worker).
# Set UVICORN_RELOAD=true to enable hot-reload for active development.
echo "[prestart] Starting uvicorn on port ${BACKEND_PORT:-5002}..."
if [ "${UVICORN_RELOAD:-false}" = "true" ]; then
  echo "[prestart] Hot-reload ENABLED (UVICORN_RELOAD=true)"
  exec uvicorn main:app --host 0.0.0.0 --port "${BACKEND_PORT:-5002}" --reload
else
  echo "[prestart] Hot-reload DISABLED. Set UVICORN_RELOAD=true in .env to enable."
  exec uvicorn main:app --host 0.0.0.0 --port "${BACKEND_PORT:-5002}"
fi
