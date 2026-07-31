# Zhanlu Live Sandbox Workspace and Inline Chat Execution

## Document status

**Status:** Final architecture note  
**Purpose:** Preserve the design for a Claude-Code-like sandbox experience inside Zhanlu chat.  
**Scope:** Sandbox execution, database-connected agents, artifact generation, inline chat timeline, PPT/DOCX/HTML/dashboard/mini-app creation, and safe enterprise controls.  

---

# 1. Core idea

Zhanlu should support a **Claude-Code-like live sandbox experience** inside the chatbot.

When a user asks an agent to make something, such as a PPT, DOCX file, dashboard, HTML page, chart, or mini app, Zhanlu should not only answer with text. It should:

```text
understand the request
plan the work
select the correct agent and skills
collect approved data
create a temporary sandbox workspace
run safe generation scripts
stream progress to the chat UI
validate the output
store the result as a governed artifact
show inline preview in chat
allow edit, regenerate, approve, download, publish, or schedule update
```

This gives the user the feeling that the AI is actually working, similar to Claude Code, but with Zhanlu enterprise controls.

The short rule:

```text
Chat is the interface.
Synexia is the brain.
Layer 5 is the executor.
Sandbox is the temporary workbench.
PostgreSQL is the source of truth.
Artifacts are the final outputs.
```

---

# 2. What Claude Code-like behavior means for Zhanlu

In Claude Code-style execution, the assistant can:

```text
read task context
read tool or skill documentation
create a working directory
write scripts
run commands
inspect outputs
fix errors
run again
create a final file
show progress in chat
```

For Zhanlu, the equivalent is:

```text
read RequestEnvelope
load agent profile
load allowed skills
load skill documentation
create a sandbox job
materialize approved input package
run sandbox commands
stream stdout and stderr to chat
collect generated files
validate generated files
store files into PostgreSQL artifact tables
show inline artifact preview
```

Important difference:

```text
Claude Code may feel like an open coding workspace.
Zhanlu must be an enterprise-governed execution workspace.
```

So Zhanlu should copy the **experience**, not unsafe freedom.

---

# 3. Final feature name

Use this product/architecture name:

```text
Zhanlu Live Sandbox Workspace
```

Alternative UI-facing names:

```text
Live Build Workspace
Artifact Build Workspace
Agent Execution Workspace
Sandbox Execution Timeline
```

Recommended final wording:

> Zhanlu Live Sandbox Workspace is the temporary, controlled execution environment used by agents and skills to create files, dashboards, mini apps, previews, and other artifacts while streaming progress back to the inline chat UI.

---

# 4. When should Zhanlu use sandbox?

Do **not** use sandbox for every message.

Use sandbox when the task requires actual execution, file generation, conversion, data processing, or validation.

## Use sandbox for

```text
make PPT
make DOCX
make PDF
make Markdown file
make HTML page
make chart
make dashboard
make mini app
analyze uploaded spreadsheet
run Python/Node code
convert PPTX to PDF
convert DOCX to PDF
render dashboard preview
validate artifact
regenerate slide or section
build/export downloadable file
```

## Do not use sandbox for

```text
simple Q&A
short explanation
general writing answer
small text rewrite
basic summarization inside chat
simple planning with no file output
```

Synexia should decide:

```text
Does this task require real execution?
Yes -> create sandbox job.
No -> answer normally through chat.
```

---

# 5. High-level architecture

```text
User Browser
  ↓ HTTPS
Nginx / Reverse Proxy
  ↓
Frontend Chat UI
  ↓
Backend API
  ↓
Layer 1 RequestEnvelope
  ↓
Layer 2 Synexia PlanDAG
  ↓
Layer 3 Agent + Skill + Data Runtime
  ↓
Layer 5 Execution Layer
  ↓
Redis Job Queue
  ↓
Sandbox Worker
  ↓
Temporary Docker Sandbox Container
  ↓
Generated Outputs
  ↓
Backend Artifact Storage
  ↓
PostgreSQL
  ↓
Inline Preview API
  ↓
Chat Artifact Card
```

Important:

```text
The user never uses SSH.
The user only uses the browser.
SSH is only for deployment and maintenance.
```

---

# 6. Docker/SSH server deployment model

On the SSH server, Zhanlu runs through Docker Compose.

Core services:

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

Only Nginx is public:

```text
Public ports:
  80
  443

Internal Docker network:
  backend
  frontend
  synexia
  worker
  sandbox-worker
  postgres
  redis
```

Security rule:

```text
Only sandbox-worker can create temporary sandbox containers.
Backend must not run user code.
Synexia must not run user code.
Normal worker must not run user code.
```

---

# 7. Sandbox worker responsibility

The sandbox-worker is a special service. It is the only component that can create temporary sandbox containers.

Responsibilities:

```text
read sandbox job from Redis queue
load job metadata from PostgreSQL
create temporary job folder
materialize approved input package
start temporary Docker container
stream command logs to execution events
collect output files
run validation
send output bytes/metadata back to backend
store artifacts in PostgreSQL through backend/internal service
remove container
delete temporary files
mark job completed or failed
```

The sandbox-worker should be strongly isolated from normal backend logic.

---

# 8. Sandbox container rules

A sandbox container is temporary. It exists only for one job.

Default rules:

```text
network disabled
non-root user
limited CPU
limited memory
limited runtime
limited file size
read-only root filesystem
input folder read-only
output folder writable
no secrets
no raw database credentials
no Docker socket
no host filesystem access
container removed after job
```

Example Docker command:

```bash
docker run --rm \
  --name zhanlu-sandbox-job-123 \
  --network none \
  --memory 1g \
  --cpus 1 \
  --pids-limit 128 \
  --read-only \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  -v /tmp/zhanlu_sandbox/job_123/input:/workspace/input:ro \
  -v /tmp/zhanlu_sandbox/job_123/output:/workspace/output:rw \
  zhanlu-sandbox-pptx:latest
```

Meaning:

```text
--rm                       delete container after run
--network none             no internet by default
--memory 1g                memory limit
--cpus 1                   CPU limit
--pids-limit 128           process limit
--read-only                root filesystem read-only
--security-opt             no privilege escalation
--cap-drop ALL             remove Linux capabilities
input:ro                   input is read-only
output:rw                  only output folder is writable
```

---

# 9. Sandbox modes

Zhanlu should support three sandbox modes.

## Mode 1: Safe Auto Sandbox

Runs automatically with strict limits.

Use for:

```text
Markdown generation
HTML static preview
PPT generation from approved DataSnapshots
DOCX generation from approved content
chart generation
PDF preview generation
artifact validation
```

Default restrictions:

```text
no network
no secrets
no database credentials
approved input only
fixed runtime image
strict timeout
```

## Mode 2: Approval Sandbox

Requires approval before execution.

Use for:

```text
network access
external API call
custom code skill
large database extract
installing new dependencies
publishing artifact
sending email or external message
```

## Mode 3: Blocked

Always blocked.

```text
host filesystem access
read /etc secrets
access Docker socket
connect to arbitrary internet
run privileged container
delete persistent data
write to production database
run unknown binary without review
exfiltrate data
```

---

# 10. Prebuilt sandbox images

Do not install random dependencies during every job in production.

Use prebuilt sandbox images.

Recommended images:

```text
zhanlu-sandbox-python
  python
  pandas
  matplotlib
  python-pptx
  python-docx
  openpyxl
  reportlab

zhanlu-sandbox-node
  node
  pptxgenjs
  chart libraries
  static site builder tools

zhanlu-sandbox-office
  libreoffice
  poppler
  PDF conversion tools
  thumbnail generation tools

zhanlu-sandbox-webapp
  node
  vite
  react
  html/css/js builder

zhanlu-sandbox-dashboard
  python
  plotly
  dashboard renderer
  JSON dashboard builder
```

For custom user-created skills:

```text
Option A: allow only approved dependencies.
Option B: build reviewed skill image after approval.
Option C: use private package mirror.
Option D: later integrate managed sandbox providers.
```

---

# 11. Database-connected agents and sandbox safety

Each agent can connect with approved databases, but the sandbox should **not** receive raw database credentials.

Bad design:

```text
Sandbox gets finance database password.
Sandbox runs any SQL.
Sandbox can access all tables.
Sandbox can export full database.
Sandbox can connect to arbitrary internet.
```

Correct Zhanlu design:

```text
Agent has datasource binding.
Layer 3 checks agent permission.
Datasource Gateway validates query.
Backend runs read-only query.
Layer 4 stores DataSnapshot.
Sandbox receives only DataSnapshot JSON/CSV.
Sandbox uses snapshot to build artifact.
```

This is the key safety rule:

```text
Sandbox uses DataSnapshots, not live unrestricted databases.
```

---

# 12. Data flow for a database-connected agent

Example user request:

```text
Make a Q2 finance PPT using my finance database.
```

Flow:

