# Zhanlu Central Datasource Connector and Agent-Specific Data Binding

**Document status:** Final implementation specification  
**Purpose:** Define how users connect databases and knowledge bases once in a central place, then explicitly bind selected data sources to specific main agents and delegated subagents.  
**Audience:** Claude Code, backend engineers, frontend engineers, product designers, and Zhanlu architecture maintainers.

---

## 1. Core Decision

Zhanlu must use **one central datasource connector system**.

Users connect databases and knowledge bases in one central UI area:

```text
My Space
  → Databases / KB
      → New Database Connection
      → New Knowledge Base
      → Manage Existing Connections
```

Agents do **not** create independent raw database connections.

When creating or editing an agent, the user selects which existing datasource(s) that agent may use.

If a user has three databases but wants one agent to use only one database, that agent must only see and use that selected database.

Example:

```text
User-owned datasources:
  1. Finance Database
  2. Customer Business Database
  3. Quality Control Database

Agent being created:
  Quality Control Agent

Selected datasource:
  ✓ Quality Control Database
  ✗ Finance Database
  ✗ Customer Business Database
```

The Quality Control Agent and its subagents must not see, query, infer from, or use the Finance Database or Customer Business Database unless the user explicitly adds those datasource bindings later.

---

## 2. Final Product Rule

Use these rules as non-negotiable implementation constraints:

```text
DATA-CORE-1:
Zhanlu has one central datasource connection system. Users create and manage database/KB connections only in My Space → Databases / KB.

DATA-CORE-2:
Agents and subagents never create independent raw database connections.

DATA-CORE-3:
A main agent can only use datasource handles explicitly selected by the user during agent creation or later agent editing.

DATA-CORE-4:
Subagents inherit no database access by default. They receive only explicit delegated access from the main agent.

DATA-CORE-5:
All unselected user databases are invisible and blocked for that agent, its subagents, skills, MCP tools, and sandbox jobs.

DATA-CORE-6:
All database access must go through the Datasource Gateway. No agent, subagent, skill, MCP tool, or sandbox may receive raw database credentials.

DATA-CORE-7:
Database-derived outputs must use DataSnapshots. PPTs, dashboards, DOCX reports, charts, and mini apps cite DataSnapshots, not live mutable queries.

DATA-CORE-8:
Users interact with the main agent in chat. Subagents work under the main agent through Synexia-controlled delegation.
```

---

## 3. Why This Matters

This design solves five important problems:

### 3.1 User control

The user may have many databases, but each agent needs a clear boundary.

```text
Having access to My Space does not mean an agent has access to every database.
```

### 3.2 Security

A quality-control agent should not accidentally access finance or HR data.

### 3.3 Routing clarity

Synexia can route requests correctly because each agent has explicit datasource bindings.

### 3.4 Auditability

Every query is traceable:

```text
user → main agent → delegated subagent → datasource binding → query run → DataSnapshot → artifact
```

### 3.5 Sandbox safety

Sandbox jobs generate files and dashboards from approved DataSnapshots, not from raw database credentials.

---

## 4. UI Structure

### 4.1 My Space: Central datasource management

The UI should contain a central asset area:

```text
My Space
  ├── My Agents
  ├── Databases / KB
  ├── My Files
  ├── Decision Flows
  └── My Reports
```

Inside `Databases / KB`, the user can create and manage reusable data assets:

```text
Databases / KB
  ├── Enterprise Product Knowledge Base
  ├── Customer Business Database
  ├── Finance Database
  ├── Quality Control Database
  └── + New Knowledge Base / + New Database Connection
```

Each datasource card should show:

```text
Name
Type
Description
Owner
Visibility
Connection status
Last schema sync
Last used by agent
Actions: Run Test · Edit · Delete · View Schema · Permissions
```

Example card:

```text
Quality Control Database
Contains defect records, inspection results, production batches, and supplier quality data.
Type: PostgreSQL
Status: Connected
Actions: Test · View Schema · Edit · Delete
```

---

## 5. Agent Studio: datasource binding

