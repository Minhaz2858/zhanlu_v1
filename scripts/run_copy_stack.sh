#!/usr/bin/env bash
# ── Run THIS checkout as an isolated stack, side-by-side with the Acme one ──
#
# Two independent Zhanlu lineages live on this Mac:
#   Acme_zhanlu_v.1        project zhanlu_sep_1_n8n   UI 8088  API 5002  pg 5400  redis 6300  minio 9400/9401
#   zhanlu_n8n_main_version  project zhanlu_copy        UI 8090  API 5012  pg 5401  redis 6301  minio 9402/9403   <- THIS
#
# Everything that could collide is separated:
#   project name    -p zhanlu_copy
#   containers      zhanlu-copy-*
#   networks        zhanlu_copy_app_net / zhanlu_copy_data_net
#   volumes         zhanlu_copy_* named volumes (own postgres data)
#   image tags      zhanlu-copy-backend / zhanlu-copy-sandbox-worker   (base file shares `zhanlu-backend:latest` with Acme!)
#   host ports      see table above
#
# Usage:
#   scripts/run_copy_stack.sh up       # build + start (default)
#   scripts/run_copy_stack.sh down     # stop THIS stack only
#   scripts/run_copy_stack.sh restart
#   scripts/run_copy_stack.sh status
#   scripts/run_copy_stack.sh logs [service]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROJECT=zhanlu_copy
COMPOSE=(docker compose -p "$PROJECT"
         -f docker-compose.yml
         -f docker-compose.override.yml
         -f docker-compose.copy.yml)

UI_PORT=8090
API_PORT=5012
COPY_PORTS=(5012 8090 5401 6301 9402 9403)
DEMO_PORTS=(5002 8088 5400 6300 9400 9401)

# Fail loudly if a NON-docker process squats a port we need. Docker owning it is
# expected (our own stack) — anything else means the ports drifted.
check_ports() {
  local bad=0
  for p in "${COPY_PORTS[@]}"; do
    local owners
    owners=$(lsof -nP -iTCP:"$p" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1}' | sort -u | tr '\n' ' ')
    if [ -n "$owners" ] && ! echo "$owners" | grep -q 'com.docke'; then
      echo "  !! port $p held by: $owners"
      bad=1
    fi
  done
  if [ "$bad" -ne 0 ]; then
    echo "Refusing to start: a non-Docker process holds one of the copy ports."
    exit 1
  fi
  echo "  ports ${COPY_PORTS[*]} clear (or owned by our own Docker stack)"
}

guard_acme() {
  # Never let this script touch the Acme lineage's ports or image tags.
  for p in "${DEMO_PORTS[@]}"; do
    if [ "$p" = "$UI_PORT" ] || [ "$p" = "$API_PORT" ]; then
      echo "FATAL: script would collide with Acme port $p"; exit 1
    fi
  done
  if "${COMPOSE[@]}" config 2>/dev/null | grep -qE 'image: zhanlu-(backend|sandbox-worker):latest'; then
    echo "FATAL: shared image tag still configured — rebuild would clobber Acme's image."; exit 1
  fi
}

case "${1:-up}" in
  up)
    echo "== port check"; check_ports
    echo "== shared-tag guard"; guard_acme
    echo "== building isolated images (zhanlu-copy-*)"
    if ! "${COMPOSE[@]}" build backend sandbox-worker; then
      echo
      echo "  !! build failed (usually: registry.docker.io unreachable on this network)."
      if docker image inspect zhanlu-copy-backend:latest >/dev/null 2>&1; then
        echo "  !! local zhanlu-copy-backend:latest present -> continuing."
        echo "     Note: app code is bind-mounted from ./backend, so only Python"
        echo "     DEPENDENCIES come from the image. Rebuild when registry access returns."
      else
        echo "  !! no local image and build failed -> cannot start."; exit 1
      fi
    fi
    echo "== starting"
    "${COMPOSE[@]}" up -d
    echo "== waiting for API"
    for i in $(seq 1 60); do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:$API_PORT/" || true)
      [ "$code" = "200" ] && break
      sleep 2
    done
    echo "== waiting for UI"
    for i in $(seq 1 30); do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:$UI_PORT/" || true)
      [ "$code" = "200" ] && break
      sleep 2
    done
    echo
    echo "  THIS project  UI  http://localhost:$UI_PORT     API http://localhost:$API_PORT"
    echo "  Acme        UI  http://localhost:8088    API http://localhost:5002   (untouched)"
    ;;
  down)    "${COMPOSE[@]}" down ;;
  restart) "$0" down; "$0" up ;;
  status)
    "${COMPOSE[@]}" ps
    echo "-- acme (must stay up) --"
    docker ps --filter name=zhanlu-frontend --filter name=zhanlu-backend --format '{{.Names}} {{.Status}} {{.Ports}}'
    ;;
  logs)    shift; "${COMPOSE[@]}" logs -f --tail=100 "${@:-backend}" ;;
  *)       echo "usage: $0 {up|down|restart|status|logs [service]}"; exit 1 ;;
esac
