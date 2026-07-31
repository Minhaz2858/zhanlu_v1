# Zhanlu Event Stream Contract

## Purpose

The event stream powers the Claude-Code-like live timeline in the Zhanlu chat UI.

It must show:

- assistant text deltas,
- execution progress,
- plan node progress,
- sandbox command output,
- artifact creation,
- preview readiness,
- approval pauses,
- failures and retries.

## Transport

MVP should use Server-Sent Events from:

```text
POST /api/v1/chat/stream
```

Future may add WebSocket:

```text
GET /api/v1/ws/conversations/{conversation_id}
```

## Base event envelope

Every event should follow this shape:

```json
{
  "event_id": "uuid",
  "event_type": "execution.node_started",
  "org_id": "uuid",
  "app_id": "uuid",
  "conversation_id": "uuid",
  "message_id": "uuid-or-null",
  "execution_id": "uuid-or-null",
  "node_run_id": "uuid-or-null",
  "created_at": "2026-07-08T00:00:00Z",
  "data": {}
}
```

## Required event types

### `message.created`

```json
{
  "event_type": "message.created",
  "data": {
    "message_id": "uuid",
    "role": "assistant",
    "content": ""
  }
}
```

### `message.delta`

Used for assistant streaming text.

```json
{
  "event_type": "message.delta",
  "data": {
    "message_id": "uuid",
    "delta": "I will create the deck..."
  }
}
```

### `message.completed`

```json
{
  "event_type": "message.completed",
  "data": {
    "message_id": "uuid"
  }
}
```

### `execution.started`

```json
{
  "event_type": "execution.started",
  "data": {
    "execution_id": "uuid",
    "task_kind": "pptx_artifact",
    "title": "Create Q2 Sales PPT"
  }
}
```

### `execution.node_started`

```json
{
  "event_type": "execution.node_started",
  "node_run_id": "uuid",
  "data": {
    "node_key": "build_ppt",
    "node_type": "sandbox_job",
    "title": "Build PPTX in sandbox"
  }
}
```

### `execution.node_completed`

```json
{
  "event_type": "execution.node_completed",
  "node_run_id": "uuid",
  "data": {
    "node_key": "build_ppt",
    "status": "completed",
    "summary": "PPTX generated successfully."
  }
}
```

### `execution.node_failed`

```json
{
  "event_type": "execution.node_failed",
  "node_run_id": "uuid",
  "data": {
    "node_key": "build_ppt",
    "error_code": "SANDBOX_COMMAND_FAILED",
    "message": "Build script exited with code 1.",
    "retryable": true
  }
}
```

### `sandbox.started`

```json
{
  "event_type": "sandbox.started",
  "data": {
    "sandbox_job_id": "uuid",
    "runtime_image": "zhanlu-sandbox-pptx:latest",
    "network_policy": "none"
  }
}
```

### `sandbox.command_started`

```json
{
  "event_type": "sandbox.command_started",
  "data": {
    "sandbox_job_id": "uuid",
    "command_id": "uuid",
    "title": "Run PPT build script",
    "command_display": "python build_ppt.py"
  }
}
```

### `sandbox.command_stdout`

```json
{
  "event_type": "sandbox.command_stdout",
  "data": {
    "command_id": "uuid",
    "text": "WROTE /workspace/output/Q2_Sales_Report.pptx\n"
  }
}
```

### `sandbox.command_stderr`

```json
{
  "event_type": "sandbox.command_stderr",
  "data": {
    "command_id": "uuid",
    "text": "Warning: missing font, using fallback.\n"
  }
}
```

### `sandbox.command_completed`

```json
{
  "event_type": "sandbox.command_completed",
  "data": {
    "command_id": "uuid",
    "exit_code": 0,
    "duration_ms": 1234
  }
}
```

### `artifact.created`

```json
{
  "event_type": "artifact.created",
  "data": {
    "artifact_id": "uuid",
    "artifact_version_id": "uuid",
    "artifact_type": "pptx",
    "title": "Q2 Sales Report.pptx",
    "status": "created"
  }
}
```

### `artifact.preview_ready`

```json
{
  "event_type": "artifact.preview_ready",
  "data": {
    "artifact_id": "uuid",
    "artifact_version_id": "uuid",
    "artifact_type": "pptx",
    "title": "Q2 Sales Report.pptx",
    "preview_kind": "pdf",
    "actions": ["preview", "download", "regenerate", "approve"]
  }
}
```

### `approval.required`

```json
{
  "event_type": "approval.required",
  "data": {
    "confirmation_request_id": "uuid",
    "risk_tier": "medium",
    "required_role": "admin",
    "message": "Publishing this artifact to the app workspace requires approval."
  }
}
```

### `execution.completed`

```json
{
  "event_type": "execution.completed",
  "data": {
    "execution_id": "uuid",
    "status": "completed"
  }
}
```

### `execution.failed`

```json
{
  "event_type": "execution.failed",
  "data": {
    "execution_id": "uuid",
    "error_code": "EXECUTION_FAILED",
    "message": "The execution failed after retry."
  }
}
```

## Persistence rule

All important events must be stored in PostgreSQL in `execution_events`. Redis may be used only for temporary fanout.

If Redis restarts, the UI can reload the timeline from PostgreSQL.

## UI rendering guidance

- `message.delta`: append to assistant message bubble.
- `execution.*`: render timeline steps.
- `sandbox.command_*`: render command cards.
- `artifact.preview_ready`: render ArtifactPreviewCard.
- `approval.required`: render confirmation card.
- `execution.failed`: render error card with retry button if allowed.