```text
1. User sends message in chat.
2. Layer 1 stores message and creates RequestEnvelope.
3. Synexia understands the task and selects Finance Agent.
4. Finance Agent profile allows finance datasource only.
5. Synexia creates PlanDAG.
6. Datasource Gateway checks data binding.
7. Governed NL2SQL skill creates SQL.
8. SQL Validator checks read-only, allowed tables, row limits, timeout.
9. Backend runs query with controlled credential.
10. DataSnapshot is stored in PostgreSQL.
11. Sandbox receives DataSnapshot JSON/CSV, not database password.
12. PPT skill uses snapshot to generate PPT.
13. Artifact is validated and stored.
14. Inline chat preview appears.
```

Example Finance Agent binding:

```text
allowed datasource: finance_postgres
allowed tables: revenue, cost, budget
blocked tables: payroll, salary, credentials
allowed operation: read-only select/aggregate
row limit: 10,000
query timeout: 30 seconds
```

---

# 13. Combining AI knowledge, memory, and database data

Zhanlu responses and artifacts can combine three context types:

```text
1. General model knowledge
2. Zhanlu memory and knowledge base
3. Connected database DataSnapshots
```

Trust hierarchy:

```text
DataSnapshot = strongest evidence for numbers
Uploaded documents = evidence with source
Approved organization knowledge = trusted policy/context
Memory = useful context, not automatic truth
General AI knowledge = background reasoning and structure
```

Example:

```text
User asks: Make a sales pitch PPT using our Q2 finance data.
```

Zhanlu uses:

```text
general AI knowledge:
  how to structure a sales pitch

company knowledge:
  brand tone, previous reports, approved templates

database data:
  revenue, margin, growth, customer segments

skills:
  governed-nl2sql
  data-snapshot
  chart-generation
  pptx-generation
  pdf-preview
  artifact-validation

sandbox:
  actually builds the PPTX and preview files
```

Final artifact must record:

```text
source_data_snapshots
source_documents
source_template
created_by_agent
created_by_skill
created_by_execution
validation_report
```

---

# 14. Full example: make PPT from user input to final result

User:

```text
Make a 10-slide PPT about Q2 sales performance using my finance database.
```

## Step 1: Chat request

Frontend sends:

```http
POST /api/v1/chat/stream
```

Payload includes:

```json
{
  "conversation_id": "conversation_uuid",
  "app_id": "finance_app_uuid",
  "message": "Make a 10-slide PPT about Q2 sales performance using my finance database."
}
```

Backend stores:

```text
messages
request_envelopes
```

## Step 2: Synexia planning

Synexia creates:

```text
TaskSpec
ArtifactIntent
PlanDAG
PolicyDecision
ExecutionVersionStamp
```

Plan nodes:

```text
node_1: understand finance PPT goal
node_2: retrieve finance app context
node_3: validate Finance Agent datasource binding
node_4: create Q2 sales DataSnapshot
node_5: generate charts
node_6: build slide outline
node_7: run PPT sandbox job
node_8: generate PDF preview and thumbnails
node_9: validate artifact
node_10: attach artifact to chat
```

## Step 3: DataSnapshot creation

Datasource Gateway runs:

```text
schema lookup
metric definition lookup
SQL generation
SQL validation
read-only query
row limit check
query timeout check
```

Layer 4 stores:

```text
data_snapshots
```

## Step 4: Sandbox job creation

Layer 5 creates:

```text
artifact_build_jobs
sandbox_jobs
sandbox_job_events
```

Redis receives:

```text
sandbox_job_id
```

## Step 5: Sandbox materialization

Sandbox-worker creates temporary input folder:

```text
/tmp/zhanlu_sandbox/job_123/input/
```

Input package contains:

```text
input.json
q2_sales_snapshot.json
chart_assets/
company_template.pptx
skill_package/
brand_rules.json
build_instructions.json
```

The sandbox does **not** receive:

```text
database password
raw server path
full company file system
unrestricted internet
Docker socket
```

## Step 6: Sandbox execution

Sandbox runs:

```bash
python build_ppt.py --input /workspace/input/input.json --output /workspace/output
```

Or for Node-based PPT generation:

```bash
node build.js
```

Sandbox creates:

```text
/workspace/output/Q2_Sales_Performance.pptx
/workspace/output/Q2_Sales_Performance_preview.pdf
/workspace/output/thumbnails/slide_1.png
/workspace/output/thumbnails/slide_2.png
/workspace/output/build_manifest.json
/workspace/output/validation_report.json
```