When a user creates a main agent, the Agent Studio must include a **Data Access** section.

### 5.1 Data Access section

Show all datasources available to the user in My Space, but do not select them by default.

```text
Data Access

Select database or knowledge base this agent can use:

[ ] Finance Database
[ ] Customer Business Database
[✓] Quality Control Database
[ ] Enterprise Product Knowledge Base
```

For the selected datasource, show configuration fields:

```text
Datasource: Quality Control Database
Access mode: Read-only
Allowed tables: defect_records, inspection_results, production_batches
Blocked tables: employee_salary, credentials, admin_logs
Allowed operations: select, aggregate, summarize, visualize
Row limit: 10,000
Query timeout: 30 seconds
Allow subagent delegation: Yes
Require approval for large exports: Yes
```

### 5.2 Explicit selection behavior

If the user selects only one datasource, the agent can only use that one.

```text
Agent: Quality Control Agent
Allowed datasource handles:
  quality_control_database

Blocked by omission:
  finance_database
  customer_business_database
```

The backend must treat omitted datasources as blocked, not merely hidden in the UI.

---

## 6. Main Agent and Subagent Relationship

The user chats with the **main agent**.

The main agent can delegate work to subagents.

```text
User
  → Main Agent
      → Data Analyst Subagent
      → SPC Chart Subagent
      → PPT Builder Subagent
      → Reviewer Subagent
```

Subagents should not appear as independent user-facing chat agents unless explicitly allowed by the product.

### 6.1 Example

```text
Main Agent:
  Quality Control Agent

Purpose:
  Analyze defect trends, generate SPC control charts, and recommend improvement measures.

Selected datasource:
  Quality Control Database only

Subagents:
  Data Analyst Subagent
  SPC Chart Subagent
  Report Writer Subagent
  PPT Builder Subagent
  Reviewer Subagent
```

---

## 7. Delegated Subagent Data Access

Subagents do not automatically inherit full datasource access.

Subagent access must be delegated explicitly.

### 7.1 Recommended delegation levels

Use these access levels:

```text
none
  The subagent cannot use this datasource or its snapshots.

snapshot_only
  The subagent can use approved DataSnapshots created by another subagent or the main agent.

query_via_gateway
  The subagent can request read-only queries through the Datasource Gateway, within limits.

schema_only
  The subagent can see schema summaries but cannot query data.

review_only
  The subagent can inspect source references and validation reports, but cannot request new data.
```

### 7.2 Example subagent data grants

```text
Quality Control Agent
  datasource: Quality Control Database

Subagent grants:

Data Analyst Subagent:
  query_via_gateway
  allowed tables: defect_records, inspection_results, production_batches

SPC Chart Subagent:
  snapshot_only
  can use DataSnapshots created by Data Analyst Subagent

PPT Builder Subagent:
  snapshot_only
  can use chart artifacts and DataSnapshots

Reviewer Subagent:
  review_only
  can inspect DataSnapshot references, artifact validation reports, and chart sources
```

---

## 8. Datasource Gateway

All database activity goes through the **Datasource Gateway**.

No agent, subagent, skill, MCP tool, sandbox job, or generated code receives raw database credentials.

### 8.1 Gateway responsibilities

The Datasource Gateway must handle:

```text
Datasource permission check
Agent datasource binding check
Subagent grant check
Schema lookup
Semantic model lookup
Metric definition lookup
SQL generation request
SQL validation
Read-only enforcement
Allowed table enforcement
Blocked table enforcement
Row limit enforcement
Query timeout enforcement
Result checksum
DataSnapshot creation
Audit logging
```

### 8.2 Correct flow

```text
Main Agent / Subagent requests data
↓
Datasource Gateway checks agent_data_bindings and subagent_data_grants
↓
SQL is generated or selected
↓
SQL Validator checks safety
↓
Backend runs read-only query
↓
DataSnapshot is created
↓
Subagent / skill / sandbox uses DataSnapshot
```

### 8.3 Forbidden flow

