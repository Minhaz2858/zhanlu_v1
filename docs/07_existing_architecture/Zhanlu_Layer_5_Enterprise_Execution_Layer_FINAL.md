# Zhanlu Layer 5 — Enterprise Execution Layer

**Layer name:** Enterprise Execution Layer  
**Subtitle:** From Decision to Action · Durable · Governed · Human-in-the-Loop · Artifact-Aware

## 1. Core meaning

Layer 5 is the governed action layer of Zhanlu. It converts Synexia-approved plans into real enterprise work: workflows, automations, integrations, sandbox jobs, artifact builds, approvals, notifications, collaboration, exports, and delivery.

Layer 2 decides and plans. Layer 3 provides agents, skills, tools, MCP connectors, and data bindings. Layer 4 provides context, memory, knowledge, and DataSnapshots. Layer 5 executes the approved work. Layer 6 governs the execution. Layer 7 runs the Docker/PostgreSQL/Redis infrastructure.

Layer 5 should not replace Synexia reasoning. It should not own enterprise memory. It should not bypass policy. It should execute approved plan nodes safely, durably, audibly, and with human control.

## 2. Main principle

```text
Layer 5 executes approved plans.
Layer 5 does not invent unauthorized actions.
Layer 5 never bypasses identity, policy, memory, artifact, or audit governance.
```

Every action must be recorded, recoverable, permission-aware, and connected back to a user request, conversation, execution, plan node, agent, skill, and artifact.

## 3. Layer 5 responsibilities

Layer 5 is responsible for:

```text
Workflow execution
Automation execution
Sandbox execution
Skill execution coordination
MCP/tool execution coordination
Artifact generation
PPT/DOCX/PDF/XLSX/MD/HTML/dashboard/mini app building
Preview generation
Approval and control gates
Human collaboration
Notification delivery
Retry, recovery, and compensation
Execution events
Validation and quality assurance
Export, publish, and delivery actions
```

Layer 5 is not responsible for:

```text
User identity resolution
AI reasoning strategy
LLM provider access policy
Long-term memory truth
Direct credential storage
Raw datasource authorization
Permanent artifact storage on disk
```

## 4. Core engines

Layer 5 should contain the following execution engines:

```text
1. Workflow Engine
2. Automation Engine
3. Integration Hub
4. Sandbox Execution Engine
5. Artifact & App Execution Engine
6. Approval & Control Engine
7. Notification Engine
8. Human Collaboration Engine
9. Execution Governance
```

## 5. Workflow Engine

The Workflow Engine executes Synexia's PlanDAG.

It supports:

```text
plan-node execution
dependency management
state transitions
pause and resume
retry
timeout
cancel
repair
compensation
idempotency
replay
execution event emission
```

Recommended node-run types:

```text
agent_call
skill_run
tool_call
mcp_call
datasource_query
sandbox_job
artifact_build
preview_build
human_approval
notification
validation
export
publish
```

In Zhanlu v1, the workflow engine can be implemented with PostgreSQL as the source of truth and Redis as queue/lock/temporary coordination infrastructure. Temporal-like durable concepts should be used, but Temporal itself is optional in v1.

## 6. Automation Engine

The Automation Engine runs work later, repeatedly, or when a condition becomes true.

Trigger types:

```text
manual
scheduled
event_based
webhook
file_uploaded
datasource_changed
approval_completed
artifact_published
condition_watch
```

Examples:

```text
Generate a weekly finance report every Monday.
Refresh a sales dashboard every morning.
Summarize new uploaded documents.
Notify the manager when budget usage exceeds a threshold.
Publish PDF after approval.
```

Automation creates an execution just like a chat request, but the trigger is not necessarily a user message.

## 7. Integration Hub

The Integration Hub connects Zhanlu to external business systems through approved connectors and MCP servers.

Connector types:

```text
database connector
file connector
email connector
calendar connector
Slack/WeCom/DingTalk connector
ERP connector
CRM connector
HTTP API connector
webhook connector
RPA connector
MCP connector
```

Correct execution flow:

```text
Synexia PlanDAG
→ Layer 5 Integration Hub
→ Tool / Skill / MCP Gateway
→ Layer 6 policy and permission check
→ approved external action
→ ObservationRecord
→ audit event
```

No agent, skill, or MCP server should directly access secrets or bypass the Integration Hub.

## 8. Sandbox Execution Engine

The Sandbox Execution Engine runs user-created code skills, artifact builders, preview converters, dashboard builders, mini app builders, and validation tools in isolated temporary Docker containers.

Sandbox rules:

```text
Sandbox receives only approved input packages.
Sandbox receives handles, not secrets.
Sandbox network is disabled by default.
Sandbox has CPU, memory, time, process, and filesystem limits.
Sandbox filesystem is temporary.
Sandbox output is validated before storage.
Sandbox container is destroyed after execution.
```

