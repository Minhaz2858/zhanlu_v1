# Zhanlu Final Coding Agent Resource Package

Read this file first.

## Final implementation decision

Use the user's existing UI. Do not rebuild the frontend from zero. Build the backend and connect it to the current UI.

## Final product rules

1. Synexia is the only AI orchestration brain.
2. Users chat with a main agent.
3. Main agents may delegate to subagents under Synexia control.
4. Users select high-level capabilities, not raw internal skills.
5. Raw skills exist in backend and advanced admin settings only.
6. Users connect databases once in My Space / Databases & KB.
7. Each main agent can use only the datasource(s) explicitly selected by the user.
8. Subagents inherit no database access by default; access is delegated by the main agent.
9. All database access goes through Datasource Gateway and creates DataSnapshots.
10. Sandbox never receives raw database credentials.
11. Generated files are Artifacts, not chat text attachments.
12. Inline preview uses preview derivatives and permission-checked APIs.
13. PostgreSQL is the source of truth.
14. Redis is temporary queue/cache/locks/events only.
15. Sandbox filesystems are temporary and destroyed after execution.

## Build order

1. Inspect the existing UI file tree and map components.
2. Add backend FastAPI skeleton, database, Redis, and config.
3. Add auth/mock auth, apps/projects, conversations, and messages.
4. Add main agent/subagent backend tables and APIs.
5. Add capability registry and replace raw skills UI with core capabilities for normal users.
6. Add central datasource connector and per-agent datasource bindings.
7. Add Synexia simple FSM and PlanDAG execution records.
8. Add skill registry and slash `/` action picker.
9. Add artifact storage and inline preview APIs.
10. Add live execution timeline events.
11. Add sandbox-worker and Docker sandbox execution.
12. Add PPT/DOCX/MD/HTML/dashboard generation incrementally.

## Start with MVP

Do not implement all advanced features in the first pass. Phase 1 should include:

- Existing UI integration
- Main agent creation
- Subagent binding
- Capability selection instead of raw skills
- Central datasource connector
- Agent-specific datasource binding
- Chat with main agent
- Markdown/HTML artifact generation
- Basic PPT artifact generation
- Inline preview card
- Live execution timeline
- PostgreSQL + Redis
- Docker sandbox-worker

MCP, full swarm, mini apps, and advanced dashboard auto-update can be Phase 2.
