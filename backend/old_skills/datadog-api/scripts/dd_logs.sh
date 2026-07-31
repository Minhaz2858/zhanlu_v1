#!/usr/bin/env bash
# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
# search datadog logs (v2) end-to-end with curl + jq: build the flat filter/sort/page body,
# follow the meta.page.after cursor through every page, decode each event's attributes into tsv
# or jsonl, and surface the {"errors":[...]} envelope verbatim. generic to any datadog org —
# everything instance-specific comes from env vars or flags.

set -euo pipefail

usage() {
  cat <<'EOF'
usage:
  dd_logs.sh [options] "service:web status:error"   # query as a single quoted argument
  echo "service:web ..." | dd_logs.sh [options]     # query on stdin
  dd_logs.sh [options]                              # no query -> "*" (everything in the window)

options:
  --from WHEN      start of the time window (default now-15m). accepts datadog relative syntax
                   (now-1h, now-2d), a bare offset (-15m -> now-15m), iso-8601, or unix ms.
  --to WHEN        end of the time window (default now). same accepted forms as --from.
  --index NAME     restrict to a log index, repeatable (default: all indexes).
  --sort asc|desc  order by timestamp (default desc = newest first).
  --limit N        stop after N events total (default 100; 0 = fetch everything).
  --page-size N    events per request page, max 1000 (default 1000, capped to remaining).
  --json           emit one json object per event (jsonl) instead of tsv.
  -h, --help       show this help

environment:
  DD_API_KEY  org api key header; injected by the runtime, so the placeholder default is fine
  DD_APP_KEY  application key header; injected by the runtime, placeholder default is fine
  DD_SITE     regional site (default datadoghq.com; also us3./us5./datadoghq.eu/ap1./ddog-gov.com)
  DD_API      api root override (default https://api.${DD_SITE})

output:
  events on stdout — tsv with header (timestamp, status, service, host, message) by default,
  jsonl with --json. tabs/newlines inside message are escaped by @tsv. event count and any
  truncation warning go to stderr.

exit codes:
  0 success    1 request failed, api error, or bad arguments
EOF
}

err() { printf '%s\n' "$*" >&2; }

command -v curl >/dev/null || { err "curl is required"; exit 1; }
command -v jq >/dev/null || { err "jq is required"; exit 1; }

DD_SITE="${DD_SITE:-datadoghq.com}"
BASE_URL="${DD_API:-https://api.${DD_SITE}}"
API_KEY="${DD_API_KEY:-placeholder}"
APP_KEY="${DD_APP_KEY:-placeholder}"
FROM="now-15m"
TO="now"
INDEXES='[]'
SORT="desc"
LIMIT=100
PAGE_SIZE=1000
FORMAT=tsv
QUERY=""

# require_int FLAG VALUE — flags that take a count must get a non-negative integer
require_int() {
  case "$2" in
    ''|*[!0-9]*) err "$1 needs a non-negative integer, got '${2:-nothing}'"; exit 1 ;;
  esac
}

# normalize a time bound: a bare relative offset like -15m becomes now-15m; everything else
# (now, now-1h, iso-8601, unix ms) passes through untouched
norm_time() {
  case "$1" in
    -[0-9]*) printf 'now%s' "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    --from)
      if [ -z "${2:-}" ]; then err "--from needs a value"; exit 1; fi
      FROM="$(norm_time "$2")"; shift 2 ;;
    --to)
      if [ -z "${2:-}" ]; then err "--to needs a value"; exit 1; fi
      TO="$(norm_time "$2")"; shift 2 ;;
    --index)
      if [ -z "${2:-}" ]; then err "--index needs a name"; exit 1; fi
      INDEXES="$(jq -c --arg i "$2" '. + [$i]' <<<"$INDEXES")"; shift 2 ;;
    --sort)
      case "${2:-}" in
        asc|desc) SORT="$2"; shift 2 ;;
        *) err "--sort must be asc or desc, got '${2:-nothing}'"; exit 1 ;;
      esac ;;
    --limit) require_int --limit "${2:-}"; LIMIT="$2"; shift 2 ;;
    --page-size)
      require_int --page-size "${2:-}"
      if [ "$2" -lt 1 ] || [ "$2" -gt 1000 ]; then err "--page-size must be 1..1000"; exit 1; fi
      PAGE_SIZE="$2"; shift 2 ;;
    --json) FORMAT=json; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) err "unknown option: $1 (see --help)"; exit 1 ;;
    *)
      if [ -n "$QUERY" ]; then
        err "unexpected extra argument: $1 (pass the query as one quoted string)"; exit 1
      fi
      QUERY="$1"; shift ;;
  esac
