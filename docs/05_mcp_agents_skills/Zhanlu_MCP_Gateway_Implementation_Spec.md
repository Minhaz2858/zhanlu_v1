# Zhanlu MCP Gateway Implementation Spec

## Purpose

MCP allows Zhanlu to connect agents to external tools, resources, prompts, apps, databases, and workflows. In Zhanlu, MCP must sit behind the Tool/Skill/MCP Gateway and must never bypass governance.

## Core rule

```text
Agent does not directly call MCP.
Skill does not directly call MCP without gateway.
Model does not see all MCP tools.
Everything goes through Tool / Skill / MCP Gateway.
```

## MVP status

MCP is Phase 2 unless existing UI requires it. Build interfaces and database schema now, but full MCP runtime can be added later.

## MCP vs Skill

```text
Skill = Zhanlu-native capability package.
MCP = external connector protocol.
```

Examples:

```text
pptx-generation = Skill
html-generation = Skill
dashboard-generation = Skill
governed-nl2sql = Skill

Google Drive = MCP server
Slack = MCP server
GitHub = MCP server
PostgreSQL readonly connector = MCP server or native datasource connector
ERP = MCP server
```

## Gateway flow

```text
Synexia PlanDAG
→ Tool/Skill/MCP Gateway
→ permission filter
→ schema validation
→ policy evaluation
→ approved MCP server/tool
→ result returned
→ ObservationRecord stored
→ audit event stored
```

## Backend folder

```text
backend/mcp_gateway/
  registry.py
  server_manager.py
  tool_mapper.py
  resource_mapper.py
  prompt_mapper.py
  permission_filter.py
  schema_validator.py
  call_executor.py
  audit.py
  health_check.py
```

## Database tables

```sql
CREATE TABLE mcp_servers (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    name TEXT NOT NULL,
    server_key TEXT NOT NULL,
    transport TEXT NOT NULL,
    endpoint TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    config_json JSONB NOT NULL DEFAULT '{}',
    credential_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE mcp_tools (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    tool_key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    input_schema JSONB NOT NULL DEFAULT '{}',
    output_schema JSONB NOT NULL DEFAULT '{}',
    risk_tier TEXT NOT NULL DEFAULT 'low',
    side_effect_type TEXT NOT NULL DEFAULT 'read',
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE agent_mcp_bindings (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    agent_profile_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    allowed_tool_keys TEXT[] NOT NULL DEFAULT '{}',
    policy_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE mcp_call_logs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    execution_id UUID,
    agent_profile_id UUID,
    mcp_server_id UUID NOT NULL,
    tool_key TEXT NOT NULL,
    input_summary JSONB NOT NULL DEFAULT '{}',
    output_summary JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Permission filtering

Before exposing or calling an MCP tool, check:

- user has access to app,
- selected agent is active,
- agent has MCP binding,
- tool is in allowed_tool_keys,
- tool side-effect is allowed,
- data sensitivity permits this tool,
- risk tier does not require missing approval,
- schema validates,
- cost/budget is acceptable.

## Readonly database MCP mode

For database MCP connectors, default must be read-only.

Allowed:

```text
schema_summary
metric_definition_lookup
readonly_query with validator
sample_rows with row limit
```

Blocked by default:

```text
insert
update
delete
drop
alter
copy full table
export unrestricted data
```

Best practice for artifact generation:

```text
MCP/database connector creates DataSnapshot.
Sandbox receives DataSnapshot, not raw database credentials.
```

## Tool exposure to model

Do not expose all MCP tools to the model. Synexia should receive only a filtered capability summary.

Example:

```json
{
  "available_mcp_tools": [
    {
      "tool_key": "finance_postgres.readonly_query",
      "description": "Run approved read-only finance queries through SQL validator.",
      "risk_tier": "medium"
    }
  ]
}
```

## Audit requirements

Every MCP call must store:

- who requested,
- which app,
- which agent,
- which execution,
- which MCP server/tool,
- input summary,
- output summary,
- policy decision,
- status/error,
- timestamp.

## Phase plan

Phase 1:

- create schema,
- create registry APIs,
- create mock MCP connector adapter,
- support readonly internal datasource connector.

Phase 2:

- real MCP client runtime,
- tool discovery,
- health check,
- permission filter,
- audit.

Phase 3:

- connector studio,
- external MCP imports,
- review workflow,
- per-agent MCP binding UI.
