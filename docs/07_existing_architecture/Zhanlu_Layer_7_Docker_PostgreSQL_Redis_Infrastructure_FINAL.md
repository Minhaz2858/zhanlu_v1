# Zhanlu™ Layer 7 — Docker-First Infrastructure Layer

**Version:** 1.0  
**Status:** Final architecture draft for implementation planning  
**Layer:** 7 of the Zhanlu™ Enterprise AI Operating System  
**Owner:** Zhanlu™ / Synexia™ Engineering  
**Primary Technology Choice:** Docker + PostgreSQL + Redis  
**Future Direction:** Kubernetes-ready, but Docker-first for v1  

---

## 0. Executive Summary

Layer 7 is the infrastructure foundation of Zhanlu™. It provides the runtime environment for all upper layers: the interaction gateway, Synexia™ cognitive core, Harness Agent runtime, custom skill execution, enterprise memory and knowledge services, artifact generation, governance services, and observability.

The final Layer 7 direction is:

> **Docker-first, database-first, Redis-supported, sandbox-ready, and Kubernetes-ready later.**

Zhanlu™ v1 should run with a practical Docker Compose infrastructure stack:

```text
Docker / Docker Compose
PostgreSQL
Redis
Nginx / Reverse Proxy
Frontend Service
Backend API Service
Synexia Service
Worker Service
Sandbox Worker Service
Observability Services
Backup Jobs
```

The most important infrastructure rule is:

> **PostgreSQL is the source of truth. Redis is temporary infrastructure. Docker container filesystems and sandbox filesystems are temporary only. Persistent enterprise data must not live in uncontrolled server folders.**

This means Zhanlu™ stores tenant data, memory, knowledge, artifacts metadata, data snapshots, execution records, agent manifests, skill manifests, audit logs, and governance records in PostgreSQL. Redis may support caching, queues, locks, streaming coordination, and rate limiting, but Redis must never become the permanent source of truth.

---

## 1. Layer 7 Core Meaning

Layer 7 should be named:

> **Enterprise Docker-First Infrastructure Layer**  
> **Database-First · Redis-Supported · Sandbox-Ready · Kubernetes-Ready**

Layer 7 is not the AI brain. It does not decide policy. It does not manage agent reasoning. It does not own enterprise knowledge semantics.

Layer 7 provides the physical and runtime substrate:

```text
compute
containers
networking
database
cache / queue
sandbox execution
storage volumes
backup
observability runtime
service deployment
local and dedicated deployment
```

The upper layers use Layer 7 as follows:

| Upper Layer | What it needs from Layer 7 |
|---|---|
| Layer 1 — Interaction & Identity | Nginx, backend API, frontend, WebSocket/SSE support, upload path |
| Layer 2 — Synexia Cognitive Core | Synexia service, execution persistence, Redis coordination, tracing |
| Layer 3 — Harness Agent, Skill & Data Runtime | worker services, sandbox containers, skill package materialization |
| Layer 4 — Memory & Knowledge | PostgreSQL, pgvector, full-text search, backup, optional graph/search later |
| Layer 5 — Execution Layer | sandbox workers, workflow workers, artifact builders, queues |
| Layer 6 — Platform Services | observability stack, audit persistence, cost collection, governance metadata |

---

## 2. Official Layer 7 Design Principle

Use this as the canonical sentence:

> **Layer 7 is Docker-first and database-first. Zhanlu™ v1 runs as a Docker Compose based infrastructure stack with PostgreSQL as the source of truth, Redis as temporary cache/queue infrastructure, isolated backend/Synexia/worker/sandbox services, and Nginx as the entry gateway. Application containers and sandbox containers do not hold permanent business data. All tenant data, memory, knowledge, execution records, agent and skill manifests, artifacts metadata, data snapshots, and audit logs are stored in PostgreSQL, while Redis is used only for temporary coordination, caching, queues, locks, and rate limiting.**

---

## 3. Layer 7 Full Architecture Diagram

