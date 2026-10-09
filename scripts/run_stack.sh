#!/usr/bin/env bash
# ── Zhanlu — run THIS checkout as its own stack ─────────────────────────────
#
#   project    zhanlu_n8n_main_version   (derived from this folder's name)
#   containers zhanlu-backend / zhanlu-postgres / zhanlu-frontend / …
#   UI         http://localhost:8089     API http://localhost:5002
#   pg 5400    redis 6300    minio 9400/9401
#
# Files: docker-compose.yml (base) + docker-compose.override.yml (frontend +
# restart policy). No port remapping, no renamed containers — this IS the
# canonical stack. The DB/redis/minio live in the named volumes of the same
# project, so `down` never touches your data.
#
# Usage:
#   scripts/run_stack.sh up        # build sandbox images + start (default)
#   scripts/run_stack.sh restart   # stop + start (no rebuild — fast)
#   scripts/run_stack.sh down      # stop (keeps volumes)
#   scripts/run_stack.sh status
#   scripts/run_stack.sh logs [service]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.override.yml)
UI_PORT="$(grep -E '^WEB_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8089)"
API_PORT="$(grep -E '^BACKEND_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 5002)"

wait_for() { # url name
  local url="$1" name="$2"
  for _ in $(seq 1 60); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || true)" = "200" ] && { echo "  $name UP"; return 0; }
    sleep 2
  done
  echo "  $name did NOT come up — check: ${COMPOSE[*]} logs"; return 1
}

build_sandbox() {
  echo "== building sandbox images"
  # Non-fatal: the sandbox-* services are build-only helpers; if the registry is
  # unreachable but the images already exist, the stack still starts fine.
  "${COMPOSE[@]}" build sandbox-python sandbox-office sandbox-pptx sandbox-webapp \
    || echo "  !! sandbox build failed — continuing (images likely already present)"
}

start_stack() {
  echo "== starting"
  "${COMPOSE[@]}" up -d --remove-orphans
  echo "== waiting for API"
  wait_for "http://localhost:${API_PORT}/healthz" "API  :${API_PORT}"
  echo "== waiting for UI"
  wait_for "http://localhost:${UI_PORT}/" "UI   :${UI_PORT}"
  echo
  echo "  UI  http://localhost:${UI_PORT}     API http://localhost:${API_PORT}"
}

case "${1:-up}" in
  up)      build_sandbox; start_stack ;;
  restart) echo "== stopping"; "${COMPOSE[@]}" down || true; start_stack ;;
  down)    "${COMPOSE[@]}" down || true ;;
  status)  "${COMPOSE[@]}" ps ;;
  logs)    shift; "${COMPOSE[@]}" logs -f --tail=100 "${@:-backend}" ;;
  *)       echo "usage: $0 {up|down|restart|status|logs [service]}"; exit 1 ;;
esac