done

if [ -z "$QUERY" ] && [ ! -t 0 ]; then QUERY="$(cat)"; fi
if [ -z "${QUERY//[[:space:]]/}" ]; then QUERY="*"; fi

SORT_FIELD="-timestamp"
if [ "$SORT" = "asc" ]; then SORT_FIELD="timestamp"; fi

dd_api() {
  curl -sS --max-time 60 \
    -H "DD-API-KEY: ${API_KEY}" \
    -H "DD-APPLICATION-KEY: ${APP_KEY}" \
    -H "Content-Type: application/json" \
    "$@"
}

# exit with the api's own message if the response is an error envelope (or not json at all)
dd_check_error() {
  if ! jq -e . >/dev/null 2>&1 <<<"$1"; then
    err "non-json response from the api:"
    printf '%s\n' "$1" | head -c 2000 >&2
    exit 1
  fi
  if [ "$(jq -r 'type == "object" and has("errors")' <<<"$1")" = "true" ]; then
    jq -r '.errors[0] as $e | "datadog error: " +
      (if ($e|type) == "object" then ($e.detail // $e.title // ($e|tostring))
       else ($e // "unknown error" | tostring) end)' <<<"$1" >&2
    exit 1
  fi
  if [ "$(jq -r 'type == "object" and has("data")' <<<"$1")" != "true" ]; then
    err "unexpected response (no data key):"
    printf '%s\n' "$1" | head -c 2000 >&2
    exit 1
  fi
}

# build the request body for one page; cursor is empty on the first call
build_body() {
  jq -cn \
    --arg q "$QUERY" --arg from "$FROM" --arg to "$TO" --arg sort "$SORT_FIELD" \
    --argjson idx "$INDEXES" --argjson lim "$1" --arg cur "$2" \
    '{filter: ({query: $q, from: $from, to: $to}
               + (if ($idx | length) > 0 then {indexes: $idx} else {} end)),
      sort: $sort,
      page: ({limit: $lim} + (if $cur != "" then {cursor: $cur} else {} end))}'
}

print_page() {
  if [ "$FORMAT" = "tsv" ]; then
    jq -r --argjson take "$2" \
      '[.data[]?][:$take][] | .attributes
       | [.timestamp, .status, .service, .host, .message]
       | map(if . == null then "" elif type == "string" then . else tojson end) | @tsv' <<<"$1"
  else
    jq -c --argjson take "$2" \
      '[.data[]?][:$take][] | .attributes
       | {timestamp, status, service, host, message}' <<<"$1"
  fi
}

URL="${BASE_URL}/api/v2/logs/events/search"
CURSOR=""
FETCHED=0
TRUNCATED=false
HEADER_PRINTED=false

while :; do
  WANT="$PAGE_SIZE"
  if [ "$LIMIT" -gt 0 ]; then
    REMAINING=$(( LIMIT - FETCHED ))
    if [ "$REMAINING" -le 0 ]; then break; fi
    if [ "$WANT" -gt "$REMAINING" ]; then WANT="$REMAINING"; fi
  fi
  BODY="$(build_body "$WANT" "$CURSOR")"
  PAGE="$(dd_api -X POST "$URL" -d "$BODY")"
  dd_check_error "$PAGE"

  if [ "$FORMAT" = "tsv" ] && [ "$HEADER_PRINTED" = "false" ]; then
    printf 'timestamp\tstatus\tservice\thost\tmessage\n'
    HEADER_PRINTED=true
  fi

  COUNT="$(jq -r '[.data[]?] | length' <<<"$PAGE")"
  TAKE="$COUNT"
  if [ "$LIMIT" -gt 0 ]; then
    REMAINING=$(( LIMIT - FETCHED ))
    if [ "$COUNT" -gt "$REMAINING" ]; then TAKE="$REMAINING"; fi
  fi
  if [ "$TAKE" -gt 0 ]; then print_page "$PAGE" "$TAKE"; fi
  FETCHED=$(( FETCHED + TAKE ))

  CURSOR="$(jq -r '.meta.page.after // empty' <<<"$PAGE")"
  if [ "$LIMIT" -gt 0 ] && [ "$FETCHED" -ge "$LIMIT" ]; then
    if [ -n "$CURSOR" ] || [ "$COUNT" -gt "$TAKE" ]; then TRUNCATED=true; fi
    break
  fi
  if [ -z "$CURSOR" ]; then break; fi
done

err "fetched ${FETCHED} event(s)"
if [ "$TRUNCATED" = "true" ]; then
  err "output truncated at ${FETCHED} events (raise --limit, or 0 for everything)"
fi
