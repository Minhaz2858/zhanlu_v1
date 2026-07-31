# Zhanlu SSH Server Deployment & Docker Sandbox Guide

**Purpose:** Explain how Zhanlu works when deployed on an SSH server using Docker, PostgreSQL, Redis, Nginx, workers, and temporary sandbox containers.

## 1. Core idea

An SSH server is the remote machine where Zhanlu runs. Users do not use SSH. Users open Zhanlu in a browser through HTTPS.

```text
User Browser
  ↓ HTTPS
Nginx / Reverse Proxy
  ↓
Frontend + Backend API
  ↓
Synexia + Workers
  ↓
PostgreSQL + Redis
  ↓
Sandbox Worker creates temporary Docker sandbox containers
```

SSH is only for deployment and maintenance.

## 2. Services on the SSH server

Recommended Docker Compose services:

```text
zhanlu-nginx
zhanlu-frontend        internal UI service
zhanlu-backend         FastAPI API service
zhanlu-synexia         cognitive orchestration service
zhanlu-worker          workflow/execution worker
zhanlu-sandbox-worker  sandbox container manager
zhanlu-postgres        source of truth database
zhanlu-redis           temporary queue/cache/lock service
```

Only Nginx should be exposed publicly.

```text
Public internet:
  80 / 443 only

Internal Docker network:
  backend, frontend, synexia, postgres, redis, workers
```

## 3. How the sandbox works

The sandbox is not a permanent folder. It is a temporary Docker container created by the sandbox-worker.

When Zhanlu needs to generate PPT, DOCX, MD, HTML, dashboard, mini app, chart, or preview:

```text
Backend creates execution
↓
Redis queue receives sandbox job
↓
sandbox-worker picks job
↓
sandbox-worker creates temporary Docker container
↓
skill runs inside temporary container
↓
output returns to backend
↓
backend stores output in PostgreSQL
↓
temporary sandbox container is destroyed
```

Important rule:

```text
Sandbox filesystem is temporary.
PostgreSQL is the source of truth.
```

## 4. Example: user asks for PPT

```text
1. User asks: Make a Q2 finance PPT.
2. Nginx sends request to backend.
3. Backend stores user message, RequestEnvelope, and execution record.
4. Synexia creates PlanDAG.
5. Worker starts execution.
6. sandbox-worker creates temporary PPT sandbox container.
7. Sandbox receives approved skill package, input JSON, template, DataSnapshot, and chart images.
8. Sandbox generates PPTX, PDF preview, slide thumbnails, and validation report.
9. sandbox-worker sends outputs back to backend.
10. Backend stores PPTX, preview PDF, thumbnails, artifact metadata, build manifest, and validation report.
11. Chat receives artifact.preview_ready.
12. Frontend shows inline PPT preview in chat.
```

No permanent PPT file should remain inside sandbox folders.

## 5. Basic sandbox container command

The sandbox-worker can create a temporary job container similar to:

```bash
docker run --rm \
  --name zhanlu-sandbox-job-123 \
  --network none \
  --memory 1g \
  --cpus 1 \
  --read-only \
  --pids-limit 128 \
  --security-opt no-new-privileges \
  -v /tmp/zhanlu_sandbox/job_123/input:/workspace/input:ro \
  -v /tmp/zhanlu_sandbox/job_123/output:/workspace/output:rw \
  zhanlu-sandbox-python:latest
```

Meaning:

```text
--rm                       delete container after run
--network none             no internet access by default
--memory 1g                memory limit
--cpus 1                   CPU limit
--read-only                container root filesystem read-only
--pids-limit 128           process limit
--security-opt             prevent privilege escalation
input mounted read-only
output mounted writable
```

## 6. Security model

Do not let the main backend create sandbox containers directly.

Better design:

```text
backend              cannot access Docker socket
worker               cannot access Docker socket
sandbox-worker       only service allowed to create sandbox containers
```

For MVP, sandbox-worker may mount:

```text
/var/run/docker.sock
```

But this is powerful and risky. Isolate it carefully.

MVP rule:

```text
Only sandbox-worker has Docker socket access.
No user code runs in backend container.
No agent, skill, or MCP server receives credentials.
```

Better production options:

```text
rootless Docker
Docker socket proxy
separate sandbox server
gVisor/Kata later
Firecracker later
```

## 7. Storage model

Correct storage:

```text
PostgreSQL:
  conversations
  messages
  executions
  agents
  skills
  memory
  artifacts
  PPTX/DOCX/PDF/MD/HTML blobs
  previews
  thumbnails
  audit logs

Redis:
  queues
  locks
  worker heartbeat
  temporary cache

Sandbox temp folder:
  input/output during one job only
  deleted after job

Docker volume:
  physical storage for PostgreSQL and Redis
```

Do not permanently store user business files in:

```text
backend/uploads/
server/files/
sandbox/tmp/
container filesystem
```

## 8. Inline preview on the SSH server

Inline preview flow:

```text
PPTX generated in sandbox
↓
PPTX converted to PDF preview and thumbnails
↓
preview stored in PostgreSQL
↓
chat receives artifact.preview_ready event
↓
frontend renders ArtifactPreviewCard
↓
frontend calls /api/v1/artifacts/{artifact_id}/preview
↓
backend checks permission
↓
backend streams PDF or thumbnails
↓
user sees preview inside chat
```

Preview methods:

```text
PPTX → PDF preview + slide thumbnails
DOCX → PDF preview
MD → rendered HTML preview
HTML → sandboxed iframe preview
Dashboard → iframe/card preview
Mini app → sandboxed iframe preview
```

## 9. Docker network design

Recommended Docker networks:

```text
zhanlu_app_net
  nginx
  frontend
  backend
  synexia
  worker

zhanlu_data_net
  backend
  postgres
  redis
  worker
  sandbox-worker

zhanlu_sandbox_net
  sandbox-worker
  temporary sandbox containers only when needed
```

For most sandbox jobs:

```text
network = none
```

If a skill needs network, use allowlist, approval, and audit.

Default rule:

```text
Sandbox network disabled.
```

## 10. Minimal Docker Compose pattern

```yaml
services:
  backend:
    image: zhanlu-backend
    env_file: .env
    depends_on:
      - postgres
      - redis
      - synexia
    networks:
      - app_net
      - data_net

  synexia:
    image: zhanlu-synexia
    env_file: .env
    networks:
      - app_net
      - data_net

  worker:
    image: zhanlu-worker
    env_file: .env
    depends_on:
      - postgres
      - redis
    networks:
      - data_net

  sandbox-worker:
    image: zhanlu-sandbox-worker
    env_file: .env
    depends_on:
      - postgres
      - redis
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - sandbox_tmp:/tmp/zhanlu_sandbox
    networks:
      - data_net

  postgres:
    image: postgres:16
    volumes:
      - postgres_data:/var/lib/postgresql/data
    networks:
      - data_net

  redis:
    image: redis:7
    volumes:
      - redis_data:/data
    networks:
      - data_net

  nginx:
    image: nginx:latest
    ports:
      - "80:80"
      - "443:443"
    depends_on:
      - backend
      - frontend
    networks:
      - app_net

volumes:
  postgres_data:
  redis_data:
  sandbox_tmp:

networks:
  app_net:
  data_net:
  sandbox_net:
```

## 11. Recovery after server restart

Because PostgreSQL is the source of truth:

```text
messages are safe
executions are safe
artifacts are safe
skills are safe
memory is safe
audit logs are safe
```

On startup:

```text
worker checks PostgreSQL for running jobs
sandbox-worker checks PostgreSQL for running sandbox jobs
stuck jobs become retryable or failed
temporary folders are cleaned
containers with old job labels are removed
```

Redis may lose temporary queue state, but Zhanlu must not lose execution truth.

## 12. Server operation checklist

```text
Install Docker and Docker Compose.
Expose only Nginx ports 80/443.
Use HTTPS certificate.
Keep PostgreSQL and Redis internal.
Backup PostgreSQL regularly.
Use .env for non-secret config.
Use encrypted secret storage for real credentials.
Restrict Docker socket to sandbox-worker only.
Run sandbox containers with network disabled by default.
Clean old sandbox temp folders.
Monitor disk usage, memory, CPU, and queue backlog.
```

## 13. Final rule

When Zhanlu runs on an SSH server, Docker Compose runs all core services: frontend, backend, Synexia, workers, PostgreSQL, Redis, Nginx, and sandbox-worker. Users access the system through HTTPS, not SSH. The sandbox-worker is the only service allowed to create temporary Docker sandbox containers. Skills and artifact builders run inside these temporary containers with CPU, memory, time, filesystem, and network limits. Outputs are validated and stored back into PostgreSQL as versioned artifacts. Redis is used only for queues and temporary coordination. Backend folders and sandbox folders are never permanent storage.