```mermaid
flowchart TD

U[User / Browser / Enterprise Client]

subgraph EDGE[Edge & Entry]
NGINX[Nginx / Reverse Proxy<br/>TLS · Routing · Upload Limits · Static Preview Proxy]
end

subgraph APP[Application Services]
FE[Frontend Service<br/>React / Vite]
BE[Backend API Service<br/>FastAPI · Auth · Gateway · Admin APIs]
SYN[Synexia Service<br/>Cognitive FSM · BrainClient · Policy Hook]
end

subgraph WORKERS[Worker Services]
WK[General Worker<br/>Background jobs · ingestion · notifications]
AGW[Agent / Skill Worker<br/>Agent invocation · skill orchestration]
SBX[Sandbox Worker<br/>ephemeral skill/code/artifact containers]
ART[Artifact Worker<br/>PPT/DOCX/PDF/chart preview generation]
end

subgraph DATA[Database-First Data Layer]
PG[(PostgreSQL<br/>source of truth · JSONB · RLS · full-text · pgvector)]
REDIS[(Redis<br/>cache · queue · locks · rate limit · temporary state)]
end

subgraph OBS[Observability & Operations]
OTEL[OpenTelemetry Collector]
PROM[Prometheus]
GRAF[Grafana]
LOGS[Loki / Filebeat / Log Collector]
BACKUP[Backup Jobs<br/>pg_dump / WAL archive / restore test]
end

subgraph OPTIONAL[Optional Scale-Out Later]
MINIO[MinIO / S3-compatible Blob Store<br/>large encrypted artifact blobs]
SEARCH[OpenSearch / Qdrant<br/>large-scale hybrid retrieval]
NEO[Neo4j<br/>enterprise graph / GraphRAG]
K8S[Kubernetes Migration Path]
end

U --> NGINX
NGINX --> FE
NGINX --> BE
BE --> SYN
SYN --> AGW
BE --> WK
AGW --> SBX
AGW --> ART
SBX --> PG
ART --> PG
BE --> PG
SYN --> PG
WK --> PG
BE --> REDIS
SYN --> REDIS
WK --> REDIS
AGW --> REDIS
SBX --> REDIS

BE --> OTEL
SYN --> OTEL
AGW --> OTEL
SBX --> OTEL
OTEL --> PROM
OTEL --> LOGS
PROM --> GRAF
PG --> BACKUP
PG -.optional.-> MINIO
PG -.optional metadata.-> SEARCH
PG -.optional metadata.-> NEO
APP -.future.-> K8S
WORKERS -.future.-> K8S
```

---

## 4. Why Docker-First Is Correct for Zhanlu™ v1

A Kubernetes-first architecture is powerful, but it is too heavy for the first stable implementation. Zhanlu™ needs speed, clarity, easy debugging, and strong conceptual separation before operational complexity.

Docker-first gives:

```text
fast local development
clear service boundaries
easy deployment on one VM or ECS server
simple logs and debugging
simple backup setup
simple skill/sandbox worker separation
clear path to Kubernetes later
```

The key is to design the Docker services with Kubernetes migration in mind:

```text
one process per container
configuration by environment variables
stateless application containers
PostgreSQL as source of truth
Redis as replaceable temporary infrastructure
no permanent business data in app container filesystem
health checks for every service
structured logs for every service
explicit volumes only for PostgreSQL, Redis, backups, and optional blob store
```

---

## 5. Core Services

### 5.1 `zhanlu-frontend`

Purpose:

```text
User interface
chat interface
artifact preview cards
Agent Studio
Skill Studio
Datasource Studio
Admin console
activity rail
plan editor
confirmation cards
```

Technology:

```text
React
Vite
TypeScript
Tailwind
Radix UI
```

Rules:

```text
Frontend never holds secrets.
Frontend never enforces final authorization.
Frontend receives only server-projected apps, artifacts, and actions.
Frontend displays events produced by backend/Synexia.
```

---

### 5.2 `zhanlu-backend`

Purpose:

```text
API gateway
identity/session handling
app projection
conversation APIs
artifact APIs
admin APIs
confirmation APIs
request envelope sealing
```

Technology:

