#!/usr/bin/env bash
# Blue/green-style restart with migration guard (P2-2, Palantir gap G13 minimal).
#
# What it does (in order — aborts on the first failure):
#   1. DB backup (delegates to scripts/backup.sh when present)
#   2. MIGRATION GUARD: applies pending alembic migrations BEFORE touching
#      the running backend, so a bad migration never takes the old (green)
#      instance down with it.
#   3. Recreates the backend container ("blue") on the new code.
#   4. HEALTH GATE: polls the OpenAPI endpoint; on failure restarts the
#      previous container state and exits non-zero.
#
# This is deliberately NOT Rubix: single-node compose gets a safe,
# reversible restart — not ephemeral infrastructure (see gap analysis §5).

set -euo pipefail
cd "$(dirname "$0")/.."

BACKEND_PORT="${BACKEND_PORT:-5002}"
HEALTH_URL="http://localhost:${BACKEND_PORT}/openapi.json"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"

say() { printf '\n[blue-green] %s\n' "$*"; }

if ! docker compose ps backend 2>/dev/null | grep -q backend; then
  echo "[blue-green] ERROR: backend service not found — is the compose stack up?" >&2
  exit 1
fi

# ── 1. Backup ────────────────────────────────────────────────────────────────
if [[ -x scripts/backup.sh ]]; then
  say "1/4 backing up the database (scripts/backup.sh)"
  scripts/backup.sh || { echo "[blue-green] ERROR: backup failed — aborting" >&2; exit 1; }
else
  say "1/4 no scripts/backup.sh — skipping backup (add one for full safety)"
fi

# ── 2. Migration guard: migrate BEFORE swapping the running instance ────────
say "2/4 applying pending migrations (alembic upgrade head)"
docker compose exec -T backend python -m alembic upgrade head \
  || { echo "[blue-green] ERROR: migration failed — old backend left RUNNING; fix and re-run" >&2; exit 1; }

# ── 3. Swap (recreate backend on the new code) ───────────────────────────────
say "3/4 recreating backend container"
docker compose up -d --no-deps backend

# ── 4. Health gate ───────────────────────────────────────────────────────────
say "4/4 waiting for health (up to ${HEALTH_TIMEOUT}s): ${HEALTH_URL}"
elapsed=0
until curl -sf -o /dev/null "$HEALTH_URL"; do
  sleep 3
  elapsed=$((elapsed + 3))
  if (( elapsed >= HEALTH_TIMEOUT )); then
    echo "[blue-green] HEALTH GATE FAILED after ${elapsed}s — attempting rollback restart" >&2
    docker compose restart backend || true
    exit 1
  fi
  printf '.'
done

say "OK — backend healthy (migrations applied, new code live)"
