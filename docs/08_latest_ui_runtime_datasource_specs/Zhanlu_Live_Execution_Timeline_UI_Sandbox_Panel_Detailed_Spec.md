# Zhanlu Live Execution Timeline UI and Sandbox Panel — Detailed Implementation Spec

Version: v1.0  
Target reader: Claude Code / implementation agent  
Status: Required for Zhanlu MVP and future enterprise artifact generation

---

## 0. Purpose

Zhanlu must provide a Claude-Code-like execution experience inside the chatbot. When the user asks an agent to create or modify something, such as a PPT, DOCX, Markdown file, HTML page, dashboard, chart, or mini app, the user should be able to see the system working in a live, collapsible sandbox/workflow timeline.

The user should be able to hide the technical details, expand them, or view developer-level logs depending on permission and preference.

This feature is called:

```text
Live Execution Timeline
Sandbox Workspace Panel
```

It is not only a frontend animation. It is backed by real persisted backend execution events, sandbox events, command logs, artifact build jobs, and audit records.

---

## 1. Core product requirement

When a user sends a message such as:

```text
Make a Q2 finance PPT using my finance database.
```

Zhanlu should show the following flow inside the chat:

```text
Assistant:
I will create the PPT using your approved Finance Agent, finance datasource, and company PPT skill.

[Live Execution Timeline]                    [Hide]
✓ Understanding request
✓ Checking Finance Agent permissions
✓ Creating DataSnapshot from finance database
▶ Running PPT sandbox
  Command: python build_ppt.py
  Output: Created Q2_Finance_Report.pptx
✓ Generating PDF preview
✓ Validating artifact
✓ Storing artifact

[Artifact Card]
Q2 Finance Report.pptx
Preview · Edit · Regenerate · Download · Approve
```

The user can hide the execution timeline and still see the final artifact card.

---

## 2. Non-negotiable architecture rules

Claude Code must follow these rules exactly.

```text
RULE 1: The Live Execution Timeline is separate from the final Artifact Preview Card.
RULE 2: Hiding the timeline only changes the UI. Backend events must still be stored.
RULE 3: Execution events are persisted in PostgreSQL.
RULE 4: Redis is only for temporary event fanout, queues, locks, and worker coordination.
RULE 5: The frontend must never display raw secrets, credentials, full internal paths, API keys, or private system prompts.
RULE 6: Sandbox command logs must be role-filtered and redacted before display.
RULE 7: The backend is the authority for event visibility. The frontend may further hide or collapse events, but cannot upgrade visibility.
RULE 8: The sandbox timeline must support page refresh recovery by loading past events from PostgreSQL.
RULE 9: The sandbox-worker is the only service allowed to create Docker sandbox containers.
RULE 10: The chatbot should feel like Claude Code, but with enterprise governance, visibility filtering, artifact previews, and audit persistence.
```

---

## 3. User experience modes

The timeline must support three main user-facing modes.

### 3.1 Simple mode

Default for normal business users.

Show only short progress steps.

Example:

```text
Creating your PPT...
✓ Request understood
✓ Data prepared
✓ Slides generated
✓ Preview ready
```

Do not show commands, stdout, stderr, file paths, container IDs, or raw technical logs.

### 3.2 Detailed mode

For power users, analysts, and business users who want transparency.

Example:

```text
✓ Finance Agent selected
✓ Datasource permission checked
✓ DataSnapshot created from finance_postgres
✓ PPT skill package loaded
✓ Sandbox job completed
✓ PDF preview generated
```

May show safe metadata such as:

```text
DataSnapshot rows: 248
Artifact version: v1
Skill: company-finance-ppt v2
Template: company_finance_template_v3
```

### 3.3 Developer mode

For developers/admin users only.

Example:

```text
Command: python build_ppt.py
stdout:
WROTE /workspace/output/Q2_Finance_Report.pptx

stderr:
warning: optional font not found, fallback used
```