## Step 7: Store artifact

Backend stores:

```text
artifacts
artifact_versions
artifact_blobs
artifact_previews
artifact_validation_reports
message_artifacts
execution_events
```

The PPTX binary, PDF preview, and thumbnails are stored in PostgreSQL in strict DB-first mode.

Later, for large deployments, PostgreSQL can remain source of truth while encrypted object/blob storage stores large binary bytes.

## Step 8: Inline preview

Backend emits:

```text
artifact.preview_ready
```

Frontend shows:

```text
Q2 Sales Performance.pptx
Status: Preview ready
Version: v1
Sources: Q2 sales DataSnapshot, company template
Actions: Preview · Edit · Regenerate · Approve · Download · Publish
```

User previews the PPT inside chat.

---

# 15. Live execution timeline in chat

Zhanlu should show progress like Claude Code.

Example timeline:

```text
User:
Make a Q2 finance PPT.

Assistant:
I’ll create a finance presentation using your approved finance data and company PPT template.

[Step] Understanding request
Done

[Step] Checking Finance Agent permissions
Done

[Step] Creating DataSnapshot from finance database
Running...
SQL validated: read-only
Rows: 248
Done

[Step] Building slide outline
Done

[Step] Running PPT generation sandbox
Command: python build_ppt.py
Output: Created Q2_Finance_Report.pptx
Done

[Step] Generating PDF preview
Done

[Step] Validating artifact
Done

[Artifact Card]
Q2 Finance Report.pptx
Preview · Edit · Regenerate · Approve · Download
```

This is not just decoration. Each visible event should be backed by a stored execution event in PostgreSQL.

---

# 16. Sandbox event model

Every sandbox action should emit events.

Event types:

```text
sandbox.started
sandbox.file_materialized
sandbox.command_started
sandbox.command_stdout
sandbox.command_stderr
sandbox.command_completed
sandbox.file_created
sandbox.validation_started
sandbox.validation_completed
artifact.created
artifact.preview_ready
execution.completed
execution.failed
```

Example command-started event:

```json
{
  "event_type": "sandbox.command_started",
  "execution_id": "exec_123",
  "sandbox_job_id": "job_456",
  "title": "Build PPTX file",
  "command_display": "python build_ppt.py"
}
```

Example stdout event:

```json
{
  "event_type": "sandbox.command_stdout",
  "sandbox_job_id": "job_456",
  "text": "WROTE /workspace/output/Q2_Finance_Report.pptx"
}
```

Frontend rendering:

```text
[Command] Build PPTX file
python build_ppt.py

Output:
WROTE Q2_Finance_Report.pptx
```

---

# 17. Inline artifact preview

The frontend should never access raw server paths.

Preview APIs:

```text
GET /api/v1/artifacts/{artifact_id}
GET /api/v1/artifacts/{artifact_id}/preview
GET /api/v1/artifacts/{artifact_id}/thumbnail?page=1
GET /api/v1/artifacts/{artifact_id}/download
POST /api/v1/artifacts/{artifact_id}/regenerate
POST /api/v1/artifacts/{artifact_id}/approve
POST /api/v1/artifacts/{artifact_id}/publish
```

Preview behavior:

```text
PPTX      -> PDF preview + slide thumbnails
DOCX      -> PDF preview
MD        -> rendered markdown HTML
HTML      -> sandboxed iframe preview
Dashboard -> interactive dashboard card/iframe
Mini app  -> sandboxed iframe with permission boundary
Chart     -> inline chart card
```

PPT preview flow:

```text
PPTX generated
↓
Convert PPTX to PDF
↓
Convert PDF pages to slide thumbnails
↓
Store PPTX, PDF, thumbnails in PostgreSQL
↓
Emit artifact.preview_ready
↓
Frontend shows ArtifactPreviewCard
↓
User previews slides inline
```

---

# 18. Edit and regeneration flow

Modern AI should support partial regeneration.

User:

```text
Make slide 3 simpler.
```

Flow:

```text
1. User message stored.
2. Synexia identifies target artifact and target part.
3. Artifact source JSON is loaded.
4. PPT skill receives previous artifact version and target slide ID.
5. Sandbox regenerates slide 3 if possible.
6. New artifact version is stored.
7. Chat shows version comparison or updated preview.
```

Artifacts should store structured source parts:

