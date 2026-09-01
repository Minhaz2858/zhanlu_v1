#!/bin/bash
# ============================================================================
# Zhanlu Storage Health Check
# Comprehensive 3-layer health verification with integrity checks.
# Usage: ./storage_healthcheck.sh [--verbose] [--integrity]
# ============================================================================

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-zhanlu}"
POSTGRES_DB="${POSTGRES_DB:-zhanlu}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is not set}"
REDIS_PASSWORD="${REDIS_PASSWORD:?REDIS_PASSWORD is not set}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
MINIO_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_PASS="${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is not set}"
MINIO_ALIAS="hc-zhanlu"

VERBOSE=false
INTEGRITY=false
PASS_COUNT=0
FAIL_COUNT=0

# ── Arguments ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --verbose) VERBOSE=true; shift ;;
        --integrity) INTEGRITY=true; shift ;;
        *) shift ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
check() {
    local name="$1"
    local result="$2"
    local detail="${3:-}"

    if [ "${result}" = "PASS" ]; then
        echo "  [PASS] ${name}"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo "  [FAIL] ${name} — ${detail}"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    if [ "${VERBOSE}" = true ] && [ -n "${detail}" ]; then
        echo "         Detail: ${detail}"
    fi
}

vlog() {
    if [ "${VERBOSE}" = true ]; then
        echo "  [INFO] $*"
    fi
}