```text
FastAPI
SQLAlchemy / SQLModel
Alembic
Pydantic v2
JWT / session middleware
```

Rules:

```text
Backend is not the AI brain.
Backend does not call LLM providers directly.
Backend passes sealed RequestEnvelope to Synexia.
Backend verifies org_id, app_id, user_id, permissions.
Backend writes audit records through database transactions.
```

---

### 5.3 `zhanlu-synexia`

Purpose:

```text
Layer 2 Synexia Cognitive Core
FSM execution
TaskSpec creation
ContextManifest request
PlanDAG validation
PolicyDecision processing
Capability routing
BrainClient model calls
execution event emission
```

Rules:

```text
All LLM/model calls flow through Synexia.
No generic backend service calls model providers directly.
Synexia receives governed context only.
Synexia does not directly access unrestricted databases or files.
```

---

### 5.4 `zhanlu-worker`

Purpose:

```text
background jobs
knowledge ingestion
document parsing tasks
notification jobs
learning proposal jobs
cleanup tasks
scheduled maintenance
```

Uses Redis for queue coordination but PostgreSQL for persistent state.

---

### 5.5 `zhanlu-agent-skill-worker`

Purpose:

```text
agent invocation coordination
skill package resolution
agent manifest loading
skill manifest loading
AgentExecutionContext preparation
Tool / Skill Gateway dispatch
skill run recording
agent result recording
```

Rules:

```text
Does not execute untrusted code directly.
Delegates risky execution to sandbox worker.
Calls tools only through Tool / Skill Gateway.
Writes AgentInvocation and SkillRun records to PostgreSQL.
```

---

### 5.6 `zhanlu-sandbox-worker`

Purpose:

```text
custom skill execution
code skill execution
PPT/DOCX/PDF artifact generation
chart rendering
preview conversion
safe temporary data package execution
```

Rules:

```text
Runs separate ephemeral execution containers.
Network deny-by-default.
CPU/memory/time limits required.
No persistent business data in sandbox filesystem.
Receives approved data packages by ID.
Writes outputs back through governed artifact/data APIs.
Deletes temporary workspace after execution.
```

---

### 5.7 `zhanlu-artifact-worker`

Purpose:

```text
PPTX generation
DOCX generation
PDF rendering
chart image generation
thumbnail generation
artifact preview conversion
artifact validation
```

This may be combined with `zhanlu-sandbox-worker` in MVP, but it should remain conceptually separate because artifact generation will become important.

---

### 5.8 `zhanlu-postgres`

Purpose:

```text
source of truth for the entire system
```

Stores:

```text
organizations
users
groups
apps
app_grants
conversations
request_envelopes
executions
plans
plan_nodes
context_manifests
observation_records
agent_profiles
skill_profiles
agent_data_bindings
agent_skill_bindings
datasource_profiles
documents
artifacts
artifact_versions
artifact_build_manifests
data_snapshots
memory_items
knowledge_items
decision_memory
experience_entries
learning_proposals
audit_logs
policy_decisions
model_routes
cost_ledger
```

Recommended PostgreSQL features:

```text
JSONB for manifests and flexible metadata
pgvector for embeddings
full-text search for keyword retrieval
RLS for tenant/app/user isolation
transactions for audit safety
indexes for org_id/app_id/user_id
partitioning later for large tables
```

---

### 5.9 `zhanlu-redis`

Purpose:

```text
cache
rate limiting
short-lived locks
job queues
event stream coordination
worker heartbeat
session cache
WebSocket/SSE coordination
```

Redis must not store permanent enterprise data.

Named invariant:

```text
INF-REDIS-1: Redis is never the source of truth. Any Redis data can be lost without corrupting Zhanlu™.
```

---

### 5.10 `zhanlu-nginx`

Purpose:

```text
reverse proxy
TLS termination in simple deployments
routing frontend/backend traffic
upload size limits
request timeout rules
basic compression
preview/static proxy
```

Nginx can later be replaced or extended by cloud load balancers, API gateways, or Kubernetes ingress.

---

## 6. Database-First Storage Policy

