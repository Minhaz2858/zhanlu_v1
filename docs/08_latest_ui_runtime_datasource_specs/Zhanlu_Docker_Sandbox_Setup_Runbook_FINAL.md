# Zhanlu Docker Sandbox Setup Runbook

**Version:** FINAL v1.0  
**Target reader:** Claude Code / implementation agent / backend engineer  
**Purpose:** Provide exact implementation guidance for the Docker-based sandbox runtime used by Zhanlu agents, skills, artifact generation, live execution timeline, and inline chat previews.

---

## 0. Non-negotiable architecture rules

These rules must not be changed during implementation.

```text
1. PostgreSQL is the source of truth.
2. Redis is temporary queue/cache/lock/event infrastructure only.
3. Backend, Synexia, and normal workers must not run user code.
4. Backend, Synexia, and normal workers must not access Docker socket.
5. Only sandbox-worker may create temporary sandbox containers.
6. Sandbox containers are temporary and destroyed after each job.
7. Sandbox filesystem is temporary and never authoritative.
8. Sandbox receives approved input packages, not raw credentials.
9. Sandbox does not directly access enterprise databases in v1.
10. Sandbox network is disabled by default.
11. Generated outputs are validated before becoming trusted artifacts.
12. Generated outputs are stored back through Artifact Service into PostgreSQL or governed object storage.
13. Chat preview uses permission-checked APIs, never raw server paths.
```

The sandbox is designed to give Zhanlu a Claude-Code-like experience inside chat, while remaining enterprise-safe.

---

## 1. What this sandbox is for

The sandbox is used when an agent needs real execution, file generation, conversion, code execution, or validation.

Use sandbox for:

```text
PPTX generation
DOCX generation
PDF generation
Markdown file generation
HTML page generation
Dashboard generation
Mini app generation
Chart generation
File conversion
Preview generation
Artifact validation
Custom skill execution
Code-based skill execution
```

Do not use sandbox for simple text-only chat replies.

```text
Simple Q&A → Synexia answers through model route.
Artifact/data/code task → Layer 5 creates sandbox job.
```

---

## 2. High-level runtime flow

```text
User message in chat
↓
Backend stores message + RequestEnvelope
↓
Synexia creates TaskSpec + PlanDAG
↓
Layer 5 creates execution + node runs
↓
Skill node requires sandbox
↓
Backend/worker creates sandbox_jobs row in PostgreSQL
↓
Redis queue receives sandbox_job_id
↓
sandbox-worker picks sandbox_job_id
↓
sandbox-worker materializes approved input package
↓
sandbox-worker starts temporary Docker container
↓
container runs skill command
↓
sandbox-worker streams logs/events to PostgreSQL + Redis/SSE/WebSocket
↓
sandbox-worker collects output files
↓
output validation runs
↓
Artifact Service stores files, previews, manifests, validation reports
↓
artifact.preview_ready event emitted
↓
frontend shows inline artifact preview card
↓
sandbox temp folder and container are removed
```

---

## 3. Required repository files

Claude Code must create or verify these files.

```text
infra/docker/
  backend.Dockerfile
  frontend.Dockerfile
  worker.Dockerfile
  sandbox-worker.Dockerfile
  sandbox-python.Dockerfile
  sandbox-node.Dockerfile
  sandbox-office.Dockerfile
  sandbox-webapp.Dockerfile
  nginx.conf

backend/sandbox/
  __init__.py
  docker_runner.py
  sandbox_policy.py
  input_materializer.py
  output_collector.py
  validation_runner.py
  cleanup.py

backend/workers/
  sandbox_worker.py
  worker_main.py

backend/execution/
  sandbox_jobs.py
  execution_events.py

backend/artifacts/
  storage.py
  preview.py
  validation.py
  build_manifest.py

backend/models/
  sandbox.py
  artifacts.py
  executions.py

docker-compose.yml
.env.example
```

Optional later:

```text
infra/docker/sandbox-dashboard.Dockerfile
infra/docker/sandbox-mini-app.Dockerfile
infra/docker/docker-socket-proxy.yml
```

---

## 4. Docker Compose service topology

Only `nginx` is public. All other services are internal.

```text
Public internet
  ↓
nginx : 80/443
  ↓
frontend + backend
  ↓
synexia + worker + sandbox-worker
  ↓
postgres + redis
```