Developer mode may show command display strings and sanitized stdout/stderr. It must not show secrets, hidden prompts, credentials, internal absolute host paths, Docker socket paths, environment variables, or private tenant data beyond the user's permissions.

---

## 4. Frontend user controls

Each `LiveExecutionTimeline` component should support these actions:

```text
Hide timeline
Show timeline
Show details
Hide details
Expand all
Collapse all
Switch to simple mode
Switch to detailed mode
Switch to developer mode, if user has permission
Copy safe logs, developer/admin only
Retry failed step, if backend allows
Open trace view, admin/developer only
```

Important behavior:

```text
Hide = hides from UI only.
Collapse = keeps header visible but hides details.
Simple = shows business-level steps only.
Detailed = shows step-level progress.
Developer = shows safe command-level logs.
```

---

## 5. Frontend component structure

Add or adapt these components in the existing UI.

```text
frontend/src/features/chat/components/
  AssistantMessage.tsx
  LiveExecutionTimeline.tsx
  ExecutionTimelineHeader.tsx
  ExecutionStep.tsx
  ExecutionStepGroup.tsx
  SandboxCommandBlock.tsx
  SandboxLogStream.tsx
  ArtifactPreviewCard.tsx
  ArtifactActions.tsx
  ArtifactVersionBadge.tsx
```

Recommended React structure:

```tsx
<AssistantMessage message={message}>
  <AssistantText content={message.content} />

  {message.execution_id && (
    <LiveExecutionTimeline
      executionId={message.execution_id}
      defaultCollapsed={userPreferences.executionTimelineDefault === "collapsed"}
      defaultHidden={userPreferences.executionTimelineDefault === "hidden"}
      mode={userPreferences.executionTimelineMode ?? "simple"}
      currentUserRole={currentUser.role}
    />
  )}

  {message.artifacts?.map((artifact) => (
    <ArtifactPreviewCard
      key={artifact.id}
      artifact={artifact}
      onPreview={() => openArtifactPreview(artifact.id)}
      onRegenerate={() => regenerateArtifact(artifact.id)}
      onApprove={() => approveArtifact(artifact.id)}
      onDownload={() => downloadArtifact(artifact.id)}
    />
  ))}
</AssistantMessage>
```

---

## 6. Timeline UI states

The timeline should handle the following states.

```text
idle
starting
running
paused_for_approval
repairing
completed
failed
cancelled
expired
```

Example labels:

```text
starting: Preparing execution
running: Working in sandbox
paused_for_approval: Waiting for approval
repairing: Repairing failed step
completed: Completed
failed: Failed
cancelled: Cancelled
```

---

## 7. Event model

All live timeline updates should come from backend events.

### 7.1 Required event fields

Every execution event should include:

```json
{
  "id": "event_uuid",
  "event_type": "sandbox.command_started",
  "org_id": "org_uuid",
  "app_id": "app_uuid",
  "conversation_id": "conversation_uuid",
  "message_id": "assistant_message_uuid_or_null",
  "execution_id": "execution_uuid",
  "node_run_id": "node_run_uuid_or_null",
  "sandbox_job_id": "sandbox_job_uuid_or_null",
  "artifact_id": "artifact_uuid_or_null",
  "title": "Build PPTX file",
  "summary": "Running PPT generation script",
  "payload": {},
  "visibility": "business",
  "severity": "info",
  "status": "running",
  "created_at": "2026-07-08T10:30:00Z"
}
```

### 7.2 Event visibility levels

Use these levels:

```text
public      safe for all users in this conversation
business    safe business details, no command logs
technical   safe technical details, no raw secrets
admin       admin-only operational details
hidden      never show in chat, store for audit only
```

Visibility rules:

```text
Normal user: public, business
Power user: public, business, technical if allowed
Developer: public, business, technical
Admin: public, business, technical, admin
No UI user: hidden
```

The backend must filter event visibility before sending events to frontend.

### 7.3 Severity levels

```text
info
success
warning
error
blocked
approval_required
```

### 7.4 Event types

Required event types:

```text
execution.started
execution.step_started
execution.step_completed
execution.step_failed
execution.paused_for_approval
execution.resumed
execution.repair_started
execution.repair_completed
execution.completed
execution.failed
execution.cancelled

agent.selected
agent.permission_checked
skill.selected
skill.preflight_started
skill.preflight_completed
skill.preflight_blocked

datasource.permission_checked
datasource.query_validated
data_snapshot.created

sandbox.job_created
sandbox.started
sandbox.file_materialized
sandbox.command_started
sandbox.command_stdout
sandbox.command_stderr
sandbox.command_completed
sandbox.validation_started
sandbox.validation_completed
sandbox.completed
sandbox.failed

artifact.build_started
artifact.created
artifact.preview_started
artifact.preview_ready
artifact.validation_started
artifact.validation_completed
artifact.validation_failed
artifact.stored
artifact.attached_to_message

approval.requested
approval.approved
approval.rejected

notification.sent
```

---

## 8. Example event payloads

### 8.1 Execution started

```json
{
  "event_type": "execution.started",
  "title": "Creating Q2 Finance PPT",
  "summary": "Finance Agent is preparing a presentation from approved finance data.",
  "visibility": "public",
  "severity": "info",
  "status": "running",
  "payload": {
    "task_kind": "artifact_generation",
    "artifact_type": "pptx"
  }
}
```

### 8.2 DataSnapshot created

```json
{
  "event_type": "data_snapshot.created",
  "title": "Data snapshot created",
  "summary": "A read-only finance data snapshot was created for the PPT.",
  "visibility": "business",
  "severity": "success",
  "status": "completed",
  "payload": {
    "data_snapshot_id": "snapshot_uuid",
    "datasource_label": "Finance Database",
    "row_count": 248,
    "tables_used": ["revenue", "cost", "budget"]
  }
}
```

### 8.3 Sandbox command started

```json
{
  "event_type": "sandbox.command_started",
  "title": "Build PPTX file",
  "summary": "Running the PPT generation script inside a temporary sandbox.",
  "visibility": "technical",
  "severity": "info",
  "status": "running",
  "payload": {
    "command_display": "python build_ppt.py",
    "runtime_image": "zhanlu-sandbox-pptx:1.0"
  }
}
```

### 8.4 Sandbox stdout

```json
{
  "event_type": "sandbox.command_stdout",
  "title": "Command output",
  "summary": "PPTX file created.",
  "visibility": "technical",
  "severity": "info",
  "status": "running",
  "payload": {
    "text": "WROTE Q2_Finance_Report.pptx"
  }
}
```

### 8.5 Artifact preview ready

```json
{
  "event_type": "artifact.preview_ready",
  "title": "Preview ready",
  "summary": "Q2 Finance Report.pptx is ready to preview.",
  "visibility": "public",
  "severity": "success",
  "status": "completed",
  "payload": {
    "artifact_id": "artifact_uuid",
    "artifact_version_id": "artifact_version_uuid",
    "artifact_type": "pptx",
    "preview_kind": "pdf",
    "actions": ["preview", "edit", "regenerate", "download", "approve"]
  }
}
```

---

## 9. Backend API requirements

### 9.1 Load historical timeline

```http
GET /api/v1/executions/{execution_id}/events
```

Query parameters:

```text
mode=simple|detailed|developer
since_event_id=optional
limit=optional
```

Response:

```json
{
  "execution_id": "exec_uuid",
  "status": "running",
  "events": []
}
```

The backend must filter events based on user permission and requested mode.

### 9.2 Live event stream

Use SSE or WebSocket.

Recommended MVP:

```http
GET /api/v1/executions/{execution_id}/events/stream
```

SSE event example:

```text
event: sandbox.command_stdout
data: {"id":"event_uuid","text":"WROTE Q2_Finance_Report.pptx"}
```

Alternative existing conversation stream can multiplex execution events:

```http
GET /api/v1/conversations/{conversation_id}/stream
```

### 9.3 User preference API

```http
GET /api/v1/users/me/preferences
PATCH /api/v1/users/me/preferences
```