Zhanlu™ uses database-first storage.

### 6.1 Source of Truth

PostgreSQL is the source of truth for:

```text
tenant data
user/app/conversation records
request envelopes
execution records
agent and skill manifests
skill packages metadata
memory and knowledge
artifacts metadata
artifact versions
data snapshots
audit logs
policy decisions
cost records
validation reports
```

### 6.2 Container Filesystem Rule

Application container filesystems are temporary. They may contain application code, runtime dependencies, and temporary cache, but not permanent enterprise business data.

### 6.3 Sandbox Filesystem Rule

Sandbox filesystem is temporary and destroyed after execution.

```text
Sandbox input = approved data package
Sandbox output = artifact/data result saved to PostgreSQL or governed blob store
Sandbox disk = temporary only
```

### 6.4 Optional Blob Store

For strict v1 deployments, PostgreSQL may store artifact binaries using `bytea` or large objects.

For larger deployments, an encrypted object/blob store such as MinIO may store large PPTX, PDF, DOCX, XLSX, images, and model artifacts.

If MinIO or S3-compatible storage is used:

```text
PostgreSQL still stores metadata, permissions, lineage, checksums, versions, and audit.
Blob storage is not an authorization source.
Blob storage is governed binary storage only.
Every access must go through backend permission checks or signed short-lived URLs.
```

---

## 7. Docker Compose Service Plan

Recommended service names:

```text
zhanlu-nginx
zhanlu-frontend
zhanlu-backend
zhanlu-synexia
zhanlu-worker
zhanlu-agent-skill-worker
zhanlu-sandbox-worker
zhanlu-artifact-worker
zhanlu-postgres
zhanlu-redis
zhanlu-otel-collector
zhanlu-prometheus
zhanlu-grafana
zhanlu-loki
```

Minimal MVP service set:

```text
zhanlu-nginx
zhanlu-frontend
zhanlu-backend
zhanlu-synexia
zhanlu-worker
zhanlu-sandbox-worker
zhanlu-postgres
zhanlu-redis
```

---

## 8. Example Docker Compose Skeleton

This is not the final production file. It is the reference structure for implementation.

```yaml
version: "3.9"

services:
  nginx:
    image: nginx:1.27
    container_name: zhanlu-nginx
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./deploy/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - frontend
      - backend
    networks:
      - zhanlu-net

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    container_name: zhanlu-frontend
    environment:
      - VITE_API_BASE_URL=/api
    networks:
      - zhanlu-net

  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: zhanlu-backend
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://zhanlu:${POSTGRES_PASSWORD}@postgres:5432/zhanlu
      - REDIS_URL=redis://redis:6379/0
      - SYNEXIA_URL=http://synexia:8643
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      synexia:
        condition: service_started
    networks:
      - zhanlu-net

  synexia:
    build:
      context: ./synexia
      dockerfile: Dockerfile
    container_name: zhanlu-synexia
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://zhanlu:${POSTGRES_PASSWORD}@postgres:5432/zhanlu
      - REDIS_URL=redis://redis:6379/1
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  worker:
    build:
      context: ./backend
      dockerfile: Dockerfile.worker
    container_name: zhanlu-worker
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://zhanlu:${POSTGRES_PASSWORD}@postgres:5432/zhanlu
      - REDIS_URL=redis://redis:6379/2
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  agent_skill_worker:
    build:
      context: ./backend
      dockerfile: Dockerfile.agent-worker
    container_name: zhanlu-agent-skill-worker
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://zhanlu:${POSTGRES_PASSWORD}@postgres:5432/zhanlu
      - REDIS_URL=redis://redis:6379/3
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - zhanlu-net

  sandbox_worker:
    build:
      context: ./sandbox_worker
      dockerfile: Dockerfile
    container_name: zhanlu-sandbox-worker
    env_file:
      - .env
    environment:
      - DATABASE_URL=postgresql+asyncpg://zhanlu:${POSTGRES_PASSWORD}@postgres:5432/zhanlu
      - REDIS_URL=redis://redis:6379/4
      - SANDBOX_NETWORK_DEFAULT=deny
      - SANDBOX_MAX_SECONDS=300
      - SANDBOX_MAX_MEMORY_MB=1024
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
    container_name: zhanlu-postgres
    environment:
      - POSTGRES_DB=zhanlu
      - POSTGRES_USER=zhanlu
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
    volumes:
      - zhanlu-postgres-data:/var/lib/postgresql/data
      - ./deploy/postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U zhanlu -d zhanlu"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - zhanlu-net

  redis:
    image: redis:7
    container_name: zhanlu-redis
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

### Important Security Note

Mounting `/var/run/docker.sock` into `sandbox_worker` is convenient but dangerous. It gives the sandbox worker high control over the host Docker daemon. For MVP it may be acceptable in a trusted deployment, but enterprise deployments should use one of the following instead:

```text
separate sandbox host
restricted Docker API proxy
rootless Docker
container runtime with stricter isolation
gVisor / Kata / Firecracker in later architecture
```

---

## 9. Sandbox Execution in Docker-First Mode

Layer 3 and Layer 5 need sandboxed execution for:

```text
custom code skills
PPT generation
DOCX generation
PDF conversion
chart rendering
spreadsheet processing
safe data transformation
preview generation
```

### 9.1 Sandbox Runtime Flow

```mermaid
sequenceDiagram
participant Worker as Sandbox Worker
participant DB as PostgreSQL
participant Docker as Docker Runtime
participant Box as Ephemeral Sandbox Container