```text
For PPT:
  slides
  placeholders
  charts
  tables
  speaker notes
  theme
  data references

For DOCX:
  sections
  paragraphs
  tables
  figures
  references
  appendices

For dashboard:
  cards
  charts
  filters
  data views
  layout
  refresh policy

For mini app:
  pages
  components
  state
  actions
  data bindings
  permissions
```

Required artifact fields:

```text
artifact_source_json
artifact_source_parts
build_manifest
validation_report
version history
source references
```

---

# 19. Sandboxed dashboard and mini app

## Dashboard

Dashboard generation should create a data-bound artifact, not only an image.

Flow:

```text
Dashboard skill creates dashboard_definition.json.
Frontend renders it with approved React chart components.
Data comes from DataSnapshots or approved DataViews.
Auto-update creates new snapshots and new dashboard versions.
```

Dashboard update modes:

```text
manual_refresh
scheduled_refresh
event_driven_refresh
```

## Mini app

Mini app generation is more sensitive.

Flow:

```text
Mini app skill creates app package.
Sandbox builds it.
Preview runs in sandboxed iframe.
No enterprise data access unless explicit data binding exists.
Publishing requires approval.
```

Iframe baseline:

```html
<iframe
  sandbox="allow-scripts"
  src="/api/v1/mini-apps/{id}/preview">
</iframe>
```

Do not allow by default:

```text
allow-same-origin
allow-forms
allow-popups
credential access
unrestricted network access
```

Only add permissions after review.

---

# 20. Sandbox database tables

Required tables:

```text
sandbox_jobs
sandbox_job_events
sandbox_files
sandbox_commands
sandbox_outputs
sandbox_validation_reports
sandbox_runtime_images
sandbox_policies
```

Example:

```sql
CREATE TABLE sandbox_jobs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    artifact_build_job_id UUID,
    skill_version_id UUID,
    runtime_image TEXT NOT NULL,
    status TEXT NOT NULL,
    network_policy TEXT NOT NULL DEFAULT 'none',
    cpu_limit TEXT NOT NULL DEFAULT '1',
    memory_limit_mb INT NOT NULL DEFAULT 1024,
    timeout_seconds INT NOT NULL DEFAULT 120,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

Command log table:

```sql
CREATE TABLE sandbox_commands (
    id UUID PRIMARY KEY,
    sandbox_job_id UUID NOT NULL,
    command_display TEXT NOT NULL,
    exit_code INT,
    stdout TEXT,
    stderr TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

---

# 21. Storage model

PostgreSQL stores persistent truth:

```text
conversations
messages
request_envelopes
executions
execution_events
sandbox_jobs
sandbox_commands
agents
skills
datasources
data_snapshots
artifacts
artifact_versions
artifact_blobs
artifact_previews
audit_logs
```

Redis stores only temporary coordination:

```text
queues
locks
worker heartbeat
stream fanout
rate limit
short-lived cache
```

Sandbox temporary folder stores only job-local files:

```text
/tmp/zhanlu_sandbox/job_id/input
/tmp/zhanlu_sandbox/job_id/output
```

After job completion:

```text
outputs are copied to PostgreSQL
container is removed
temporary folders are deleted
```

Final storage rule:

```text
No permanent business data in backend folders.
No permanent artifacts in sandbox folders.
No permanent skill packages in temp folders.
No Redis source-of-truth data.
PostgreSQL is the source of truth.
```

---

# 22. Failure and recovery

If a command fails:

```text
store stdout/stderr
store exit code
mark sandbox command failed
emit sandbox.command_failed
Synexia decides repair, retry, ask user, or fail
```

If sandbox job times out:

```text
kill container
store timeout error
mark job failed
emit sandbox.timeout
retry if policy allows
```

If server restarts:

```text
PostgreSQL keeps execution state
worker scans running jobs on startup
stale jobs become retryable or failed
temporary folders are cleaned
Redis queues are rebuilt from PostgreSQL when necessary
```

Important:

```text
If Redis is lost, Zhanlu may restart jobs, but must not lose execution truth.
```

---

# 23. Security model

Zhanlu must use defense in depth:

```text
permission gate
policy gate
memory gate
data binding gate
skill preflight
sandbox isolation
artifact validation
audit logging
approval for high-risk actions
```

Sandbox must never have:

```text
raw database credentials
long-lived tokens
Docker socket
host filesystem
unrestricted network
production write permissions
unbounded CPU/memory/runtime
```

High-risk actions require approval:

```text
network access
external API access
custom code skill
publishing artifact
sharing outside app
writing to connected system
large data export
installing dependency
```

---

# 24. MVP implementation plan

## Phase 1: Basic sandbox execution

```text
sandbox_jobs table
Redis sandbox queue
sandbox-worker service
prebuilt Python sandbox image
run simple script
capture stdout/stderr
store command events
```

## Phase 2: Artifact generation

```text
MD artifact
HTML artifact
basic PPTX artifact
PDF preview
artifact storage in PostgreSQL
inline chat artifact card
```

## Phase 3: DataSnapshot integration

```text
agent datasource binding
read-only query
SQL validation
DataSnapshot creation
sandbox receives snapshot file
PPT/dashboard generated from snapshot
```

## Phase 4: Live execution timeline

```text
SSE/WebSocket event stream
sandbox command cards
file creation events
validation events
artifact preview ready events
```

## Phase 5: Advanced artifacts

```text
DOCX generation
dashboard generation
mini app generation
partial regeneration
version comparison
auto-update dashboards
```

## Phase 6: Hardening

```text
rootless Docker
Docker socket proxy
network allowlist
custom skill review
image scanning
resource quotas
audit evidence
```

---

# 25. What Zhanlu should copy from Claude Code

Copy these ideas:

```text
visible step-by-step progress
tool/action timeline
command output blocks
self-repair when command fails
file creation indicators
working directory concept
skill/documentation reading before execution
final artifact output
```

Do not copy unsafe parts:

```text
install arbitrary packages freely
allow unlimited shell
allow broad network
allow host filesystem access
allow direct database credentials
allow unreviewed code skills in production
```

Zhanlu should be:

```text
Claude-Code-like experience
+
enterprise governance
+
database-connected agents
+
artifact workspace
+
inline preview
+
sandbox safety
```

---

# 26. Final architecture principle

**Zhanlu should provide a Claude-Code-like live sandbox experience inside chat. When a user asks an agent to create a PPT, DOCX, dashboard, HTML page, mini app, or data-driven artifact, Synexia creates a governed PlanDAG and Layer 5 starts a sandbox job. The sandbox-worker launches a temporary Docker container with strict CPU, memory, time, filesystem, and network limits. The sandbox receives only approved input packages, such as skill package, template, DataSnapshot, chart assets, and build instructions, never raw credentials or unrestricted database access. Command execution, stdout/stderr, file creation, validation, and artifact preview events are streamed back to the chat UI as a live execution timeline. Outputs are validated, stored in PostgreSQL as versioned artifacts, and previewed inline through permission-checked APIs. Redis is used only for queues and temporary coordination. The sandbox filesystem is temporary and destroyed after execution.**

---

# 27. Implementation invariants

```text
SBX-0: Sandbox is a temporary execution workspace, not persistent storage.

SBX-1: Only sandbox-worker may create sandbox containers.

SBX-2: Backend, Synexia, and normal workers must not run user code directly.

SBX-3: Sandbox receives approved input packages only.

SBX-4: Sandbox must not receive raw database credentials or unrestricted database access.

SBX-5: Database-connected agent tasks must use DataSnapshots before sandbox artifact generation.

SBX-6: Sandbox network is disabled by default.

SBX-7: Sandbox filesystem is temporary and destroyed after execution.

SBX-8: Sandbox output is not trusted until validation passes.

SBX-9: All sandbox commands, outputs, errors, and generated files are recorded as execution events.

SBX-10: Inline chat progress must be backed by stored execution events, not only frontend animation.

SBX-11: Artifacts generated by sandbox must be stored as PostgreSQL-backed artifact versions.

SBX-12: Redis is only queue and coordination infrastructure, never source of truth.

SBX-13: High-risk sandbox capabilities require approval.

SBX-14: Mini apps and HTML previews must run in sandboxed iframes.

SBX-15: Every artifact must link back to execution_id, skill_version_id, source DataSnapshots, build manifest, and validation report.
```

---

# 28. Reference technologies to consider later

These are not required for MVP, but useful for future hardening:

```text
Docker Compose for first SSH deployment
Rootless Docker for safer container runtime
Docker socket proxy to reduce sandbox-worker risk
gVisor for stronger container sandboxing
Kata Containers for VM-backed container isolation
Firecracker-style microVMs for high-security isolation
E2B or Daytona-style managed sandboxes for agent code execution
Gotenberg or LibreOffice for document-to-PDF preview
ONLYOFFICE for future inline office editing
```

MVP recommendation:

```text
Start with Docker sandbox-worker + prebuilt sandbox images + PostgreSQL artifact storage + Redis queue.
Then harden the sandbox step by step.
```