```text
Subagent receives database password
↓
Subagent sends raw SQL directly
↓
Sandbox connects to production database
```

This must never be allowed.

---

## 9. DataSnapshot Rule

Database-derived outputs must use DataSnapshots.

A DataSnapshot is a governed, auditable result produced from a controlled query.

### 9.1 Why DataSnapshots are required

DataSnapshots provide:

```text
traceability
repeatability
artifact source references
permission boundary
query audit
stable report numbers
safe sandbox input
```

### 9.2 Flow

```text
Database
  → Datasource Gateway
  → Read-only query
  → DataSnapshot
  → Subagent / Skill / Sandbox
  → Artifact
```

Not:

```text
Database
  → Sandbox directly
  → Artifact
```

### 9.3 Artifact source references

Any generated artifact should record:

```json
{
  "source_refs": {
    "data_snapshots": ["snapshot_qc_defects_q2"],
    "datasource_id": "quality_control_database",
    "semantic_model_id": "qc_semantic_model_v1",
    "query_runs": ["query_run_123"]
  }
}
```

---

## 10. User Flow: Creating an Agent with One Database

### 10.1 Step 1: Create datasource in My Space

User opens:

```text
My Space → Databases / KB → New Database Connection
```

User adds:

```text
Name: Quality Control Database
Type: PostgreSQL
Host: configured securely
Database: qc_production
Description: Defect records and inspection results
```

Zhanlu stores credentials securely as encrypted secrets or secret references.

The datasource appears in My Space as a reusable asset.

### 10.2 Step 2: Create main agent

User opens:

```text
My Space → My Agents → Create Agent
```

User enters:

```text
Name: Quality Control Agent
Description: Analyze defect distribution trends, generate SPC control charts, and improvement measures.
Agent role: Main Agent
```

### 10.3 Step 3: Select data access

User selects only:

```text
✓ Quality Control Database
```

Unselected:

```text
✗ Finance Database
✗ Customer Business Database
```

### 10.4 Step 4: Configure subagents

User adds:

```text
Data Analyst Subagent
SPC Chart Subagent
PPT Builder Subagent
Reviewer Subagent
```

The UI lets the user assign data grants:

```text
Data Analyst Subagent: query_via_gateway
SPC Chart Subagent: snapshot_only
PPT Builder Subagent: snapshot_only
Reviewer Subagent: review_only
```

### 10.5 Step 5: Save and test

User tests:

```text
Analyze defect trends for the last quarter and make SPC charts.
```

Zhanlu preflight checks:

```text
✓ Agent has selected datasource
✓ Data Analyst Subagent has query_via_gateway
✓ SQL is read-only
✓ Tables are allowed
✓ Row limit is enforced
✓ DataSnapshot can be created
✓ Chart skill is available
```

---

## 11. Runtime Example

User asks in chat:

```text
Quality Control Agent, analyze defect trends and make a PPT with SPC charts.
```

Execution:

```text
1. User chats with Main Agent: Quality Control Agent.

2. Synexia loads:
   - main agent profile
   - subagents
   - skill bindings
   - datasource bindings
   - subagent data grants

3. Synexia creates PlanDAG:
   node_1: Data Analyst Subagent creates defect trend DataSnapshot
   node_2: SPC Chart Subagent creates control charts from DataSnapshot
   node_3: PPT Builder Subagent builds PPT from charts and DataSnapshot
   node_4: Reviewer Subagent validates formatting and sources

4. Datasource Gateway:
   - verifies Quality Control Database binding
   - blocks all other databases
   - validates SQL
   - runs read-only query
   - creates DataSnapshot

5. Sandbox:
   - receives DataSnapshot
   - receives chart assets
   - receives PPT skill package
   - receives template
   - creates PPTX and preview

6. Artifact Service:
   - stores PPTX
   - stores PDF preview
   - stores thumbnails
   - records source DataSnapshot

7. Chat UI:
   - shows Live Execution Timeline
   - shows inline PPT preview
```

---

## 12. Backend Data Model

### 12.1 datasources

