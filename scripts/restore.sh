#!/bin/bash
# ============================================================================
# Zhanlu Storage Restore
# Restore data from backup files to PostgreSQL, Redis, or MinIO.
# Usage: ./restore.sh --layer pg --file ./backups/zhanlu-pg-*.dump
#        ./restore.sh --layer redis --file ./backups/zhanlu-redis-*.rdb
#        ./restore.sh --layer minio --file mirror-YYYYMMDD/
# ============================================================================

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-zhanlu}"
POSTGRES_DB="${POSTGRES_DB:-zhanlu}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is not set}"
REDIS_PASSWORD="${REDIS_PASSWORD:?REDIS_PASSWORD is not set}"
MINIO_ALIAS="${MINIO_ALIAS:-zhanlu}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is not set}"

LAYER=""
FILE_PATH=""
FORCE=false

# ── Usage ────────────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
Usage: $0 --layer {pg|redis|minio} --file PATH [--force]

Options:
  --layer   Storage layer to restore (pg, redis, minio)
  --file    Path to backup file or mirror directory
  --force   Skip confirmation prompt (USE WITH CAUTION)

Examples:
  $0 --layer pg --file ./backups/zhanlu-pg-20260720-030000.dump
  $0 --layer redis --file ./backups/zhanlu-redis-20260720-030000.rdb
  $0 --layer minio --file mirror-20260720-030000/
EOF
    exit 1
}

# ── Argument Parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --layer) LAYER="$2"; shift 2 ;;
        --file)  FILE_PATH="$2"; shift 2 ;;
        --force) FORCE=true; shift ;;
        *) usage ;;
    esac
done

if [ -z "${LAYER}" ] || [ -z "${FILE_PATH}" ]; then
    echo "ERROR: --layer and --file are required."
    usage
fi

if [ ! -e "${FILE_PATH}" ]; then
    echo "ERROR: File not found: ${FILE_PATH}"
    exit 1
fi

# ── Confirmation Prompt ──────────────────────────────────────────────────────
log() { echo "[restore] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

log "==================================================="
log "!!! WARNING: RESTORE OPERATION !!!"
log "Target:  ${LAYER}"
log "Source:  ${FILE_PATH}"
log "This will OVERWRITE existing data in the target layer."
log "==================================================="

if [ "${FORCE}" != true ]; then
    read -p "Type 'yes' to confirm restore: " CONFIRM
    if [ "${CONFIRM}" != "yes" ]; then
        log "Restore CANCELLED."
        exit 0
    fi
fi

# ── PostgreSQL Restore ───────────────────────────────────────────────────────
restore_postgres() {
    log "Restoring PostgreSQL from ${FILE_PATH}..."

    # Stop the application to prevent concurrent writes
    log "Verifying PostgreSQL connection..."
    if ! PGPASSWORD="${POSTGRES_PASSWORD}" psql \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        -c "SELECT 1;" >/dev/null 2>&1; then
        log "ERROR: Cannot connect to PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}"
        exit 1
    fi

    # Drop and recreate (--clean --if-exists won't drop the database itself)
    log "Dropping existing objects..."
    PGPASSWORD="${POSTGRES_PASSWORD}" psql \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" 2>&1 || true

    # Restore from custom format dump
    log "Restoring from dump..."
    if PGPASSWORD="${POSTGRES_PASSWORD}" pg_restore \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        --clean --if-exists --verbose "${FILE_PATH}" 2>&1; then
        log "PostgreSQL restore COMPLETE."

        # Re-run governance SQL to install audit triggers
        log "Re-installing governance layer (audit triggers)..."
        if [ -f /app/infra/postgres/governance.sql ]; then
            PGPASSWORD="${POSTGRES_PASSWORD}" psql \
                --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
                --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
                -f /app/infra/postgres/governance.sql 2>&1
        fi
    else
        log "ERROR: PostgreSQL restore FAILED."
        exit 1
    fi
}

# ── Redis Restore ────────────────────────────────────────────────────────────
restore_redis() {
    log "Restoring Redis from ${FILE_PATH}..."

    if ! redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning PING >/dev/null 2>&1; then
        log "ERROR: Cannot connect to Redis."
        exit 1
    fi

    # Copy RDB file into Redis data directory via Docker
    if docker ps --format '{{.Names}}' | grep -q "zhanlu-redis"; then
        log "Copying RDB file to Redis container..."
        docker cp "${FILE_PATH}" "zhanlu-redis:/data/dump.rdb"
        docker restart "zhanlu-redis"

        # Wait for Redis to come back
        sleep 3
        if redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning PING >/dev/null 2>&1; then
            log "Redis restore COMPLETE."
        else
            log "ERROR: Redis did not restart successfully."
            exit 1
        fi
    else
        log "ERROR: Redis container 'zhanlu-redis' not found."
        exit 1
    fi
}

# ── MinIO Restore ────────────────────────────────────────────────────────────
restore_minio() {
    log "Restoring MinIO from ${FILE_PATH}..."

    if mc alias set "${MINIO_ALIAS}" "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" 2>/dev/null; then
        if mc mirror --overwrite "${MINIO_ALIAS}/zhanlu-backups/${FILE_PATH}/" \
            "${MINIO_ALIAS}/zhanlu-artifacts/" 2>&1; then
            log "MinIO restore COMPLETE."
        else
            log "ERROR: MinIO restore FAILED."
            exit 1
        fi
    else
        log "ERROR: Cannot connect to MinIO."
        exit 1
    fi
}

# ── Execute ──────────────────────────────────────────────────────────────────
case "${LAYER}" in
    pg)    restore_postgres ;;
    redis) restore_redis ;;
    minio) restore_minio ;;
    *)     log "ERROR: Unknown layer '${LAYER}'. Use: pg, redis, minio"; exit 1 ;;
esac

log "Restore operation complete."