Worker->>DB: Load approved skill package and input data package by IDs
Worker->>Docker: Create ephemeral container with limits
Docker->>Box: Start sandbox execution
Box->>Box: Run skill / artifact generation
Box-->>Worker: Return structured output and files
Worker->>DB: Save outputs, validation report, ArtifactBuildManifest
Worker->>Docker: Destroy container and temp volume
```

### 9.2 Sandbox Rules

```text
Network deny-by-default.
No credentials in sandbox.
Temporary filesystem only.
CPU and memory limits required.
Wall-clock timeout required.
Only approved input package mounted.
Only allowed tools available.
Output must pass validation.
Sandbox destroyed after run.
```

---

## 10. PostgreSQL Architecture

### 10.1 Database Extensions

Recommended extensions:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
```

### 10.2 Database Principles

```text
Every tenant-owned table has org_id.
Every app-owned table has org_id + app_id.
User-private records include user_id.
RLS policies fail closed.
Migrations are controlled by Alembic.
Every critical write is inside a transaction.
Audit writes must succeed in the same transaction as governed reads/actions.
```

### 10.3 Backup Strategy

For v1:

```text
daily pg_dump
continuous WAL archive if possible
backup encryption
restore test schedule
backup retention policy
```

For enterprise:

```text
point-in-time recovery
replica database
backup integrity checks
restore drills
separate backup storage
```

---

## 11. Redis Architecture

Redis is used for short-lived infrastructure support.

### 11.1 Redis Use Cases

```text
rate limiting
session cache
short-lived locks
job queues
task status cache
worker heartbeat
SSE/WebSocket coordination
cache of server-projected app lists
cache of skill registry summaries
```

### 11.2 Redis Anti-Patterns

Do not store in Redis:

```text
audit logs
conversation history
long-term memory
knowledge graph
artifact records
agent manifests
skill manifests
DataSnapshots
policy decisions as only copy
```

### 11.3 Redis Failure Rule

If Redis is lost:

```text
active jobs may need retry
cache is rebuilt
locks expire
stream coordination reconnects
no permanent business data is lost
```

---

## 12. Observability in Docker-First Mode

Layer 7 should provide observability even before Kubernetes.

Recommended MVP:

```text
structured JSON logs from all services
trace_id propagated across services
OpenTelemetry SDK in backend, Synexia, workers
Prometheus metrics endpoint
Grafana dashboard
Loki or log collector for logs
```

Trace chain:

```text
trace_id
  → envelope_id
  → execution_id
  → plan_id
  → node_key
  → agent_invocation_id
  → skill_run_id
  → sandbox_job_id
  → artifact_id
```