Service responsibilities:

```text
zhanlu-frontend:
  React UI, chat, artifact cards, live execution timeline.

zhanlu-backend:
  FastAPI API, auth, conversations, artifacts, preview APIs, execution records.

zhanlu-synexia:
  Cognitive controller, TaskSpec, PlanDAG, model routing.

zhanlu-worker:
  Normal workflow node executor. No Docker socket.

zhanlu-sandbox-worker:
  Only service allowed to create temporary Docker sandbox containers.

zhanlu-postgres:
  Source of truth.

zhanlu-redis:
  Queue, locks, temporary event fanout, worker heartbeat.

zhanlu-nginx:
  HTTPS reverse proxy.
```

---

## 5. Minimal Docker Compose skeleton

Claude Code should implement a Compose file following this structure.

```yaml
services:
  nginx:
    image: nginx:1.27-alpine
    container_name: zhanlu-nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./infra/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - frontend
      - backend
    networks:
      - app_net

  frontend:
    build:
      context: .
      dockerfile: infra/docker/frontend.Dockerfile
    container_name: zhanlu-frontend
    env_file: .env
    networks:
      - app_net

  backend:
    build:
      context: .
      dockerfile: infra/docker/backend.Dockerfile
    container_name: zhanlu-backend
    env_file: .env
    depends_on:
      - postgres
      - redis
      - synexia
    networks:
      - app_net
      - data_net
    # Important: backend must NOT mount /var/run/docker.sock

  synexia:
    build:
      context: .
      dockerfile: infra/docker/backend.Dockerfile
    container_name: zhanlu-synexia
    command: python -m backend.synexia.service_main
    env_file: .env
    depends_on:
      - postgres
      - redis
    networks:
      - data_net

  worker:
    build:
      context: .
      dockerfile: infra/docker/worker.Dockerfile
    container_name: zhanlu-worker
    command: python -m backend.workers.worker_main
    env_file: .env
    depends_on:
      - postgres
      - redis
    networks:
      - data_net
    # Important: normal worker must NOT mount /var/run/docker.sock

  sandbox-worker:
    build:
      context: .
      dockerfile: infra/docker/sandbox-worker.Dockerfile
    container_name: zhanlu-sandbox-worker
    command: python -m backend.workers.sandbox_worker
    env_file: .env
    depends_on:
      - postgres
      - redis
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - sandbox_tmp:/tmp/zhanlu_sandbox
    networks:
      - data_net
    # Only this service may access Docker socket in MVP.

  postgres:
    image: postgres:16-alpine
    container_name: zhanlu-postgres
    env_file: .env
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./infra/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    networks:
      - data_net

  redis:
    image: redis:7-alpine
    container_name: zhanlu-redis
    command: redis-server /usr/local/etc/redis/redis.conf
    volumes:
      - redis_data:/data
      - ./infra/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
    networks:
      - data_net

volumes:
  postgres_data:
  redis_data:
  sandbox_tmp:

networks:
  app_net:
  data_net:
```

Security note: mounting `/var/run/docker.sock` gives the sandbox-worker powerful control over Docker. This is acceptable only for MVP if the sandbox-worker is isolated, minimal, audited, and not exposed to users. For stronger production, use rootless Docker, Docker socket proxy, a separate sandbox host, gVisor, Kata Containers, Firecracker, E2B, or Daytona-style managed sandboxes.

---

## 6. Environment variables

Add these to `.env.example`.

