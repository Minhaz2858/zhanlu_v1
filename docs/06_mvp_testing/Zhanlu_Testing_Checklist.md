# Zhanlu Testing Checklist

## Basic system tests

- [ ] Docker Compose starts all services.
- [ ] Backend health check passes.
- [ ] PostgreSQL connection works.
- [ ] Redis connection works.
- [ ] Frontend opens existing UI.
- [ ] User can log in.
- [ ] Default app appears.

## Chat tests

- [ ] User can create a conversation.
- [ ] User message is stored in PostgreSQL.
- [ ] Assistant message is stored in PostgreSQL.
- [ ] RequestEnvelope is created.
- [ ] Execution is created.
- [ ] Execution events are stored.
- [ ] SSE stream shows events in UI.

## Artifact tests

- [ ] Markdown artifact can be created.
- [ ] HTML artifact can be created.
- [ ] Basic PPTX artifact can be created.
- [ ] Artifact metadata is stored in `artifacts`.
- [ ] Artifact version is stored in `artifact_versions`.
- [ ] Artifact blob is stored in `artifact_blobs`.
- [ ] Artifact is linked to assistant message through `message_artifacts`.
- [ ] Preview API works.
- [ ] Download API works.

## Sandbox tests

- [ ] Sandbox job row is created.
- [ ] Redis queue receives sandbox job.
- [ ] Sandbox-worker picks job.
- [ ] Temporary input folder is created.
- [ ] Temporary output folder is created.
- [ ] Sandbox container runs as non-root.
- [ ] Sandbox network is disabled by default.
- [ ] Sandbox cannot access host filesystem.
- [ ] Sandbox cannot access Docker socket.
- [ ] Sandbox respects CPU/memory/time limits.
- [ ] Output is copied into PostgreSQL.
- [ ] Temp folder is deleted after success.
- [ ] Temp folder is cleaned after failure.

## Security tests

- [ ] User cannot access another app's conversation.
- [ ] User cannot access another app's artifact.
- [ ] Skill not bound to selected agent is blocked.
- [ ] Datasource not bound to selected agent is blocked.
- [ ] Sandbox does not receive database credentials.
- [ ] Raw file paths are not exposed in preview API.
- [ ] Redis restart does not delete messages/artifacts.
- [ ] Backend restart preserves execution/artifact state.

## Event tests

- [ ] `execution.started` emitted.
- [ ] `execution.node_started` emitted.
- [ ] `sandbox.command_started` emitted.
- [ ] `sandbox.command_stdout` emitted.
- [ ] `sandbox.command_completed` emitted.
- [ ] `artifact.created` emitted.
- [ ] `artifact.preview_ready` emitted.
- [ ] `execution.completed` emitted.
- [ ] Failure emits `execution.failed`.

## Regression tests before Phase 2

- [ ] Existing UI still loads.
- [ ] Existing UI chat input works.
- [ ] Artifact preview card does not break layout.
- [ ] Database migrations run from empty DB.
- [ ] Seed data script is idempotent.
- [ ] Docker Compose can be rebuilt from scratch.
