# Zhanlu™ Layer 7 — Docker Compose Implementation Guide

**Version:** 1.0  
**Purpose:** Practical implementation companion for Layer 7 infrastructure  
**Stack:** Docker Compose + PostgreSQL + Redis + Nginx  

---

## 1. Goal

This file provides a practical starting point for implementing the Zhanlu™ Docker-first infrastructure.

The architecture rule is:

```text
PostgreSQL = source of truth
Redis = temporary infrastructure
Docker container filesystem = not permanent business storage
Sandbox filesystem = temporary only
```

---

## 2. Recommended Service List

Minimum v1 services:

```text
nginx
frontend
backend
synexia
worker
agent_skill_worker
sandbox_worker
postgres
redis
```

Optional services:

```text
artifact_worker
otel_collector
prometheus
grafana
loki
minio
```

---

## 3. Folder Layout

```text
deploy/
  docker/
    docker-compose.yml
    docker-compose.dev.yml
    docker-compose.prod.yml
  nginx/
    nginx.conf
  postgres/
    init/
      001_extensions.sql
    backup/
      backup.sh
      restore.sh
  redis/
    redis.conf
  sandbox/
    Dockerfile.skill-sandbox
    Dockerfile.artifact-sandbox
scripts/
  dev_up.sh
  dev_down.sh
  migrate.sh
  backup_postgres.sh
  restore_postgres.sh
.env.example
```

---

## 4. PostgreSQL Init File

`deploy/postgres/init/001_extensions.sql`

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
```

---

## 5. Environment Example

`.env.example`

```env
POSTGRES_PASSWORD=change_me
DATABASE_URL=postgresql+asyncpg://zhanlu:change_me@postgres:5432/zhanlu
REDIS_URL=redis://redis:6379/0
JWT_SECRET=change_me
SYNEXIA_URL=http://synexia:8643
MODEL_PROVIDER=minimax
MODEL_API_KEY=replace_with_secure_secret
MODEL_BASE_URL=
SANDBOX_NETWORK_DEFAULT=deny
SANDBOX_MAX_SECONDS=300
SANDBOX_MAX_MEMORY_MB=1024
ARTIFACT_STORAGE_MODE=postgres
ENABLE_MINIO=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

Do not commit real `.env` files.

---

## 6. Minimal Compose Skeleton

```yaml
version: "3.9"

services:
  nginx:
    image: nginx:1.27
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ../nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - frontend
      - backend
    networks:
      - zhanlu-net

  frontend:
    build: ../../frontend
    environment:
      - VITE_API_BASE_URL=/api
    networks:
      - zhanlu-net

  backend:
    build: ../../backend
    env_file:
      - ../../.env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  synexia:
    build: ../../synexia
    env_file:
      - ../../.env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  worker:
    build:
      context: ../../backend
      dockerfile: Dockerfile.worker
    env_file:
      - ../../.env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  agent_skill_worker:
    build:
      context: ../../backend
      dockerfile: Dockerfile.agent-worker
    env_file:
      - ../../.env
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  sandbox_worker:
    build: ../../sandbox_worker
    env_file:
      - ../../.env
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      - POSTGRES_DB=zhanlu
      - POSTGRES_USER=zhanlu
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
    volumes:
      - zhanlu-postgres-data:/var/lib/postgresql/data
      - ../postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U zhanlu -d zhanlu"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - zhanlu-net

  redis:
    image: redis:7
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - zhanlu-redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - zhanlu-net

volumes:
  zhanlu-postgres-data:
  zhanlu-redis-data:

networks:
  zhanlu-net:
    driver: bridge
```

---

## 7. Important Warning About Docker Socket

Mounting Docker socket into `sandbox_worker` is risky:

```yaml
- /var/run/docker.sock:/var/run/docker.sock
```

This should be used only for MVP or trusted deployments.

Safer future options:

```text
separate sandbox host
restricted Docker API proxy
rootless Docker
gVisor / Kata runtime
Firecracker microVM workers
```

---

## 8. Backup Script Example

`deploy/postgres/backup/backup.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=${BACKUP_DIR:-./backups}
mkdir -p "$BACKUP_DIR"

docker exec zhanlu-postgres pg_dump -U zhanlu zhanlu \
  | gzip > "$BACKUP_DIR/zhanlu_${TIMESTAMP}.sql.gz"

echo "Backup created: $BACKUP_DIR/zhanlu_${TIMESTAMP}.sql.gz"
```

---

## 9. Restore Script Example

`deploy/postgres/backup/restore.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: restore.sh <backup.sql.gz>"
  exit 1
fi

BACKUP_FILE="$1"

gunzip -c "$BACKUP_FILE" | docker exec -i zhanlu-postgres psql -U zhanlu -d zhanlu

echo "Restore completed from: $BACKUP_FILE"
```

---

## 10. Health Check Checklist

- [ ] `nginx` responds on `/healthz`
- [ ] `backend` responds on `/healthz` and `/readyz`
- [ ] `synexia` responds on `/healthz`
- [ ] `postgres` passes `pg_isready`
- [ ] `redis` responds to `PING`
- [ ] `worker` heartbeat is visible
- [ ] `sandbox_worker` can start a test sandbox job

---

## 11. Production Checklist

- [ ] Real secrets are not committed to Git
- [ ] PostgreSQL volume is backed up
- [ ] Restore test was performed
- [ ] Redis is not publicly exposed
- [ ] PostgreSQL is not publicly exposed
- [ ] Only Nginx exposes external ports
- [ ] TLS is configured
- [ ] Sandbox worker has resource limits
- [ ] Sandbox network is deny-by-default
- [ ] Logs include `trace_id`
- [ ] All business data is stored in PostgreSQL or governed artifact storage

---

## 12. Final Rule

If a file or record matters to the business, it must be represented in PostgreSQL.

Docker folders are runtime implementation details, not enterprise memory.