```env
# Core ports
BACKEND_PORT=5002
FRONTEND_PORT=5152
SYNEXIA_PORT=8643

# Database
POSTGRES_DB=zhanlu
POSTGRES_USER=zhanlu
POSTGRES_PASSWORD=change_me
DATABASE_URL=postgresql+asyncpg://zhanlu:change_me@postgres:5432/zhanlu

# Redis
REDIS_URL=redis://redis:6379/0

# Auth
JWT_SECRET=change_me_long_random_secret
JWT_EXPIRE_MINUTES=10080

# Sandbox
SANDBOX_ENABLED=true
SANDBOX_TMP_ROOT=/tmp/zhanlu_sandbox
SANDBOX_DEFAULT_NETWORK=none
SANDBOX_DEFAULT_TIMEOUT_SECONDS=120
SANDBOX_DEFAULT_MEMORY_MB=1024
SANDBOX_DEFAULT_CPUS=1
SANDBOX_DEFAULT_PIDS_LIMIT=128
SANDBOX_MAX_OUTPUT_MB=100
SANDBOX_CLEANUP_ALWAYS=true
SANDBOX_ALLOW_DOCKER_SOCKET_ONLY_IN_WORKER=true

# Sandbox images
SANDBOX_IMAGE_PYTHON=zhanlu-sandbox-python:latest
SANDBOX_IMAGE_NODE=zhanlu-sandbox-node:latest
SANDBOX_IMAGE_OFFICE=zhanlu-sandbox-office:latest
SANDBOX_IMAGE_WEBAPP=zhanlu-sandbox-webapp:latest

# Artifact storage
ARTIFACT_STORAGE_BACKEND=postgres_bytea
MAX_ARTIFACT_MB=100
PREVIEW_GENERATION_ENABLED=true

# Model provider keys, routed only through Synexia
OPENAI_API_KEY=
MODEL_PROVIDER_KEY=
```

---

## 7. Sandbox images

Create prebuilt sandbox images. Do not install random dependencies inside every job by default.

### 7.1 `sandbox-python.Dockerfile`

For data processing, Markdown, chart, PPTX, DOCX, XLSX generation.

```dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu \
    fontconfig \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 sandbox

WORKDIR /workspace

RUN pip install --no-cache-dir \
    pandas \
    numpy \
    matplotlib \
    python-pptx \
    python-docx \
    openpyxl \
    markdown \
    beautifulsoup4 \
    pillow \
    pydantic

USER sandbox

ENTRYPOINT ["python"]
```

### 7.2 `sandbox-node.Dockerfile`

For HTML, React snippets, dashboards, mini app build steps.

```dockerfile
FROM node:20-slim

RUN useradd -m -u 10001 sandbox
WORKDIR /workspace

# Keep dependencies minimal. For production, use a private package mirror or approved packages only.
RUN npm install -g pnpm

USER sandbox

ENTRYPOINT ["node"]
```

### 7.3 `sandbox-office.Dockerfile`

For PPTX/DOCX to PDF conversion and thumbnails.

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    poppler-utils \
    fonts-dejavu \
    fontconfig \
    ghostscript \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 10001 sandbox
WORKDIR /workspace
USER sandbox

ENTRYPOINT ["bash", "-lc"]
```

### 7.4 `sandbox-webapp.Dockerfile`

For generated web apps and static HTML builds.

```dockerfile
FROM node:20-slim

RUN useradd -m -u 10001 sandbox
WORKDIR /workspace
RUN npm install -g pnpm vite typescript
USER sandbox

ENTRYPOINT ["bash", "-lc"]
```

---

## 8. Sandbox job database schema

Add these tables or equivalent ORM models.

### 8.1 `sandbox_jobs`

```sql
CREATE TABLE sandbox_jobs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    conversation_id UUID,
    execution_id UUID NOT NULL,
    plan_node_run_id UUID,
    skill_version_id UUID,
    artifact_build_job_id UUID,
    runtime_image TEXT NOT NULL,
    command_display TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    -- queued | materializing | running | validating | completed | failed | cancelled | timeout
    network_policy TEXT NOT NULL DEFAULT 'none',
    cpu_limit TEXT NOT NULL DEFAULT '1',
    memory_limit_mb INT NOT NULL DEFAULT 1024,
    pids_limit INT NOT NULL DEFAULT 128,
    timeout_seconds INT NOT NULL DEFAULT 120,
    input_manifest JSONB NOT NULL DEFAULT '{}',
    output_manifest JSONB NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

### 8.2 `sandbox_job_events`