Typical sandbox lifecycle:

```text
create sandbox_jobs row
queue sandbox job in Redis
sandbox-worker picks job
materialize approved skill package into temporary folder
materialize approved input package
start temporary Docker container
capture stdout/stderr
collect outputs
run validation
store outputs into PostgreSQL artifact tables
mark job completed or failed
delete temporary folder
remove container
emit execution event
```

## 9. Artifact & App Execution Engine

This engine builds modern AI outputs as governed artifacts.

Supported artifact types:

```text
pptx
docx
pdf
md
html
xlsx
chart
dashboard
mini_app
data_view
report
image
```

Builders:

```text
PPT Builder
DOCX Builder
Markdown Builder
HTML Builder
Dashboard Builder
Mini App Builder
Chart Builder
Preview Builder
Inline Editor
Version Manager
Validation Engine
Publishing Engine
Auto-Update Engine
```

Artifact lifecycle:

```text
draft → building → preview_ready → editing → validated → approved → published/exported
```

Generated outputs are stored as versioned artifacts. Inline chat preview is served through permission-checked APIs, not raw file paths.

## 10. PPT generation flow

When the user asks:

```text
Make a Q2 finance PPT.
```

Layer 5 executes:

```text
1. Start execution from Synexia PlanDAG.
2. Run governed-nl2sql skill if data is required.
3. Create DataSnapshots.
4. Generate charts.
5. Start PPT sandbox job.
6. Materialize approved PPT skill and template.
7. Build PPTX.
8. Convert PPTX to PDF preview and slide thumbnails.
9. Validate file, placeholders, sources, and formatting.
10. Store PPTX, preview PDF, thumbnails, build manifest, and validation report.
11. Attach artifact to assistant message.
12. Emit artifact.preview_ready event.
13. User previews, edits, regenerates, approves, downloads, or publishes.
```

Data-driven PPTs must cite DataSnapshots, not live mutable queries.

## 11. DOCX, MD, HTML, dashboard, and mini app execution

DOCX artifacts use report structure, sections, tables, figures, references, and templates. The DOCX builder generates DOCX and a PDF preview.

Markdown artifacts store Markdown source and rendered HTML preview. Markdown is useful for architecture docs, specs, reports, meeting notes, and README files.

HTML artifacts store HTML/CSS/JS source and preview inside a sandboxed iframe. Unsafe scripts and external network access are disabled unless explicitly allowed.

Dashboard artifacts are data-bound artifacts with definitions, cards, charts, filters, data views, refresh policy, and versioned outputs.

Mini app artifacts are generated interactive tools with source package, manifest, permission model, runtime session, sandbox preview, validation report, and publish state.

## 12. Dashboard automatic update

Dashboards can use three update modes:

```text
manual_refresh
scheduled_refresh
event_driven_refresh
```

Scheduled dashboard flow:

```text
Dashboard is published.
Refresh policy is stored.
Automation Engine schedules refresh.
Query engine creates a new DataSnapshot.
Dashboard renderer creates a new dashboard version.
Notification Engine alerts the user if important changes appear.
```

Auto-updated dashboards must use approved datasource bindings and approved metric definitions.

## 13. Approval & Control Engine

Approval is persistent execution state, not only a frontend button.

Approval is required for high-risk actions such as:

```text
external sending
publishing to app/shared workspace
writing to connected systems
deleting data
running code skill
using restricted datasource
using expensive model route
sharing artifact outside conversation
memory write to app/org scope
```

Approval states:

```text
pending
approved
rejected
expired
cancelled
escalated
```

If the browser closes, the execution remains paused in PostgreSQL and can resume later after approval.

## 14. Notification Engine

Notification Engine sends:

```text
chat event
in-app notification
email notification
approval request
task assignment
workflow completion
failure alert
budget alert
data-refresh alert
artifact-ready alert
```

Common events:

```text
execution.started
execution.node_started
execution.node_completed
execution.node_failed
execution.paused_for_approval
approval.requested
approval.approved
approval.rejected
sandbox.started
sandbox.completed
artifact.created
artifact.preview_ready
artifact.validation_failed
artifact.published
notification.sent
execution.completed
execution.failed
```

## 15. Human Collaboration Engine

Human Collaboration is different from approval.

Approval asks:

```text
Can this action continue?
```

Collaboration asks:

```text
Can people review, comment, assign, improve, and accept the work?
```

It supports:

```text
artifact comments
review notes
task assignment
expert review
version comparison
collaborative editing handoff
final acceptance
```

Example:

```text
Finance Agent creates PPT.
User asks CFO to review.
CFO comments on slide 4.
Agent regenerates slide 4 only.
User approves final version.
Artifact becomes app_shared.
```

