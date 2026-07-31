# CLAUDE CODE MASTER BUILD PROMPT

You are building the Zhanlu Enterprise AI Operating System from the user's existing UI and this architecture handoff package.

## Your first task

Before editing code, read all Markdown files in this package and summarize your build plan. Do not start implementation until you have identified:

- existing UI routes and components,
- missing backend APIs,
- database schema/migrations needed,
- Docker Compose services needed,
- MVP scope boundaries,
- any ambiguity that blocks implementation.

## Highest priority rules

1. Use the user's existing UI. Do not rebuild the whole frontend.
2. Build the backend to match the UI, not the other way around.
3. Synexia is the only AI orchestration layer.
4. No feature module, agent, skill, MCP connector, worker, or sandbox may call a model provider directly.
5. PostgreSQL is the source of truth for persistent data.
6. Redis is only for queues, locks, temporary events, worker heartbeat, rate limits, and cache.
7. Docker container filesystems are temporary.
8. Sandbox filesystem is temporary.
9. No persistent business data should be stored permanently in `backend/uploads`, `server/files`, `sandbox/tmp`, or random project folders.
10. User-created agents and skills must be stored in PostgreSQL as package/version records.
11. Built-in templates may live in `agent_library/` and `skill_library/` and should be seeded into PostgreSQL.
12. The sandbox-worker is the only service allowed to create temporary sandbox containers.
13. Backend, Synexia, normal workers, and frontend must not access Docker socket.
14. Sandbox must receive only approved input packages, such as DataSnapshots, templates, skill package, and build instruction JSON.
15. Sandbox must not receive raw enterprise database credentials.
16. Artifact preview must be served through permission-checked APIs, never raw file paths.
17. Every execution should emit events for the chat timeline.
18. Every artifact must have metadata, version, blobs/previews, build manifest, validation report, and source references.

## Build philosophy

Build a working MVP first, then expand.

Do not implement all advanced features at once. Start with:

- auth/simple JWT,
- organizations/apps/conversations/messages,
- chat streaming endpoint,
- simple Synexia FSM,
- agent registry,
- skill registry,
- artifact storage,
- Markdown/HTML artifact generation,
- basic PPTX generation,
- artifact preview APIs,
- Redis worker queue,
- Docker sandbox-worker,
- live execution timeline.

MCP, multi-agent, swarms, dashboards, mini apps, enterprise governance, advanced approval flows, and full custom skill creation should be Phase 2/3 unless required by existing UI.

## Final architecture map

```text
Layer 1: Interaction & Identity
Layer 2: Synexia Cognitive Core
Layer 3: Harness Agent, Skill & Data Runtime
Layer 4: Memory, Knowledge & Context Intelligence
Layer 5: Enterprise Execution Layer
Layer 6: Enterprise Platform Services
Layer 7: Docker/PostgreSQL/Redis Infrastructure
```

## Port map

```text
Frontend: 5152
Backend FastAPI: 5002
Synexia Agent/Core: 8643
PostgreSQL: internal only
Redis: internal only
Nginx: 80/443 public
```

## Required project layout

Use the project layout from `Zhanlu_Project_Structure_and_Data_Storage_Model.md`. The key folders are:

```text
frontend/
backend/
backend/synexia/
backend/agents_runtime/
backend/skills_runtime/
backend/mcp_gateway/
backend/execution/
backend/artifacts/
backend/memory_knowledge/
backend/sandbox/
agent_library/
skill_library/
policies/
prompts/
infra/
```

## How to report progress

When implementing, report in phases:

1. Files inspected.
2. Database migrations created.
3. APIs implemented.
4. Worker/sandbox implemented.
5. UI integration points connected.
6. Tests run.
7. Remaining gaps.

Do not hide failures. If a feature cannot be implemented safely, create a clear TODO with reason.