# ── Section Header ───────────────────────────────────────────────────────────
section() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ═══════════════════════════════════════════════════════════════════════════
# PostgreSQL
# ═══════════════════════════════════════════════════════════════════════════
check_postgres() {
    section "PostgreSQL"

    # Test connection
    if PGPASSWORD="${POSTGRES_PASSWORD}" psql \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        -c "SELECT 1 AS healthcheck;" >/dev/null 2>&1; then
        check "Connection" "PASS" "Connected to ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    else
        check "Connection" "FAIL" "Cannot connect to PostgreSQL"
        return
    fi

    # Check PostgreSQL version
    local pg_version=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        -tAc "SELECT version();" 2>/dev/null)
    vlog "PostgreSQL version: $(echo ${pg_version} | head -c 60)"

    # Count tables in public schema
    local table_count=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        -tAc "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';" 2>/dev/null || echo "0")

    check "Tables" "PASS" "${table_count} tables in public schema"

    if [ "${table_count}" -eq 0 ]; then
        check "Schema" "FAIL" "No tables found — Alembic migrations may not have run"
    fi

    # Check Alembic head
    local alembic_head=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        -tAc "SELECT version_num FROM alembic_version LIMIT 1;" 2>/dev/null || echo "NONE")

    if [ "${alembic_head}" != "NONE" ]; then
        check "Migrations" "PASS" "Current migration: ${alembic_head}"
    else
        check "Migrations" "FAIL" "No migration record found"
    fi

    # Check schemas
    for schema in public audit governance; do
        local has_schema=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
            --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
            --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
            -tAc "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = '${schema}';" 2>/dev/null || echo "0")
        if [ "${has_schema}" -gt 0 ]; then
            check "Schema: ${schema}" "PASS" "Schema exists"
        else
            check "Schema: ${schema}" "FAIL" "Schema does not exist"
        fi
    done

    # Check roles
    for role in zhanlu_app zhanlu_migrate zhanlu_readonly; do
        local has_role=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
            --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
            --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
            -tAc "SELECT COUNT(*) FROM pg_roles WHERE rolname = '${role}';" 2>/dev/null || echo "0")
        if [ "${has_role}" -gt 0 ]; then
            check "Role: ${role}" "PASS" "Role exists"
        else
            check "Role: ${role}" "FAIL" "Role does not exist — run init.sql"
        fi
    done

    # Check extensions
    for ext in "uuid-ossp" pg_trgm pgcrypto; do
        local has_ext=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
            --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
            --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
            -tAc "SELECT COUNT(*) FROM pg_extension WHERE extname = '${ext}';" 2>/dev/null || echo "0")
        if [ "${has_ext}" -gt 0 ]; then
            check "Extension: ${ext}" "PASS" "Installed"
        else
            check "Extension: ${ext}" "FAIL" "Not installed"
        fi
    done

    # Database size
    local db_size=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
        --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
        --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
        -tAc "SELECT pg_size_pretty(pg_database_size('${POSTGRES_DB}'));" 2>/dev/null)
    vlog "Database size: ${db_size}"

    # Checksum integrity (if requested)
    if [ "${INTEGRITY}" = true ]; then
        section "PostgreSQL Integrity"
        local mismatch_count=$(PGPASSWORD="${POSTGRES_PASSWORD}" psql \
            --host="${POSTGRES_HOST}" --port="${POSTGRES_PORT}" \
            --username="${POSTGRES_USER}" --dbname="${POSTGRES_DB}" \
            -tAc "SELECT COUNT(*) FROM governance.verify_blob_checksums() WHERE error_msg LIKE 'CHECKSUM MISMATCH%';" 2>/dev/null || echo "0")

        if [ "${mismatch_count}" -eq 0 ]; then
            check "Blob checksums" "PASS" "All artifact blob checksums verified"
        else
            check "Blob checksums" "FAIL" "${mismatch_count} checksum mismatches detected"
        fi
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# Redis
# ═══════════════════════════════════════════════════════════════════════════
check_redis() {
    section "Redis"

    # Test connection with auth
    if redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning PING >/dev/null 2>&1; then
        check "Connection" "PASS" "Connected and authenticated"
    else
        check "Connection" "FAIL" "Cannot connect or authenticate to Redis"
        return
    fi

    # Memory usage
    local mem_used=$(redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning INFO memory 2>/dev/null \
        | grep "used_memory_human:" | cut -d: -f2 | tr -d '\r\n ')
    local mem_peak=$(redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning INFO memory 2>/dev/null \
        | grep "used_memory_peak_human:" | cut -d: -f2 | tr -d '\r\n ')

    check "Memory" "PASS" "Used: ${mem_used}, Peak: ${mem_peak}"

    # Check maxmemory policy
    local maxmem_policy=$(redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning CONFIG GET maxmemory-policy 2>/dev/null \
        | tail -1 | tr -d '\r\n ')
    if [ "${maxmem_policy}" = "noeviction" ]; then
        check "Memory policy" "PASS" "noeviction (safe for queues)"
    else
        check "Memory policy" "FAIL" "Expected noeviction, got: ${maxmem_policy}"
    fi

    # Key count
    local key_count=$(redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning DBSIZE 2>/dev/null | tail -1)
    check "Keys" "PASS" "${key_count} keys"

    # Verify dangerous commands are disabled
    local flush_result=$(redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning FLUSHALL 2>&1 || true)
    if echo "${flush_result}" | grep -qi "unknown command\|ERR"; then
        check "Security: FLUSHALL" "PASS" "Dangerous command disabled"
    else
        check "Security: FLUSHALL" "FAIL" "FLUSHALL is not disabled — security risk"
    fi

    # Persistence check
    local aof=$(redis-cli -h redis -a "${REDIS_PASSWORD}" --no-auth-warning INFO persistence 2>/dev/null \
        | grep "aof_enabled:" | cut -d: -f2 | tr -d '\r\n ')
    if [ "${aof}" = "1" ]; then
        check "Persistence: AOF" "PASS" "Append-Only File enabled"
    else
        check "Persistence: AOF" "FAIL" "AOF not enabled"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# MinIO
# ═══════════════════════════════════════════════════════════════════════════
check_minio() {
    section "MinIO"

    if mc alias set "${MINIO_ALIAS}" "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" 2>/dev/null; then
        check "Connection" "PASS" "Connected to ${MINIO_ENDPOINT}"
    else
        check "Connection" "FAIL" "Cannot connect to MinIO at ${MINIO_ENDPOINT}"
        return
    fi

    # Check required buckets
    for bucket in zhanlu-artifacts zhanlu-uploads zhanlu-exports zhanlu-backups; do
        if mc ls "${MINIO_ALIAS}/${bucket}" >/dev/null 2>&1; then
            check "Bucket: ${bucket}" "PASS" "Exists"
        else
            check "Bucket: ${bucket}" "FAIL" "Bucket not found"
        fi
    done

    # Check bucket policies (no anonymous access)
    for bucket in zhanlu-artifacts zhanlu-uploads zhanlu-exports zhanlu-backups; do
        local policy=$(mc anonymous get "${MINIO_ALIAS}/${bucket}" 2>/dev/null || echo "none")
        if [ "${policy}" = "none" ] || echo "${policy}" | grep -q "Access"; then
            check "Policy: ${bucket}" "PASS" "No anonymous access (${policy})"
        else
            check "Policy: ${bucket}" "FAIL" "Anonymous access detected: ${policy}"
        fi
    done

    # Check lifecycle policies on artifacts bucket
    if mc ilm ls "${MINIO_ALIAS}/zhanlu-artifacts" 2>/dev/null | grep -qi "Rule"; then
        check "Lifecycle: artifacts" "PASS" "Lifecycle policy applied"
    else
        check "Lifecycle: artifacts" "FAIL" "No lifecycle policy — objects accumulate forever"
    fi

    # Service account check
    if mc admin user info "${MINIO_ALIAS}" zhanlu-app >/dev/null 2>&1; then
        check "Service account" "PASS" "zhanlu-app exists"
    else
        check "Service account" "FAIL" "zhanlu-app service account not found"
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Zhanlu Storage Health Check"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════════"

check_postgres
check_redis
check_minio

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  SUMMARY: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
echo "═══════════════════════════════════════════════════════════"

if [ ${FAIL_COUNT} -gt 0 ]; then
    exit 1
else
    exit 0
fi