Required metrics:

```text
request latency
execution latency
model call latency
worker queue length
sandbox job duration
PostgreSQL connection count
Redis memory usage
artifact generation duration
failed skill runs
failed sandbox jobs
cost by execution
```

---

## 13. Network and Security

### 13.1 Service Network

Use a private Docker network:

```text
zhanlu-net
```

Only Nginx should expose public ports.

### 13.2 Public Ports

Recommended:

```text
80 / 443 → nginx only
PostgreSQL → not public
Redis → not public
Synexia → not public
workers → not public
sandbox worker → not public
```

### 13.3 Secret Management

For v1:

```text
.env file for local/dev only
server-side environment variables for deployment
never commit secrets
```

For enterprise:

```text
Vault or cloud secret manager
key rotation
per-tenant model keys
per-datasource credential refs
```

### 13.4 Network Egress

For sandbox and skill workloads:

```text
default deny
allowlist only if skill manifest permits
all external access logged
no arbitrary internet access for generated skills
```

---

## 14. Optional Services for Later

These should not be mandatory in v1, but the architecture should allow them.

### 14.1 MinIO

Use if artifact files become large.

```text
large PPTX/PDF/DOCX/XLSX/image blobs
encrypted storage
PostgreSQL metadata and authorization
signed preview URLs
```

### 14.2 OpenSearch / Qdrant

Use if Layer 4 retrieval scale grows beyond PostgreSQL full-text + pgvector.

```text
large-scale hybrid search
vector search at scale
large document memory
```

### 14.3 Neo4j

Use if enterprise graph becomes too complex for PostgreSQL graph tables.

```text
entity graph
GraphRAG
relationship-heavy reasoning
multi-hop decision analysis
```

### 14.4 vLLM / Triton / Ollama

Use if Zhanlu needs self-hosted model endpoints.

```text
local model inference
restricted data processing
customer-owned model runtime
embedding/reranker services
```

---

## 15. Deployment Profiles

### 15.1 Local Development

```text
Docker Compose
PostgreSQL
Redis
frontend
backend
Synexia
workers
```

Purpose:

```text
local development
testing
small demos
architecture validation
```

### 15.2 Single-Server Enterprise Deployment

```text
Docker Compose on one ECS/VM
PostgreSQL Docker volume
Redis Docker volume
Nginx TLS
scheduled backups
basic monitoring
```

Purpose:

```text
small enterprise pilot
dedicated customer demo
controlled internal deployment
```

### 15.3 Dedicated Customer Deployment

```text
customer-owned server or cloud account
Docker Compose or future Kubernetes
customer-owned PostgreSQL
customer-owned model key
customer-owned encryption key
customer-controlled backup
```

Purpose:

```text
data sovereignty
enterprise compliance
private deployment
```

### 15.4 Kubernetes Future

When scale grows:

```text
Kubernetes
PostgreSQL operator
Redis operator
KEDA
service mesh
GPU node pool
Kueue / Volcano
Kata/gVisor sandbox runtime
```

The application should be designed so containers migrate cleanly from Docker Compose to Kubernetes.

---

## 16. Repository Structure

Recommended infrastructure structure:

```text
deploy/
  docker/
    docker-compose.yml
    docker-compose.dev.yml
    docker-compose.prod.yml
    docker-compose.observability.yml

  nginx/
    nginx.conf
    conf.d/
      zhanlu.conf

  postgres/
    init/
      001_extensions.sql
      002_roles.sql
    backup/
      backup.sh
      restore.sh

  redis/
    redis.conf

  observability/
    otel-collector.yaml
    prometheus.yml
    grafana/
      dashboards/
      datasources/

  sandbox/
    Dockerfile.skill-sandbox
    Dockerfile.artifact-sandbox
    limits.json

scripts/
  dev_up.sh
  dev_down.sh
  migrate.sh
  backup_postgres.sh
  restore_postgres.sh
  check_health.sh
  rotate_logs.sh
```

---

## 17. Environment Variables

Recommended core environment variables:

```text
POSTGRES_PASSWORD=
DATABASE_URL=
REDIS_URL=
JWT_SECRET=
SYNEXIA_URL=
MODEL_PROVIDER=
MODEL_API_KEY=
MODEL_BASE_URL=
SANDBOX_NETWORK_DEFAULT=deny
SANDBOX_MAX_SECONDS=300
SANDBOX_MAX_MEMORY_MB=1024
ARTIFACT_STORAGE_MODE=postgres
ENABLE_MINIO=false
OTEL_EXPORTER_OTLP_ENDPOINT=
```

Rules:

```text
Never commit .env to Git.
Use .env.example only.
Production secrets must be injected securely.
Per-tenant secrets should be stored as credential references, not plain env vars.
```

---

## 18. Health Checks

Every service should expose health checks.

```text
/frontend health: static app reachable
/backend health: /healthz, /readyz
/synexia health: /healthz, /readyz
/worker health: heartbeat in Redis or DB
/sandbox-worker health: can start test sandbox
/postgres health: pg_isready
/redis health: PING
/nginx health: upstream status
```

Readiness should check dependencies. Liveness should check process health.

---

## 19. Backup and Disaster Recovery

Layer 7 must support restore, not only backup.

### 19.1 Backup Targets

```text
PostgreSQL database
PostgreSQL WAL archive if enabled
optional MinIO/blob store
configuration files
skill package records if stored externally
```

### 19.2 Restore Tests

Restore should be tested:

```text
weekly for development
monthly for enterprise pilot
before every major release
```

### 19.3 RPO / RTO Targets

For v1 pilot:

```text
RPO: 24 hours
RTO: 4 hours
```

For enterprise production:

```text
RPO: 1 hour or lower
RTO: 1 hour or lower
```

---

## 20. Infrastructure Governance

Infrastructure governance includes:

```text
capacity management
performance management
backup and restore
security patching
vulnerability scanning
incident response
disaster recovery
business continuity
cost monitoring
service-level monitoring
```

Required reports:

```text
service uptime
PostgreSQL backup status
restore test status
Redis memory usage
worker queue delay
sandbox failure rate
artifact generation failure rate
storage growth
cost trend
security patch status
```

---

## 21. Big Diagram Update Guidance

Current Layer 7 label:

```text
Infrastructure Layer
Cloud Native · Scalable · Resilient · High Performance
```

Recommended label:

```text
Enterprise Docker-First Infrastructure Layer
Database-First · Redis-Supported · Sandbox-Ready · Kubernetes-Ready
```

Recommended boxes:

```text
Docker Runtime
Docker Compose · Service Containers · Health Checks

PostgreSQL Infrastructure
Source of Truth · JSONB · pgvector · RLS · Backup

Redis Infrastructure
Cache · Queue · Locks · Rate Limit · Worker Coordination

Application Services
Frontend · Backend API · Synexia · Workers

Sandbox Infrastructure
Ephemeral Containers · Resource Limits · Network Deny · Artifact Jobs

Network & Edge
Nginx · TLS · Reverse Proxy · Upload Limits · Private Service Network

Observability & Operations
Logs · Metrics · Traces · Backup · Health Checks

Future Scale Path
Kubernetes · MinIO · OpenSearch/Qdrant · Neo4j · GPU Serving
```

---

## 22. Layer 7 Invariants

```text
INF-0: Layer 7 is the infrastructure data plane. It runs workloads but does not decide AI behavior.

INF-1: Zhanlu™ v1 is Docker-first, not Kubernetes-first.

INF-2: PostgreSQL is the source of truth for tenant data, memory, knowledge, artifacts metadata, executions, manifests, and audit logs.

INF-3: Redis is temporary infrastructure only and never the source of truth.

INF-4: Docker application container filesystems are not permanent business-data storage.

INF-5: Sandbox filesystems are temporary and destroyed after execution.

INF-6: Agents and skills never receive credentials; infrastructure exposes only governed handles.

INF-7: Sandbox and skill network egress is deny-by-default.

INF-8: Every service emits structured logs and trace identifiers.

INF-9: Queues and event streams are coordination mechanisms; persistent state lives in PostgreSQL.

INF-10: Backups must be restorable and restore must be tested.

INF-11: Dedicated deployment customers may receive tenant-owned database, model key, encryption key, and deployment profile.

INF-12: The Docker-first service design must remain Kubernetes-ready.
```

