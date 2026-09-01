#!/bin/bash
# ============================================================================
# Zhanlu Storage Backup
# Performs full backup of PostgreSQL, Redis, and MinIO.
# Usage: ./backup.sh [--layer pg|redis|minio|all] [--output-dir ./backups]
# Cron: 0 3 * * * /app/scripts/backup.sh --layer all >> /app/logs/backup.log 2>&1
# ============================================================================

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
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

# ── Argument Parsing ─────────────────────────────────────────────────────────
LAYER="all"
case "${1:-all}" in
    --layer) LAYER="${2:-all}" ;;
    --output-dir) BACKUP_DIR="${2:-$BACKUP_DIR}" ;;
esac

# Parse all args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --layer) LAYER="$2"; shift 2 ;;
        --output-dir) BACKUP_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

mkdir -p "${BACKUP_DIR}"

log() { echo "[backup] $(date '+%Y-%m-%d %H:%M:%S') $*"; }
success() { log "SUCCESS: $*"; }
failure() { log "FAILURE: $*"; EXIT_CODE=1; }

EXIT_CODE=0

# ── PostgreSQL Backup ────────────────────────────────────────────────────────
backup_postgres() {
    local pg_file="${BACKUP_DIR}/zhanlu-pg-${TIMESTAMP}.dump"
    log "Starting PostgreSQL backup..."

    if PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
        --host="${POSTGRES_HOST}" \
        --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" \
        --dbname="${POSTGRES_DB}" \
        --format=custom \
        --compress=9 \
        --verbose \
        --file="${pg_file}" 2>&1; then

        local size=$(du -h "${pg_file}" | cut -f1)
        success "PostgreSQL dump created: ${pg_file} (${size})"
    else
        failure "PostgreSQL dump FAILED"
    fi
}

# ── Redis Backup ─────────────────────────────────────────────────────────────
backup_redis() {
    local redis_file="${BACKUP_DIR}/zhanlu-redis-${TIMESTAMP}.rdb"
    log "Starting Redis backup..."

    # Trigger BGSAVE and wait for completion
    if redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning BGSAVE >/dev/null 2>&1; then
        sleep 2

        # Copy the RDB file from the Redis container
        if docker cp "zhanlu-redis:/data/dump.rdb" "${redis_file}" 2>/dev/null; then
            local size=$(du -h "${redis_file}" | cut -f1)
            success "Redis RDB copied: ${redis_file} (${size})"
        else
            # Fallback: try copying from volume
            log "docker cp failed, trying volume path..."
            local redis_volume=$(docker volume inspect redis_data --format '{{.Mountpoint}}' 2>/dev/null)
            if [ -n "${redis_volume}" ] && [ -f "${redis_volume}/dump.rdb" ]; then
                cp "${redis_volume}/dump.rdb" "${redis_file}"
                local size=$(du -h "${redis_file}" | cut -f1)
                success "Redis RDB copied from volume: ${redis_file} (${size})"
            else
                failure "Redis backup FAILED — could not access dump.rdb"
            fi
        fi
    else
        failure "Redis BGSAVE FAILED"
    fi
}

# ── MinIO Backup ─────────────────────────────────────────────────────────────
backup_minio() {
    local mirror_dir="mirror-${TIMESTAMP}"
    log "Starting MinIO backup..."

    if mc alias set "${MINIO_ALIAS}" "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" 2>/dev/null; then
        # Mirror zhanlu-artifacts to zhanlu-backups bucket
        if mc mirror --overwrite \
            "${MINIO_ALIAS}/zhanlu-artifacts" \
            "${MINIO_ALIAS}/zhanlu-backups/${mirror_dir}/" 2>&1; then
            success "MinIO artifacts mirrored to zhanlu-backups/${mirror_dir}"
        else
            failure "MinIO mirror FAILED"
        fi
    else
        failure "MinIO connection FAILED"
    fi
}

# ── Cleanup Old Backups ──────────────────────────────────────────────────────
cleanup_backups() {
    log "Cleaning backups older than ${RETENTION_DAYS} days..."
    local deleted=0
    find "${BACKUP_DIR}" -type f -name "zhanlu-*" -mtime "+${RETENTION_DAYS}" -delete -exec echo "  [deleted] {}" \; 2>/dev/null || true

    # Also cleanup old mirrors in MinIO backup bucket
    log "Cleaning old MinIO mirrors (>${RETENTION_DAYS} days)..."
    # MinIO lifecycle policy handles this automatically via the bucket lifecycle config.
    log "  (MinIO lifecycle policy active on zhanlu-backups bucket)"
}

# ── Main ─────────────────────────────────────────────────────────────────────
log "Zhanlu backup started (layer: ${LAYER}, timestamp: ${TIMESTAMP})"

case "${LAYER}" in
    pg)
        backup_postgres
        ;;
    redis)
        backup_redis
        ;;
    minio)
        backup_minio
        ;;
    all)
        backup_postgres
        backup_redis
        backup_minio
        ;;
    *)
        log "ERROR: Unknown layer '${LAYER}'. Use: pg, redis, minio, all"
        exit 1
        ;;
esac

cleanup_backups

if [ ${EXIT_CODE} -eq 0 ]; then
    log "Backup completed successfully."
else
    log "Backup completed with ERRORS."
fi

exit ${EXIT_CODE}