```sql
CREATE TABLE sandbox_job_events (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    sandbox_job_id UUID NOT NULL REFERENCES sandbox_jobs(id),
    execution_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'business',
    -- public | business | developer | admin | hidden
    event_payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 8.3 `sandbox_commands`

```sql
CREATE TABLE sandbox_commands (
    id UUID PRIMARY KEY,
    sandbox_job_id UUID NOT NULL REFERENCES sandbox_jobs(id),
    command_display TEXT NOT NULL,
    exit_code INT,
    stdout TEXT,
    stderr TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

### 8.4 `sandbox_outputs`

```sql
CREATE TABLE sandbox_outputs (
    id UUID PRIMARY KEY,
    sandbox_job_id UUID NOT NULL REFERENCES sandbox_jobs(id),
    output_kind TEXT NOT NULL,
    -- original | preview_pdf | thumbnail | manifest | validation_report | html_bundle | dashboard_json
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    checksum TEXT NOT NULL,
    storage_status TEXT NOT NULL DEFAULT 'pending',
    artifact_version_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 9. Sandbox job lifecycle states

Use this state machine.

```text
queued
→ materializing
→ running
→ collecting
→ validating
→ storing
→ completed
```

Failure states:

```text
failed
timeout
cancelled
blocked_by_policy
validation_failed
```

Each state transition must write an event to PostgreSQL and stream it to the frontend.

---

## 10. Input package model

The sandbox must receive a controlled input package.

Directory structure for a job:

```text
/tmp/zhanlu_sandbox/{sandbox_job_id}/
  input/
    input.json
    data_snapshots/
      snapshot_1.json
      snapshot_2.csv
    templates/
      company_template.pptx
    assets/
      logo.png
      chart_1.png
    skill/
      SKILL.md
      manifest.yaml
      schemas/
      scripts/
      validators/
      references/
  output/
  logs/
```

Rules:

```text
input/ is mounted read-only.
output/ is mounted writable.
logs/ are captured by sandbox-worker.
No secrets are written into input/.
No database credentials are written into input/.
Only approved DataSnapshots, templates, assets, and skill packages are materialized.
```

Example `input.json` for a PPT skill:

```json
{
  "artifact_type": "pptx",
  "title": "Q2 Finance Performance Report",
  "language": "English",
  "template_path": "/workspace/input/templates/company_template.pptx",
  "data_snapshots": [
    "/workspace/input/data_snapshots/snapshot_1.json"
  ],
  "brand_rules": {
    "font": "Inter",
    "logo_position": "top-right"
  },
  "output_dir": "/workspace/output"
}
```

---

## 11. Docker run policy

The sandbox-worker should execute containers with strict defaults.

Default command pattern:

```bash
docker run --rm \
  --name zhanlu-sandbox-job-${JOB_ID} \
  --network none \
  --memory ${MEMORY_MB}m \
  --cpus ${CPUS} \
  --pids-limit ${PIDS_LIMIT} \
  --read-only \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  --user 10001:10001 \
  --workdir /workspace \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v ${JOB_DIR}/input:/workspace/input:ro \
  -v ${JOB_DIR}/output:/workspace/output:rw \
  ${SANDBOX_IMAGE} \
  ${ENTRYPOINT_COMMAND}
```

Notes:

```text
--rm removes the container after execution.
--network none blocks internet and internal network.
--read-only prevents writes outside allowed mounts.
--cap-drop ALL removes Linux capabilities.
--security-opt no-new-privileges blocks privilege escalation.
--user 10001:10001 avoids root execution.
--tmpfs /tmp gives limited temporary writable space.
input mount is read-only.
output mount is writable.
```

Do not pass environment secrets into sandbox containers.

Allowed environment variables inside sandbox should be limited to harmless metadata:

```env
ZHANLU_SANDBOX_JOB_ID=
ZHANLU_ARTIFACT_TYPE=
ZHANLU_OUTPUT_DIR=/workspace/output
```

---

## 12. Network policy

Default:

```text
network_policy = none
```

Blocked by default:

```text
internet access
internal Docker network access
PostgreSQL access
Redis access
Docker socket access
metadata service access
```

If future skills need network access, implement a separate restricted mode:

```text
network_policy = allowlist
allowed_domains = ["api.example.com"]
approval_required = true
audit_required = true
```

Do not implement unrestricted network access for v1.

---

## 13. Sandbox-worker responsibilities

Implement `backend/workers/sandbox_worker.py` with this behavior.

```text
1. Listen to Redis queue: sandbox_jobs.
2. Load sandbox_jobs row from PostgreSQL.
3. Verify job status is queued.
4. Run preflight policy check.
5. Create temporary job directory.
6. Materialize input package from PostgreSQL/artifact storage.
7. Emit sandbox.started / sandbox.materializing events.
8. Run Docker sandbox container with strict limits.
9. Stream stdout/stderr as events.
10. Enforce timeout.
11. Collect output files.
12. Run validators.
13. Store outputs through Artifact Service.
14. Update sandbox_jobs status.
15. Emit artifact.created / artifact.preview_ready events if applicable.
16. Clean temporary files.
17. Remove container.
```

Pseudo-code:

```python
async def process_sandbox_job(job_id: UUID) -> None:
    job = await sandbox_repo.get(job_id)
    await sandbox_repo.mark_materializing(job_id)
    await events.emit("sandbox.materializing", job)

    job_dir = sandbox_paths.create_job_dir(job_id)
    try:
        input_manifest = await materialize_input_package(job, job_dir / "input")
        await sandbox_repo.update_input_manifest(job_id, input_manifest)

        await sandbox_repo.mark_running(job_id)
        await events.emit("sandbox.started", job)

        result = await docker_runner.run(
            job=job,
            input_dir=job_dir / "input",
            output_dir=job_dir / "output",
        )

        await save_command_logs(job_id, result)
        outputs = await output_collector.collect(job, job_dir / "output")

        await sandbox_repo.mark_validating(job_id)
        validation = await validation_runner.validate(job, outputs)

        if not validation.passed:
            await sandbox_repo.mark_validation_failed(job_id, validation)
            await events.emit("artifact.validation_failed", validation)
            return

        artifact_refs = await artifact_service.store_outputs(job, outputs, validation)
        await sandbox_repo.mark_completed(job_id, artifact_refs)
        await events.emit("artifact.preview_ready", artifact_refs)

    except TimeoutError:
        await sandbox_repo.mark_timeout(job_id)
        await events.emit("sandbox.timeout", {"job_id": str(job_id)})
    except Exception as exc:
        await sandbox_repo.mark_failed(job_id, safe_error(exc))
        await events.emit("sandbox.failed", safe_error_payload(exc))
    finally:
        await cleanup.remove_job_dir(job_dir)
        await docker_runner.ensure_container_removed(job_id)
```

---

## 14. Docker runner requirements

Implement `backend/sandbox/docker_runner.py`.

Required features:

```text
Build docker run command from sandbox policy.
Never accept raw user command directly.
Use manifest-approved entrypoint only.
Apply CPU, memory, PID, timeout limits.
Set network none by default.
Mount input read-only.
Mount output writable.
Capture stdout/stderr.
Redact secrets and internal paths before streaming.
Kill container on timeout.
Remove container after run.
Return exit code, stdout, stderr, duration.
```

Do not expose raw Docker command builder to the model.

The command must be built by backend code from approved skill manifest and sandbox policy.

---

## 15. Skill manifest integration

A skill manifest must declare sandbox needs.

Example:

```yaml
skill_id: pptx-generation
name: PPTX Generation
version: 1.0.0
artifact_types:
  - pptx

runtime:
  type: sandbox
  image: zhanlu-sandbox-python:latest
  entrypoint: python /workspace/input/skill/scripts/build_ppt.py /workspace/input/input.json
  timeout_seconds: 120
  memory_mb: 1024
  cpus: "1"
  network: none

permissions:
  requires_sandbox: true
  can_access_datasource: false
  can_write_artifact: true
  can_send_external: false

validation:
  require_preview: true
  require_build_manifest: true
  require_source_refs: true
  validators:
    - python /workspace/input/skill/validators/validate_ppt.py /workspace/output
```

Important:

```text
The manifest may request resources.
The platform policy decides final allowed resources.
Skill cannot increase its own permissions.
```

---

## 16. Data access rule for database-connected agents

Agents may connect to databases through Datasource Gateway, not through sandbox.

Correct flow:

```text
Agent requests data
↓
Datasource Gateway checks agent_data_binding
↓
SQL generation / semantic query
↓
SQL validator enforces read-only + allowlist + row limit
↓
Backend runs query
↓
DataSnapshot stored in PostgreSQL
↓
Sandbox receives DataSnapshot JSON/CSV
```

The sandbox must not receive:

```text
database password
DATABASE_URL
raw production credentials
unrestricted SQL access
full database dump
```

Future exception for advanced enterprise mode:

```text
Temporary read-only credential
short TTL
limited network
allowed tables only
approval required
audited
```

Do not implement this exception in MVP.

---

## 17. Output validation

Generated files are not trusted until validation passes.

Validation checks:

```text
file exists
file size within limit
MIME type matches expected type
checksum calculated
no unsafe macros for Office files
preview generated successfully
required build_manifest exists
required validation_report exists
source_refs include DataSnapshots if data-driven
HTML/mini app preview passes iframe safety checks
output file count within limit
```

For PPTX:

```text
PPTX opens or parses successfully
slide count > 0
no missing required placeholders
PDF preview generated
thumbnail images generated
source_data_snapshot_ids recorded if data-driven
```

For DOCX:

```text
DOCX opens or parses successfully
PDF preview generated
section outline extracted if possible
source refs recorded
```

For HTML/mini app:

```text
no disallowed external scripts
no credential references
iframe sandbox mode required
CSP headers required
network policy recorded
```

---

## 18. Artifact storage integration

Sandbox output must be handed to Artifact Service.

The sandbox-worker should not insert binary artifacts directly in random places unless using the Artifact Service API/module.

Artifact Service stores:

```text
artifacts
artifact_versions
artifact_blobs or artifact_objects
artifact_previews
artifact_validation_reports
artifact_build_manifests
message_artifacts
artifact_interactions
```

MVP:

```text
PostgreSQL BYTEA for original and preview files.
```

Production:

```text
PostgreSQL for metadata/permissions/lineage/checksums.
MinIO for large binary blobs and preview derivatives.
```

---

## 19. Live execution timeline events

The sandbox must stream user-visible events.

Event types:

```text
sandbox.queued
sandbox.materializing
sandbox.started
sandbox.command_started
sandbox.command_stdout
sandbox.command_stderr
sandbox.command_completed
sandbox.output_collected
sandbox.validation_started
sandbox.validation_completed
sandbox.failed
sandbox.timeout
artifact.created
artifact.preview_ready
execution.completed
```

Event visibility levels:

```text
public      visible to all users
business    visible in detailed mode
developer   visible to developer mode
admin       visible to admin only
hidden      stored but never shown in UI
```

Never show:

```text
API keys
DB passwords
raw env variables
Docker socket path
internal host paths
private system prompts
full stack traces with sensitive values
```

Example event:

```json
{
  "event_type": "sandbox.command_started",
  "visibility": "developer",
  "execution_id": "exec_123",
  "sandbox_job_id": "job_456",
  "title": "Build PPTX file",
  "command_display": "python build_ppt.py"
}
```

---

## 20. Frontend behavior

The frontend should show a collapsible sandbox timeline.

Default modes:

```text
Simple mode:
  Creating your PPT...
  Data prepared
  Slides generated
  Preview ready

Detailed mode:
  DataSnapshot created
  PPT skill loaded
  Sandbox started
  PDF preview generated

Developer mode:
  command_display
  stdout
  stderr
  exit code
```

Users can:

```text
Hide sandbox panel
Show sandbox panel
Show details
Collapse details
Open developer logs if allowed
```

Hiding the panel only changes UI. Events are still stored for audit.

---

## 21. Failure and repair behavior

If a sandbox command fails:

```text
1. Store failure status.
2. Store safe stdout/stderr.
3. Emit sandbox.failed.
4. Synexia may create a repair plan if policy allows.
5. A new sandbox job may run with repaired script or input.
6. UI shows failed step + repair step + rerun step.
```

Example UI:

```text
✗ Build PPT failed
  Error: missing chart image

↻ Repairing input package
✓ Added fallback chart
▶ Running PPT sandbox again
✓ PPTX generated
```

Do not retry forever.

Default retry policy:

```text
max_retries = 2
retry_on = transient_error | validation_repairable
no_retry_on = permission_denied | policy_blocked | unsafe_output
```

---

## 22. Cleanup requirements

After every job:

```text
remove temporary container
remove temporary input/output folders
clear temporary locks
keep PostgreSQL records
keep command logs after redaction
keep artifact files only through Artifact Service
```

On startup:

```text
scan sandbox_jobs with running/materializing status
mark old jobs as failed or retryable based on timeout
clean /tmp/zhanlu_sandbox stale folders
ensure no orphan zhanlu-sandbox-job-* containers remain
```

---

## 23. Security checklist

Claude Code must implement these checks.

```text
[ ] backend does not mount Docker socket
[ ] normal worker does not mount Docker socket
[ ] sandbox-worker is the only Docker socket user
[ ] sandbox containers run as non-root user
[ ] sandbox network is none by default
[ ] sandbox root filesystem is read-only
[ ] input mount is read-only
[ ] output mount is writable
[ ] no credentials are passed to sandbox
[ ] no raw database URL is passed to sandbox
[ ] no host directories are mounted except job input/output
[ ] CPU/memory/PID/time limits are enforced
[ ] output size limit is enforced
[ ] container removed after run
[ ] temp folder deleted after run
[ ] stdout/stderr redacted before UI display
[ ] artifacts stored through Artifact Service
[ ] preview served through permission-checked API
```

---

## 24. Acceptance tests

Claude Code must create tests or manual verification steps for these.

### Test 1: backend cannot use Docker

```text
Given backend container is running
When inspecting docker-compose mounts
Then backend must not mount /var/run/docker.sock
```

### Test 2: sandbox-worker can create job container

```text
Given a queued sandbox job
When sandbox-worker processes it
Then a temporary zhanlu-sandbox-job-* container runs
And it is removed after completion
```

### Test 3: sandbox has no network

```text
Given a sandbox job tries to curl https://example.com
When network_policy=none
Then command fails
And event records network blocked or command failure
```

### Test 4: sandbox cannot write input

```text
Given sandbox tries to write into /workspace/input
Then write fails because input is read-only
```

### Test 5: sandbox can write output

```text
Given sandbox writes /workspace/output/result.md
Then output collector finds result.md
And Artifact Service stores it
```

### Test 6: file preview appears in chat

```text
Given a sandbox generates PPTX and PDF preview
When artifact.preview_ready event is emitted
Then frontend shows ArtifactPreviewCard
And preview API streams PDF or thumbnails
```

### Test 7: Redis loss does not lose truth

```text
Given Redis restarts during idle state
Then conversations, artifacts, executions remain in PostgreSQL
And workers can recover pending jobs from PostgreSQL
```

### Test 8: user cannot preview another app artifact

```text
Given user A owns artifact in app A
When user B without access calls preview API
Then backend returns 403
```

---

## 25. Implementation order

Build in this order.

```text
Phase 1:
  docker-compose.yml
  backend, worker, sandbox-worker, postgres, redis services

Phase 2:
  sandbox_jobs tables and repositories
  Redis sandbox queue

Phase 3:
  sandbox-worker basic loop
  docker_runner.py with strict run command

Phase 4:
  basic markdown/html sandbox skill
  output collector
  artifact storage in PostgreSQL

Phase 5:
  live execution timeline events
  frontend sandbox panel

Phase 6:
  PPTX/DOCX sandbox skills
  office conversion sandbox image
  preview PDF/thumbnails

Phase 7:
  validation runner
  retry/repair behavior

Phase 8:
  MinIO/object storage optional
  restricted network mode optional
  gVisor/Kata/Firecracker/E2B/Daytona optional
```

---

## 26. MVP target

The first working MVP should support:

```text
User asks: make a markdown report
→ sandbox job runs
→ result.md stored as artifact
→ inline preview appears in chat

User asks: make an HTML page
→ sandbox job runs
→ HTML stored as artifact
→ sandboxed iframe preview appears

User asks: make a PPT
→ sandbox job runs
→ PPTX generated
→ PDF preview generated
→ artifact preview card appears
```

Do not start with full MCP, swarm, mini app publishing, or complex dashboard automation. Add those after sandbox, artifacts, preview, and timeline are stable.

---

## 27. Final design rule

**Zhanlu’s Docker sandbox is a temporary, governed execution environment used by Layer 5 to run approved skills and artifact builders. The sandbox-worker is the only service allowed to create sandbox containers. Each sandbox job receives a controlled input package containing approved skill files, templates, DataSnapshots, and assets, never raw credentials or unrestricted database access. Containers run with strict CPU, memory, time, filesystem, and network limits. Command logs and file creation events are streamed into the chat’s Live Execution Timeline. Outputs are validated, stored as versioned artifacts through the Artifact Service, previewed inline through permission-checked APIs, and the temporary sandbox container and folders are destroyed after execution.**