---

## 23. Acceptance Criteria

### Docker Runtime

- [ ] All core services run with Docker Compose.
- [ ] Only Nginx exposes public HTTP/HTTPS ports.
- [ ] PostgreSQL and Redis are not publicly exposed.
- [ ] Every service has health checks.
- [ ] Application services are stateless except PostgreSQL and Redis volumes.

### PostgreSQL

- [ ] PostgreSQL stores all persistent business records.
- [ ] pgvector extension is available.
- [ ] JSONB is used for manifests and flexible metadata.
- [ ] RLS policies can be enabled for tenant/app isolation.
- [ ] Backup and restore scripts exist.
- [ ] Restore test is documented.

### Redis

- [ ] Redis is used only for cache, queue, locks, rate limit, or temporary state.
- [ ] Losing Redis does not lose permanent business data.
- [ ] Redis data can be rebuilt from PostgreSQL when needed.

### Sandbox

- [ ] Sandbox jobs do not run inside main backend container.
- [ ] Sandbox containers have CPU, memory, and time limits.
- [ ] Sandbox network is deny-by-default.
- [ ] Sandbox receives approved data packages by ID.
- [ ] Sandbox output is saved back to PostgreSQL or governed storage.
- [ ] Sandbox temporary workspace is deleted after execution.

### Observability

- [ ] All services emit structured logs.
- [ ] `trace_id` is propagated across backend, Synexia, workers, and sandbox jobs.
- [ ] Metrics exist for request latency, worker queues, sandbox jobs, PostgreSQL, Redis, and artifact generation.
- [ ] Errors include execution_id / agent_invocation_id / skill_run_id where applicable.

### Deployment

- [ ] `.env.example` exists without secrets.
- [ ] Docker Compose files are separated for dev/prod/observability.
- [ ] Production deployment has backup plan.
- [ ] Dedicated deployment profile is documented.
- [ ] Kubernetes migration path is documented but not required for v1.

---

## 24. Implementation Priority

### P0 — Minimum Working Infrastructure

```text
Docker Compose
Nginx
Frontend
Backend
Synexia
PostgreSQL with pgvector
Redis
General worker
Basic sandbox worker
Backup script
Health checks
```

### P1 — Enterprise Pilot Infrastructure

```text
Agent/skill worker separation
Artifact worker
Structured logs
OpenTelemetry traces
Prometheus/Grafana
Backup restore tests
Sandbox resource limits
Nginx TLS
Production .env management
```

### P2 — Stronger Production Infrastructure

```text
PostgreSQL replication or managed PostgreSQL
Redis persistence and monitoring
MinIO optional for large artifact blobs
separate sandbox host
restricted Docker API proxy
log aggregation
alerting
incident runbooks
```

### P3 — Future Scale Path

```text
Kubernetes
KEDA
service mesh
Kata/gVisor sandbox runtime
OpenSearch/Qdrant
Neo4j
GPU serving
vLLM/Triton/Ray Serve
multi-region disaster recovery
```

---

## 25. Final Summary

Layer 7 is the practical runtime foundation for Zhanlu™. It should begin as a Docker-first infrastructure stack using PostgreSQL and Redis, because this supports fast implementation, easy debugging, and simple enterprise deployment. PostgreSQL is the system of record for all persistent enterprise data, while Redis supports temporary coordination, caching, queues, locks, and rate limiting. Application containers and sandbox containers must remain stateless with respect to business data, and sandbox execution must be isolated, resource-limited, and temporary.

The Docker-first architecture is not a dead end. It is designed so Zhanlu™ can later migrate to Kubernetes, PostgreSQL HA, stronger sandbox runtimes, GPU-serving infrastructure, and large-scale retrieval systems without changing the core architectural principles.