Preference keys:

```json
{
  "execution_timeline_default": "compact",
  "execution_timeline_mode": "simple",
  "show_developer_logs": false
}
```

---

## 10. Database requirements

### 10.1 execution_events

```sql
CREATE TABLE execution_events (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    conversation_id UUID,
    message_id UUID,
    execution_id UUID NOT NULL,
    node_run_id UUID,
    sandbox_job_id UUID,
    artifact_id UUID,
    event_type TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    payload JSONB NOT NULL DEFAULT '{}',
    visibility TEXT NOT NULL DEFAULT 'business',
    severity TEXT NOT NULL DEFAULT 'info',
    status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_execution_events_execution_id_created_at
ON execution_events (execution_id, created_at);

CREATE INDEX idx_execution_events_conversation_id_created_at
ON execution_events (conversation_id, created_at);
```

### 10.2 sandbox_command_logs

```sql
CREATE TABLE sandbox_command_logs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    sandbox_job_id UUID NOT NULL,
    command_display TEXT NOT NULL,
    stdout_redacted TEXT,
    stderr_redacted TEXT,
    exit_code INT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 10.3 user_preferences

```sql
CREATE TABLE user_preferences (
    user_id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    preferences JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 11. Redaction and safety requirements

Before sending any log or event to frontend, backend must redact:

```text
API keys
JWT tokens
database URLs
passwords
private keys
OAuth tokens
full environment variables
Docker socket paths
host absolute paths
internal service credentials
private system prompts
raw stack traces containing secrets
```

Use placeholder:

```text
[REDACTED]
```

Path redaction example:

```text
/tmp/zhanlu_sandbox/job_123/output/Q2_Finance_Report.pptx
→ Q2_Finance_Report.pptx
```

Database URL redaction example:

```text
postgres://user:password@postgres:5432/zhanlu
→ postgres://[REDACTED]@postgres:5432/zhanlu
```

---

## 12. UI rendering rules

### 12.1 Event grouping

The frontend should group low-level events under high-level steps.

Example:

```text
Step: Running PPT sandbox
  sandbox.started
  sandbox.command_started
  sandbox.command_stdout
  sandbox.command_completed
  sandbox.completed
```

### 12.2 Icons/status

Use simple symbols or UI icons:

```text
pending: ○
running: ▶
success: ✓
warning: ⚠
failed: ✗
paused: ⏸
repairing: ↻
blocked: ⛔
```

### 12.3 Default collapsed behavior

Default for normal users:

```text
Timeline shown in compact mode for artifact tasks.
Detailed logs collapsed.
Raw command logs hidden.
```

Default for developer/admin:

```text
Timeline shown in detailed mode.
Command blocks collapsed but available.
```

---

## 13. Failure and self-repair UI

When a sandbox command fails, the timeline should show a clear repair sequence.

Example:

```text
✗ Build PPT failed
  Reason: chart image missing

↻ Repairing artifact build
  Recreated chart image from DataSnapshot

▶ Running PPT build again
✓ PPTX generated
```

Backend events:

```text
execution.step_failed
execution.repair_started
sandbox.command_started
sandbox.command_completed
execution.repair_completed
artifact.preview_ready
```

The frontend should show failed steps in the timeline, not hide them.

---

## 14. Approval pause UI

If the execution requires approval:

```text
⏸ Waiting for approval
This action will publish the dashboard to the Finance App workspace.

[Approve] [Reject]
```

Approval events:

```text
approval.requested
execution.paused_for_approval
approval.approved
execution.resumed
approval.rejected
execution.cancelled
```

Approval is backend execution state, not only a frontend modal.

---

## 15. Artifact card relationship

The Artifact Card appears when `artifact.created` or `artifact.preview_ready` is emitted.

Artifact card should show:

```text
artifact title
artifact type
status
version
created by agent
created by skill
source count
available actions
```

Example:

```text
Q2 Finance Report.pptx
Preview ready · Version v1
Created by Finance Agent using Company Finance PPT Skill
Sources: 2 DataSnapshots, 1 template

Preview · Edit · Regenerate · Download · Approve
```

The Artifact Card uses artifact APIs, not the execution event stream, to load preview data.

---

## 16. Sandbox event generation responsibilities

### 16.1 sandbox-worker must emit events for:

```text
job accepted
input materialized
container started
command started
stdout line received
stderr line received
command completed
output file detected
validation started
validation completed
artifact bytes returned
container removed
temp folder cleaned
job completed or failed
```

### 16.2 backend must emit events for:

```text
message saved
request envelope sealed
execution created
agent selected
skill selected
artifact created
artifact stored
artifact preview ready
assistant message updated
```

### 16.3 frontend must render:

```text
assistant text
timeline event cards
command blocks
artifact preview cards
approval cards
error/repair cards
```

---

## 17. User preference persistence

If user hides the sandbox timeline, save preference locally first. Optionally sync to backend.

Local storage key:

```text
zhanlu.executionTimeline.default
```

Backend preference:

```json
{
  "execution_timeline_default": "hidden",
  "execution_timeline_mode": "simple"
}
```

Allowed values:

```text
execution_timeline_default: hidden | compact | expanded
execution_timeline_mode: simple | detailed | developer
```

---

## 18. MVP implementation plan for Claude Code

Build in this order.

### Phase 1: Event storage

```text
Create execution_events table.
Create backend service to append events.
Create backend API to list events.
```

### Phase 2: Frontend timeline component

```text
Create LiveExecutionTimeline component.
Render mocked events.
Support hide/show/collapse.
Support simple/detailed/developer modes.
```

### Phase 3: Live streaming

```text
Add SSE or WebSocket event stream.
Connect frontend timeline to live events.
Load historical events on refresh.
```

### Phase 4: Sandbox integration

```text
sandbox-worker writes events while running job.
Command stdout/stderr events appear in developer mode.
Events are redacted before sending to UI.
```

### Phase 5: Artifact connection

```text
artifact.preview_ready event creates ArtifactPreviewCard.
Preview API displays generated output.
```

### Phase 6: Approval and repair UI

```text
Render approval cards.
Render failed and repaired steps.
Allow retry only through backend-approved action.
```

---

## 19. Acceptance tests

Claude Code must ensure these tests pass.

```text
1. User asks for a Markdown artifact; timeline appears.
2. User clicks Hide; timeline disappears but artifact remains.
3. Refresh page; previous timeline can be loaded from PostgreSQL.
4. Normal user cannot see command_stdout events if visibility is technical.
5. Developer user can see sanitized command_stdout events.
6. No secret-like values appear in timeline.
7. Sandbox failure appears as failed step.
8. Repair attempt appears as repairing step.
9. artifact.preview_ready creates an ArtifactPreviewCard.
10. Redis restart does not erase historical execution events.
11. Backend filters event visibility before frontend receives data.
12. User preference for compact/hidden mode persists.
```

---

## 20. Exact instruction to Claude Code

Claude Code must implement this feature as a first-class part of the chat experience.

```text
Implement a collapsible Live Execution Timeline inside assistant chat messages. It must receive persisted backend execution events, support hide/show, simple/detailed/developer modes, render sandbox steps and command logs like Claude Code, and remain separate from the final Artifact Preview Card. The backend must store all events in PostgreSQL, stream live events to the frontend, filter visibility by user role, and redact secrets before display. The sandbox-worker must emit events while running Docker sandbox jobs. Hiding the timeline must only affect the UI, not audit or event storage.
```

---

## 21. Final design principle

Zhanlu chat should feel like a modern AI workspace, not only a text chatbot. When agents create files, dashboards, mini apps, or data-driven outputs, users should see a safe, collapsible, Claude-Code-like execution timeline. Normal users see simple progress. Business users see transparent task steps. Developers and admins see sanitized command logs. The final output appears as an artifact card with inline preview and actions. All events are persisted, permission-filtered, and auditable.