```sql
CREATE TABLE datasources (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    owner_user_id UUID NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    datasource_type TEXT NOT NULL,
    -- postgres | mysql | sqlite | csv | excel | knowledge_base | api
    visibility TEXT NOT NULL DEFAULT 'private',
    -- private | app_shared | org_shared
    status TEXT NOT NULL DEFAULT 'active',
    connection_metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 12.2 datasource_credentials

Credentials must not be exposed to agents or sandboxes.

```sql
CREATE TABLE datasource_credentials (
    id UUID PRIMARY KEY,
    datasource_id UUID NOT NULL REFERENCES datasources(id),
    secret_ref TEXT,
    encrypted_secret BYTEA,
    kms_key_id TEXT,
    rotation_status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rotated_at TIMESTAMPTZ
);
```

### 12.3 agent_data_bindings

```sql
CREATE TABLE agent_data_bindings (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    agent_id UUID NOT NULL,
    datasource_id UUID NOT NULL REFERENCES datasources(id),
    access_mode TEXT NOT NULL DEFAULT 'read_only',
    allowed_tables TEXT[] DEFAULT '{}',
    blocked_tables TEXT[] DEFAULT '{}',
    allowed_operations TEXT[] DEFAULT ARRAY['select', 'aggregate', 'summarize'],
    row_limit INT DEFAULT 10000,
    query_timeout_seconds INT DEFAULT 30,
    delegation_allowed BOOLEAN NOT NULL DEFAULT false,
    approval_required_for_large_export BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 12.4 subagent_data_grants

```sql
CREATE TABLE subagent_data_grants (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    parent_agent_id UUID NOT NULL,
    subagent_id UUID NOT NULL,
    datasource_id UUID NOT NULL REFERENCES datasources(id),
    grant_level TEXT NOT NULL,
    -- none | snapshot_only | query_via_gateway | schema_only | review_only
    allowed_tables TEXT[] DEFAULT '{}',
    blocked_tables TEXT[] DEFAULT '{}',
    allowed_operations TEXT[] DEFAULT '{}',
    row_limit INT,
    query_timeout_seconds INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 12.5 data_snapshots

```sql
CREATE TABLE data_snapshots (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    datasource_id UUID NOT NULL REFERENCES datasources(id),
    execution_id UUID NOT NULL,
    created_by_agent_id UUID,
    created_by_subagent_id UUID,
    query_run_id UUID,
    query_hash TEXT NOT NULL,
    query_text TEXT,
    semantic_model_id UUID,
    tables_used TEXT[] DEFAULT '{}',
    columns_used JSONB DEFAULT '{}',
    row_count INT,
    result_json JSONB,
    result_checksum TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 12.6 query_runs

```sql
CREATE TABLE query_runs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    datasource_id UUID NOT NULL REFERENCES datasources(id),
    execution_id UUID NOT NULL,
    agent_id UUID,
    subagent_id UUID,
    sql_text TEXT NOT NULL,
    sql_hash TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    status TEXT NOT NULL,
    row_count INT,
    duration_ms INT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 13. Backend API Contract

### 13.1 Datasource APIs

```text
GET    /api/v1/datasources
POST   /api/v1/datasources
GET    /api/v1/datasources/{datasource_id}
PATCH  /api/v1/datasources/{datasource_id}
DELETE /api/v1/datasources/{datasource_id}
POST   /api/v1/datasources/{datasource_id}/test
POST   /api/v1/datasources/{datasource_id}/sync-schema
GET    /api/v1/datasources/{datasource_id}/schema
```

### 13.2 Agent datasource binding APIs

```text
GET    /api/v1/agents/{agent_id}/data-bindings
POST   /api/v1/agents/{agent_id}/data-bindings
PATCH  /api/v1/agents/{agent_id}/data-bindings/{binding_id}
DELETE /api/v1/agents/{agent_id}/data-bindings/{binding_id}
```

### 13.3 Subagent grant APIs

```text
GET    /api/v1/agents/{agent_id}/subagents/{subagent_id}/data-grants
POST   /api/v1/agents/{agent_id}/subagents/{subagent_id}/data-grants
PATCH  /api/v1/agents/{agent_id}/subagents/{subagent_id}/data-grants/{grant_id}
DELETE /api/v1/agents/{agent_id}/subagents/{subagent_id}/data-grants/{grant_id}
```

### 13.4 Query and DataSnapshot APIs

These should usually be internal service APIs, not public UI APIs.

```text
POST /api/v1/internal/datasource-query/preflight
POST /api/v1/internal/datasource-query/run
GET  /api/v1/data-snapshots/{snapshot_id}
```

---

## 14. Example API Payloads

### 14.1 Create datasource

```json
{
  "name": "Quality Control Database",
  "description": "Defect records, inspection results, production batches, and supplier quality data.",
  "datasource_type": "postgres",
  "visibility": "private",
  "connection_metadata": {
    "host": "qc-db.internal",
    "port": 5432,
    "database": "qc_production",
    "ssl_required": true
  },
  "credential_payload": {
    "username": "readonly_user",
    "password": "********"
  }
}
```

### 14.2 Bind one datasource to one agent

```json
{
  "datasource_id": "quality_control_database_uuid",
  "access_mode": "read_only",
  "allowed_tables": [
    "defect_records",
    "inspection_results",
    "production_batches",
    "supplier_quality"
  ],
  "blocked_tables": [
    "employee_salary",
    "credentials",
    "admin_logs"
  ],
  "allowed_operations": [
    "select",
    "aggregate",
    "summarize",
    "visualize"
  ],
  "row_limit": 10000,
  "query_timeout_seconds": 30,
  "delegation_allowed": true,
  "approval_required_for_large_export": true
}
```

### 14.3 Grant subagent access

```json
{
  "parent_agent_id": "quality_control_agent_uuid",
  "subagent_id": "spc_chart_subagent_uuid",
  "datasource_id": "quality_control_database_uuid",
  "grant_level": "snapshot_only",
  "allowed_tables": [],
  "blocked_tables": [],
  "allowed_operations": []
}
```

### 14.4 Query preflight request

```json
{
  "agent_id": "quality_control_agent_uuid",
  "subagent_id": "data_analyst_subagent_uuid",
  "datasource_id": "quality_control_database_uuid",
  "intent": "Analyze defect distribution for last quarter",
  "requested_tables": ["defect_records", "inspection_results"]
}
```

### 14.5 Query preflight response

```json
{
  "status": "ready",
  "allowed": true,
  "datasource_id": "quality_control_database_uuid",
  "grant_level": "query_via_gateway",
  "constraints": {
    "access_mode": "read_only",
    "allowed_tables": ["defect_records", "inspection_results"],
    "row_limit": 10000,
    "query_timeout_seconds": 30
  }
}
```

---

## 15. Frontend Components

Add or adapt these components in the existing UI:

```text
MySpaceDatasourceTab.tsx
DatasourceCard.tsx
NewDatasourceModal.tsx
DatasourceSchemaViewer.tsx
AgentDataAccessSection.tsx
DatasourceSelector.tsx
AgentDatasourceBindingEditor.tsx
SubagentDataGrantEditor.tsx
AgentPreflightPanel.tsx
```

### 15.1 DatasourceSelector behavior

The datasource selector should:

```text
show only datasources the current user can access
allow selecting one or more datasource handles
show selected datasource badges
show configuration per selected datasource
make unselected datasources unavailable to the agent
save bindings to backend
```

### 15.2 SubagentDataGrantEditor behavior

For each subagent:

```text
show datasource inherited from main agent
ask whether subagent can use it
select grant level
show allowed tables if grant_level = query_via_gateway
hide raw credential fields completely
```

---

## 16. Preflight Checks

Before saving an agent or running a database task, run preflight.

### 16.1 Agent save preflight

Check:

```text
Agent has name and purpose
Selected datasource exists
User owns or can access datasource
Allowed tables exist in schema
Blocked tables are recorded
Subagent grants do not exceed parent agent binding
Selected skills are compatible with datasource use
Sandbox does not require raw DB credentials
```

### 16.2 Runtime query preflight

Check:

```text
User can use main agent
Main agent is bound to datasource
Subagent grant allows this request
Requested tables are allowed
Blocked tables are not used
SQL is read-only
Row limit is enforced
Timeout is enforced
Query cost is acceptable
DataSnapshot will be created
```

---

## 17. Security Rules

### 17.1 Never expose credentials

Do not expose datasource credentials to:

```text
LLM prompts
main agent instructions
subagent instructions
skills
MCP tools
sandbox containers
frontend
execution logs
artifact previews
```

### 17.2 Hide unselected datasources

If an agent is not bound to a datasource:

```text
Synexia should not include that datasource in agent context.
Tool/Skill Gateway should reject calls to it.
Datasource Gateway should reject queries to it.
Sandbox should not receive any file/data derived from it.
```

### 17.3 Read-only by default

All database connections should default to read-only.

Database write actions require a separate future permission model and human approval.

### 17.4 Snapshot before sandbox

Sandbox jobs should receive DataSnapshot files, not live database access.

---

## 18. Error Messages

### 18.1 Agent has no datasource

```text
This agent has no database connected. Open Agent Studio → Data Access and select a datasource.
```

### 18.2 User asks agent to use wrong database

```text
This agent is not allowed to use Finance Database. It is currently connected only to Quality Control Database.
```

### 18.3 Subagent lacks query permission

```text
The SPC Chart Subagent can use existing DataSnapshots, but it cannot query the database directly. The Data Analyst Subagent must create the DataSnapshot first.
```

### 18.4 Blocked table

```text
The requested query uses a blocked table: employee_salary. This table is not available to Quality Control Agent.
```

---

## 19. Acceptance Tests

Claude Code must implement or prepare tests for these cases:

```text
TEST-1:
User has three datasources. Agent is bound to only one. Agent can only see the selected datasource.

TEST-2:
Agent tries to query an unbound datasource. Backend rejects the request.

TEST-3:
Subagent has snapshot_only grant. It cannot query the database directly.

TEST-4:
Data Analyst Subagent has query_via_gateway grant. It can create DataSnapshot within allowed tables.

TEST-5:
Query attempts to use blocked table. SQL Validator blocks it.

TEST-6:
Sandbox job attempts to receive datasource credentials. The system never materializes credentials into sandbox input.

TEST-7:
PPT generated from database stores source_data_snapshot_ids in artifact metadata.

TEST-8:
User edits agent and adds a second datasource. New datasource becomes available only after binding is saved.

TEST-9:
Deleting datasource binding immediately prevents future agent queries to that datasource.

TEST-10:
Frontend does not show unbound datasources in agent chat context or skill execution picker.
```

---

## 20. Implementation Order

Build in this order:

```text
Phase 1:
Create datasource tables and My Space datasource CRUD UI.

Phase 2:
Create agent_data_bindings and Agent Studio Data Access section.

Phase 3:
Create subagent_data_grants and Subagent Data Grant UI.

Phase 4:
Implement Datasource Gateway permission checks.

Phase 5:
Implement read-only query execution and DataSnapshot creation.

Phase 6:
Connect DataSnapshots to sandbox artifact generation.

Phase 7:
Add runtime errors, audit logs, and acceptance tests.
```

---

## 21. Final Design Sentence

Zhanlu must use a single central datasource connector system in My Space. Users connect databases and knowledge bases once, then explicitly bind selected datasource handles to each main agent. If a user has three databases and wants an agent to use only one, the agent and all of its subagents must be restricted to that one selected datasource unless the user adds more bindings later. Users chat with the main agent, while subagents operate under the main agent through Synexia-controlled delegation. All database access passes through the Datasource Gateway with read-only enforcement, table allowlists, blocked tables, row limits, SQL validation, audit logs, and DataSnapshot creation. No agent, subagent, skill, MCP tool, or sandbox ever receives raw database credentials.
