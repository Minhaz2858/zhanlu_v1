# Zhanlu Project Structure and Data Storage Model

## 1. Core principle

Zhanlu separates three things:

```text
1. Runtime framework code
2. Built-in system package templates
3. Tenant/user runtime data
```

The Git repository stores code, built-in templates, prompts, policies, docs, and Docker infrastructure. PostgreSQL stores real user data, custom agents, custom skills, uploaded files, artifacts, memory, executions, DataSnapshots, audit logs, and governance records.

## 2. Recommended monorepo structure

```text
zhanlu/
├── README.md
├── .env.example
├── docker-compose.yml
├── docker-compose.dev.yml
├── docker-compose.prod.yml
├── Makefile
├── pyproject.toml
├── package.json
│
├── docs/
│   ├── architecture/
│   ├── specs/
│   └── operations/
│
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── app/
│       ├── features/
│       │   ├── auth/
│       │   ├── chat/
│       │   ├── artifacts/
│       │   ├── agent-studio/
│       │   ├── skill-studio/
│       │   ├── mcp-connector-studio/
│       │   ├── datasource-studio/
│       │   ├── memory-browser/
│       │   ├── admin/
│       │   └── settings/
│       ├── components/
│       ├── services/
│       └── styles/
│
├── backend/
│   ├── main.py
│   ├── config.py
│   ├── dependencies.py
│   ├── database.py
│   ├── api/
│   ├── auth/
│   ├── interaction/
│   ├── synexia/
│   ├── agents_runtime/
│   ├── skills_runtime/
│   ├── mcp_gateway/
│   ├── multi_agent/
│   ├── swarm_runtime/
│   ├── tool_gateway/
│   ├── datasources/
│   ├── memory_knowledge/
│   ├── execution/
│   ├── artifacts/
│   ├── platform_services/
│   ├── workers/
│   ├── sandbox/
│   ├── models/
│   ├── migrations/
│   └── tests/
│
├── agent_library/
│   ├── README.md
│   ├── system/
│   │   ├── executive/
│   │   ├── functional/
│   │   ├── industry/
│   │   ├── specialist/
│   │   └── tool-backed/
│   └── examples/
│
├── skill_library/
│   ├── README.md
│   ├── system/
│   │   ├── artifact/
│   │   ├── data/
│   │   ├── visualization/
│   │   └── documents/
│   └── examples/
│
├── mcp_library/
│   ├── README.md
│   ├── system/
│   │   ├── postgres-readonly/
│   │   ├── filesystem-readonly/
│   │   ├── github/
│   │   ├── slack/
│   │   ├── google-drive/
│   │   └── internal-api/
│   └── examples/
│
├── prompts/
│   ├── understand/
│   ├── plan/
│   ├── step/
│   ├── replan/
│   ├── agent_factory/
│   └── skill_factory/
│
├── policies/
│   ├── default.yaml
│   ├── artifact.yaml
│   ├── finance.yaml
│   ├── datasource.yaml
│   ├── memory.yaml
│   ├── skill_review.yaml
│   ├── mcp.yaml
│   └── model_routing.yaml
│
├── infra/
│   ├── docker/
│   ├── postgres/
│   ├── redis/
│   ├── nginx/
│   └── scripts/
│
├── storage/
│   ├── README.md
│   ├── postgres-data/
│   ├── redis-data/
│   ├── sandbox-tmp/
│   └── exports-tmp/
│
└── scripts/
    ├── create_admin.py
    ├── seed_system_agents.py
    ├── seed_system_skills.py
    ├── seed_system_mcp_templates.py
    ├── run_evals.py
    └── lint_manifests.py
```

## 3. Runtime code versus package templates

Runtime code:

```text
backend/agents_runtime/
backend/skills_runtime/
backend/mcp_gateway/
backend/multi_agent/
backend/swarm_runtime/
```

Built-in templates:

```text
agent_library/
skill_library/
mcp_library/
```

Real tenant runtime data:

```text
PostgreSQL records
```

Do not store real custom agents, custom skills, MCP credentials, conversations, artifacts, documents, memory, or audit logs permanently as project folders.

## 4. Agent storage

System/default agent templates live in Git:

```text
agent_library/system/functional/finance-agent/
  AGENT.md
  manifest.yaml
  prompts/
  tests/
```

Real active agents live in PostgreSQL:

```text
agent_profiles
agent_versions
agent_manifests
agent_skill_bindings
agent_data_bindings
agent_mcp_bindings
agent_memory_bindings
agent_policy_bindings
agent_test_cases
agent_invocations
```

## 5. Skill storage

System skills live in Git:

```text
skill_library/system/artifact/pptx-generation/
  SKILL.md
  manifest.yaml
  schemas/
  scripts/
  validators/
  assets/
  references/
  tests/
```

Real custom skills live in PostgreSQL:

```text
skill_profiles
skill_versions
skill_package_versions
skill_manifests
skill_assets
skill_review_records
skill_validation_reports
skill_runs
```

At runtime:

```text
skill package in PostgreSQL
→ temporary sandbox folder
→ Docker sandbox execution
→ artifact output stored in PostgreSQL
→ temporary folder deleted
```

## 6. User and enterprise data storage

Store in PostgreSQL:

```text
organizations
users
groups
group_members
apps
app_grants
sessions
user_preferences
conversations
messages
request_envelopes
executions
execution_events
plans
plan_nodes
agent_profiles
skill_profiles
mcp_servers
datasources
schema_snapshots
semantic_models
metric_definitions
data_snapshots
memory_items
knowledge_items
context_manifests
artifacts
artifact_versions
artifact_blobs
artifact_previews
message_artifacts
audit_logs
policy_decisions
approval_records
cost_ledger
```

Redis is only for:

```text
short-lived cache
rate limiting
job queue
temporary locks
worker heartbeat
stream coordination
```

## 7. Artifact storage

Generated PPT/DOCX/MD/HTML/dashboard/mini app outputs are stored as artifacts:

```text
artifacts
artifact_versions
artifact_blobs
artifact_previews
artifact_source_parts
artifact_build_jobs
artifact_validation_reports
artifact_interactions
message_artifacts
```

The assistant message links to artifacts through `message_artifacts`. The file itself is not stored in `messages.content`.

## 8. Final storage rule

Zhanlu separates runtime framework code, system package templates, and tenant runtime data. `backend/agents_runtime`, `backend/skills_runtime`, and `backend/mcp_gateway` contain framework code. `agent_library`, `skill_library`, and `mcp_library` contain built-in system templates and examples. User-created agents, user-created skills, MCP server configs, uploaded templates, conversations, data sources, memory, artifacts, DataSnapshots, audit logs, and execution records are stored as governed PostgreSQL records, not as permanent server folders. Custom agent and skill packages may be materialized into temporary sandbox folders only during execution, and those folders are destroyed after the run.