## 16. Execution Governance

Layer 6 owns global governance services. Layer 5 applies them at execution time.

Execution Governance checks:

```text
Is this action allowed?
Does it require approval?
Is the correct role approving?
Is the datasource allowed for this agent?
Is the skill approved?
Is the MCP server approved?
Is sandbox required?
Did validation pass?
Can the artifact be shared?
Can this output become memory?
Should execution stop, retry, repair, or escalate?
```

Execution Governance is mandatory before side effects.

Side effects include:

```text
send email
publish artifact
write database
call external API
create shared memory
delete file
run code
share output
```

## 17. Database-first execution model

PostgreSQL is the source of truth for execution state.

Required tables:

```text
executions
execution_events
plan_runs
plan_node_runs
workflow_runs
automation_triggers
automation_runs
sandbox_jobs
skill_runs
tool_calls
mcp_calls
integration_calls
approval_requests
approval_decisions
notifications
collaboration_threads
artifact_build_jobs
artifact_validation_reports
artifact_delivery_jobs
retry_records
compensation_records
execution_audit_logs
```

Redis is only for:

```text
job queue
temporary lock
worker heartbeat
stream fanout
rate limit
```

If Redis is lost, Zhanlu may need to restart some workers, but it must not lose execution truth.

## 18. Docker-first implementation

For the Docker/PostgreSQL/Redis version, Layer 5 should run through these services:

```text
zhanlu-backend
  receives requests
  creates executions
  exposes APIs

zhanlu-synexia
  creates TaskSpec and PlanDAG
  decides next action

zhanlu-worker
  executes workflow nodes
  calls Tool/Skill/MCP Gateway
  writes execution events

zhanlu-sandbox-worker
  runs sandbox jobs in temporary Docker containers
  builds PPT/DOCX/PDF/XLSX/MD/HTML/dashboard/mini app outputs
  returns outputs

zhanlu-postgres
  stores all execution state and artifacts

zhanlu-redis
  queue, locks, worker heartbeat, temporary event fanout
```

## 19. Execution event format

Use a CloudEvents-like structure:

```json
{
  "id": "event_uuid",
  "type": "artifact.preview_ready",
  "source": "zhanlu.execution",
  "time": "2026-07-07T10:30:00Z",
  "org_id": "org_uuid",
  "app_id": "app_uuid",
  "conversation_id": "conversation_uuid",
  "execution_id": "execution_uuid",
  "node_run_id": "node_run_uuid",
  "data": {
    "artifact_id": "artifact_uuid",
    "artifact_version_id": "artifact_version_uuid",
    "preview_kind": "pdf"
  }
}
```

## 20. Invariants

```text
EXE-0: Layer 5 converts approved plans into governed actions.
EXE-1: Layer 5 executes; it does not replace Synexia reasoning.
EXE-2: PostgreSQL is the source of truth for execution state.
EXE-3: Redis is temporary queue, lock, and event coordination infrastructure only.
EXE-4: Every execution has execution_id, trace_id, plan_id, status, events, and audit records.
EXE-5: Every plan node execution is recorded as a node run.
EXE-6: Side-effect actions require policy evaluation before execution.
EXE-7: High-risk actions require approval before execution continues.
EXE-8: Approval is persistent execution state, not only frontend UI state.
EXE-9: Sandbox execution is required for user-created code skills and artifact-building jobs.
EXE-10: Sandbox filesystem is temporary and never authoritative.
EXE-11: Artifacts are not trusted until validation passes.
EXE-12: PPT/DOCX/PDF/XLSX/MD/HTML/dashboard/mini app outputs must have build manifests, validation reports, and source references.
EXE-13: Data-driven artifacts must cite DataSnapshots, not live mutable queries.
EXE-14: Execution events are emitted for chat UI, audit, debugging, and workflow recovery.
EXE-15: Failed nodes must follow retry, repair, escalation, or compensation policy.
EXE-16: External integrations use approved connectors and never expose secrets to agents or skills.
EXE-17: Human collaboration actions are versioned and auditable.
EXE-18: No execution output becomes shared knowledge or memory unless accepted, approved, or validated.
```

## 21. Final design principle

The Enterprise Execution Layer is Zhanlu's governed action layer. It converts Synexia-approved plans into durable workflows, automations, integrations, sandbox jobs, artifact builds, approvals, notifications, and human collaboration. It uses PostgreSQL as the source of truth for execution state, Redis only for temporary queues and coordination, Docker sandbox workers for controlled execution, and validation gates before artifacts or side effects are trusted. Every action is policy-checked, traceable, auditable, recoverable, and connected back to the chat, artifact, memory, and governance layers.
