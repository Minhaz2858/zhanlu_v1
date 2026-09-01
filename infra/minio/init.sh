#!/bin/sh
# MinIO bucket bootstrap — runs once via docker compose init container.
# Creates the zhanlu-artifacts bucket if it doesn't exist.

set -e

# Wait for MinIO to be ready
echo "MinIO init: waiting for MinIO server..."
until mc alias set zhanlu http://minio:9000 "${MINIO_ROOT_USER:-minioadmin}" "${MINIO_ROOT_PASSWORD:-minioadmin}" 2>/dev/null; do
  echo "MinIO init: still waiting..."
  sleep 2
done

echo "MinIO init: connected. Creating bucket '${MINIO_BUCKET:-zhanlu-artifacts}'..."
mc mb --ignore-existing "zhanlu/${MINIO_BUCKET:-zhanlu-artifacts}"

# Set bucket policy to allow anonymous downloads (MVP dev only)
mc anonymous set download "zhanlu/${MINIO_BUCKET:-zhanlu-artifacts}" 2>/dev/null || true

echo "MinIO init: done."
