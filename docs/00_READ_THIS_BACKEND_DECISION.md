# Zhanlu Backend Readiness Audit and Build Decision

## 1. Audit Scope

This audit checked the current handoff materials available in the final Zhanlu package family, especially:

- `Zhanlu_Claude_Code_Implementation_Handoff_v3.zip`, the full base implementation handoff package.
- `Zhanlu_Claude_Code_Implementation_Handoff_v3_6.zip`, the latest incremental package containing the newest UI/runtime decisions.
- The latest standalone architecture files in `/mnt/data`, including the datasource binding, sandbox, inline preview, slash skill, and main-agent/subagent specs.

## 2. Important Finding

`Zhanlu_Claude_Code_Implementation_Handoff_v3_6.zip` is **not a complete standalone backend build package**. It contains only the newest incremental specs:

1. File storage and inline preview architecture
2. Slash skill invocation UI and custom skill runtime
3. Docker sandbox setup runbook
4. Main agent and subagent UI architecture
5. Central datasource connector and agent-specific data binding

The complete base package is `Zhanlu_Claude_Code_Implementation_Handoff_v3.zip`, which contains the master Claude Code prompt, environment guide, UI integration guide, API contract, database schema, event stream contract, sandbox/artifact specs, MCP spec, MVP build scope, seed data, testing checklist, full layer architecture files, and starter agent/skill templates.

## 3. Decision

Use a merged package for backend development. Claude Code should **not** receive only `v3_6.zip`, because that would miss the base API, database, MVP, testing, and architecture files.

The correct build package must include:

- The full base `v3` implementation handoff package
- The latest `v3_6` specs
- This backend readiness decision file

## 4. Backend Build Decision

The backend can start now, but only as a **Phase 1 MVP**, not the full enterprise platform in one pass.

The correct Phase 1 target is:

1. Keep the existing UI. Do not rebuild the UI from zero.
2. Build FastAPI backend services matching the existing UI.
3. Implement PostgreSQL as source of truth.
4. Implement Redis for queues, locks, and temporary event fanout.
5. Implement users, apps/projects, conversations, and messages.
6. Implement main-agent and subagent data model.
7. Implement central datasource connector and agent-specific data binding.
8. Implement skill registry and slash `/` skill picker backend support.
9. Implement simple Synexia FSM with deterministic routing for MVP.
10. Implement artifact storage, message-artifact links, and inline preview APIs.
11. Implement sandbox-worker with Docker execution.
12. Implement live execution timeline events for the chat UI.
13. Implement basic Markdown, HTML, and simple PPT artifact generation first.
14. Implement permission checks for preview, download, regenerate, and datasource use.

## 5. What Is Covered Well

The handoff materials cover:

- Seven-layer Zhanlu architecture
- Synexia as the central orchestration brain
- Existing UI preservation rule
- Main agent and subagent architecture
- Central datasource connector model
- Agent-specific database binding
- Subagent delegated datasource access
- DataSnapshot pattern
- Skills and custom skills
- Slash `/` skill invocation
- MCP gateway concept
- Multi-agent and swarm concept
- Live sandbox workspace
- Docker sandbox setup
- File storage and inline preview
- PostgreSQL/Redis/MinIO storage strategy
- Artifact versioning and preview derivatives
- Event stream contract
- MVP scope
- Seed data and testing checklist
- Starter agent and skill templates

## 6. Remaining Information Needed From the Existing UI

Before Claude Code modifies the project, the user should provide or expose the existing UI file tree. The architecture says to keep the UI, but Claude Code still needs exact frontend files.

Needed from the current UI repository:

- Frontend root path
- Chat page/component path
- Chat input component path
- Message rendering component path
- Right-side preview panel path, if it exists
- Agent creation page path
- My Space / Databases & KB page path
- Existing API client file path
- Existing routing file path
- Existing state management pattern
- Existing styling system

Without this file map, Claude Code may create duplicate components instead of integrating with the existing UI.

## 7. Highest-Risk Implementation Areas

Claude Code must be extra careful with these areas:

1. **Docker socket security**: only sandbox-worker may access Docker.
2. **Datasource security**: sandbox must never receive raw database credentials.
3. **Agent database scope**: each agent can only use explicitly selected datasource handles.
4. **Preview security**: frontend must use permission-checked preview APIs, not raw file paths.
5. **PostgreSQL source of truth**: Redis is temporary only.
6. **Existing UI preservation**: do not rebuild the UI from zero.
7. **Main-agent rule**: user chats with the main agent; subagents work under it.
8. **Skills rule**: skills are executable capability packages, not independent free agents.
9. **Synexia rule**: every AI/model call flows through Synexia.

## 8. Final Recommendation

Proceed with backend development using the merged package. The implementation should begin with the database schema, API skeleton, existing UI integration, conversation system, main-agent/subagent model, central datasource connector, artifact storage, and sandbox-worker MVP.

Do not start by building all advanced features. Build Phase 1 first, then add MCP, swarm, dashboards, mini apps, full automation, and enterprise governance in later phases.

## 9. Final One-Sentence Decision

Zhanlu is ready for backend implementation if Claude Code receives the full merged handoff package and follows the Phase 1 MVP order while preserving the existing UI and enforcing PostgreSQL-first storage, agent-specific datasource binding, sandbox isolation, and inline artifact preview through permission-checked APIs.
