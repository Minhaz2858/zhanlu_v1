# Zhanlu MVP Build Scope

## Purpose

The full Zhanlu architecture is large. The MVP must prove the core loop:

```text
User chat → Synexia plan → execution → sandbox/artifact → PostgreSQL storage → inline preview → user edit/regenerate/download
```

## MVP Phase 1: backend foundation

Implement:

- FastAPI backend on port 5002.
- PostgreSQL connection and migrations.
- Redis connection.
- Simple JWT auth or development auth.
- Organization, app, and user seed data.
- Conversations and messages.
- RequestEnvelope creation.
- Audit log base table.

Do not implement full SSO/RBAC/ABAC yet. Use simple role fields and clean interfaces so enterprise auth can replace it later.

## MVP Phase 2: Synexia simple FSM

Implement a minimal Synexia flow:

```text
INIT → GOAL → PLAN → GATE → ACT → OBSERVE → VERIFY → FINALIZE → DONE
```

Synexia should support these task kinds:

- normal_chat
- markdown_artifact
- html_artifact
- pptx_artifact_basic
- docx_artifact_basic optional

All model calls must go through `backend/synexia/brain_client.py`.

## MVP Phase 3: registry foundation

Implement:

- agent_profiles
- agent_versions
- skill_profiles
- skill_versions
- agent_skill_bindings
- simple seed system agents
- simple seed system skills

Agents are harness profiles. Skills are governed capability packages.

## MVP Phase 4: artifact system

Implement:

- artifacts
- artifact_versions
- artifact_blobs
- artifact_previews
- message_artifacts
- artifact_interactions
- artifact_validation_reports

MVP artifact types:

- md
- html
- pptx_basic

Preview:

- MD → rendered HTML.
- HTML → sandboxed iframe or safe rendered preview.
- PPTX → generated PDF preview if converter available, otherwise slide thumbnails or file card with download as fallback.

## MVP Phase 5: live execution timeline

Implement event streaming:

- SSE first, WebSocket optional.
- Event rows stored in PostgreSQL.
- Redis may fan out events temporarily.

MVP events:

- message.created
- execution.started
- execution.node_started
- execution.node_completed
- sandbox.started
- sandbox.command_started
- sandbox.command_stdout
- sandbox.command_stderr
- sandbox.command_completed
- artifact.created
- artifact.preview_ready
- execution.completed
- execution.failed

## MVP Phase 6: sandbox-worker

Implement:

- sandbox_jobs table.
- Redis queue.
- sandbox-worker service.
- temporary Docker container execution.
- strict resource limits.
- network disabled by default.
- input folder read-only.
- output folder writable.
- output copied back into PostgreSQL.
- temp folder cleanup.

## MVP Phase 7: PPT creation

Basic PPT generation should be possible.

Preferred MVP path:

- use Python `python-pptx` or Node `pptxgenjs`, whichever fits current stack better.
- use prebuilt sandbox image with dependencies installed.
- generate a 5-10 slide deck from a JSON outline.
- create artifact record.
- create preview if converter exists.
- attach artifact to chat.

## Not MVP yet

Delay unless existing UI requires it:

- full MCP connector system,
- complex multi-agent swarm,
- external publishing,
- enterprise SSO,
- full dashboard auto-refresh,
- mini app publishing,
- granular ABAC/ReBAC,
- full model governance,
- all custom skill creation flows,
- gVisor/Kata/Firecracker isolation.

## MVP success criteria

MVP is successful when:

1. User sends chat message.
2. Backend stores message.
3. Synexia creates an execution.
4. Execution streams timeline events.
5. Sandbox creates at least one artifact.
6. Artifact is stored in PostgreSQL.
7. Chat shows artifact preview card.
8. User can download artifact.
9. Server restart does not delete messages/artifacts.
10. Redis restart does not corrupt permanent state.
