# Zhanlu Main Agent and Subagent UI Architecture

**Version:** FINAL v1.0  
**Purpose:** Claude Code implementation handoff  
**Decision:** Keep the existing Zhanlu UI. Do not rebuild the UI from zero. Build Zhanlu as a native platform with an ADK-inspired main-agent and subagent model.

---

## 0. Executive Decision

Zhanlu should not start from a blank project and should not replace the existing UI.

The correct implementation direction is:

```text
Existing Zhanlu UI
→ Zhanlu backend APIs
→ Synexia orchestrator
→ Main Agent + Subagent runtime
→ Skills / MCP / Datasources / Memory
→ Sandbox execution
→ Artifacts and inline preview
```

The UI must allow users to:

```text
1. Create a main agent.
2. Add subagents under that main agent.
3. Bind skills to the main agent and subagents.
4. Bind databases, files, knowledge, memory, and MCP tools.
5. Configure permissions and execution modes.
6. Use the main agent in chat.
7. Let the main agent delegate work to subagents internally.
8. Preview outputs and execution progress in the existing chat UI.
```

The user should normally interact with the **main agent**. Subagents are specialist workers used by the main agent during execution.

---

## 1. Core Principle

Use this rule everywhere in implementation:

```text
Synexia is the only orchestration brain.
The main agent is the user-facing coordinator.
Subagents are specialist harness profiles under a main agent.
Skills are executable capability packages.
MCP servers are governed external connector endpoints.
Datasources are accessed through governed bindings and DataSnapshots.
Artifacts are generated outputs stored and previewed through the artifact system.
```

Do not allow subagents, skills, MCP servers, or external tools to bypass Synexia, policy, permissions, sandbox, or artifact storage.

---

## 2. Why This Matters

The user wants Zhanlu to feel like a modern AI work platform:

```text
User creates a main agent.
User adds specialist subagents.
User connects company databases and files.
User binds custom skills.
User chats with the main agent.
The main agent coordinates subagents.
Subagents use skills and data safely.
Outputs become previewable artifacts in chat.
```

Example user experience:

```text
User creates:
Finance Report Agent

Subagents:
- Data Analyst Subagent
- Chart Builder Subagent
- PPT Builder Subagent
- Dashboard Builder Subagent
- Reviewer Subagent

User chats with:
Finance Report Agent

User asks:
Make a Q2 finance PPT and dashboard from our database.

Internally:
Finance Report Agent delegates work to the subagents.
```

---

## 3. Use Existing UI, Do Not Rebuild It

Claude Code must not create a new UI from zero unless the user explicitly asks.

Implementation instruction:

```text
Inspect the existing frontend first.
Identify the existing chat page, sidebar, message components, preview panel, settings screens, and agent pages.
Preserve the current UI layout and styling.
Add agent/subagent creation features into the existing UI.
Add backend integration to the current chat interface.
Add Live Execution Timeline and Artifact Preview Card into current message rendering.
Add slash skill picker into the existing chat input.
Do not replace the whole frontend with a new template.
```

The existing UI is the product shell. Backend and runtime features should be mapped into it.

---

## 4. Recommended Use of ADK

ADK is a good reference for agent and subagent concepts. ADK provides production-oriented agent development concepts and supports workflow-style agent composition. Its documentation describes workflow agents such as `SequentialAgent`, which executes subagents in a fixed order, and ADK materials describe multi-agent composition and subagent patterns.

For Zhanlu, use ADK as an **architecture reference**, not as a required core dependency in v1.

### 4.1 V1 Recommendation

```text
V1: ADK-inspired Zhanlu-native implementation.
```

This means:

```text
Use Zhanlu database tables for main agents and subagents.
Use Zhanlu PlanDAG for execution.
Use Synexia for orchestration.
Use Zhanlu Skill Runtime for skills.
Use Zhanlu MCP Gateway for connectors.
Use Zhanlu Artifact System for outputs.
Use Zhanlu Sandbox Worker for execution.
```

Do not make ADK own the whole runtime in v1.

### 4.2 V2 Optional Direction

Later, Zhanlu can add:

```text
backend/adk_adapter/
```

The ADK adapter can translate Zhanlu agent definitions into ADK-compatible agents and workflows if needed.

---

## 5. Existing Projects: What to Use and What Not to Use

The user asked whether Zhanlu should use existing projects such as OpenHarness, Hermes Agent, SQLBot, NL2SQL projects, or ADK.

The answer is:

```text
Use them as references and optional modules.
Do not replace Zhanlu with any one of them.
```

### 5.1 ADK

Use ADK as the main conceptual inspiration for:

```text
main agent
subagent tree
workflow agents
sequential execution
parallel execution
reviewer/critic patterns
agent-as-tool patterns
```

But keep Zhanlu-native storage, UI, permissions, sandbox, and artifact management.

### 5.2 OpenHarness

Use OpenHarness as reference for:

```text
agent harness structure
skills/tools/plugins
memory/session concepts
permissions/hooks
multi-agent coordination
Claude-Code-like harness behavior
```

Do not fork it as the full product foundation unless later evaluation proves it fits the UI and database requirements.

### 5.3 Hermes Agent

Use Hermes as reference for:

```text
skill folder ideas
plugin/tool ideas
agent capability expansion
memory concepts
```

Do not make Hermes the central brain because Zhanlu has already chosen Synexia as the only orchestration brain.

### 5.4 SQLBot and NL2SQL Projects

Use SQLBot and NL2SQL projects for:

```text
governed NL2SQL skill
schema linking
metric definitions
business intelligence query flow
read-only SQL validation
DataSnapshot generation
chart/dashboard generation
```

Do not make SQLBot the whole platform because Zhanlu is broader than ChatBI. Zhanlu also needs agents, subagents, skills, MCP, artifacts, sandbox, inline preview, automation, dashboards, mini apps, and enterprise governance.

---

## 6. Product Concepts

### 6.1 Main Agent

A main agent is the user-facing coordinator.

It contains:

```text
name
description
mission
system instruction
conversation behavior
allowed subagents
allowed skills
allowed datasources
allowed MCP tools
memory scope
policy profile
artifact output rules
approval rules
```

The user chats with the main agent.

### 6.2 Subagent

A subagent is a specialist worker under a main agent.

It contains:

```text
name
specialist role
description
subagent instruction
allowed skills
allowed datasources
allowed MCP tools
memory scope
execution mode
output contract
review requirement
```

Subagents are not independent uncontrolled agents. They run only inside the parent main agent's approved execution context.

### 6.3 Skill

A skill is an executable capability package.

Examples:

```text
pptx-generation
docx-generation
markdown-generation
html-generation
dashboard-generation
mini-app-generation
governed-nl2sql
data-snapshot
chart-generation
pdf-preview
artifact-validation
```

### 6.4 MCP Tool

An MCP tool is an external connector exposed through the Zhanlu MCP Gateway.

Examples:

```text
finance-postgres.readonly_query
google-drive.read_template
slack.read_channel
github.read_repo
internal-api.fetch_order_status
```

MCP tools are never called directly by an LLM. They are filtered, permissioned, audited, and invoked through the gateway.

### 6.5 Datasource

A datasource is a governed connection to structured data.

Examples:

```text
PostgreSQL finance database
company sales database
warehouse database
uploaded Excel dataset
internal data API
```

In v1, sandbox jobs should not receive raw datasource credentials. The datasource gateway creates DataSnapshots, and the sandbox receives DataSnapshots.

---

## 7. UI Requirements

### 7.1 Agent Studio

Add or update the existing UI to support:

```text
Agent Studio
  Create main agent
  Edit main agent
  Add subagent
  Edit subagent
  Reorder subagents
  Enable/disable subagent
  Bind skills
  Bind datasources
  Bind MCP tools
  Configure memory scope
  Configure policy profile
  Configure artifact output types
  Test agent
  Publish agent
```

### 7.2 Main Agent Creation UI

Fields:

```text
Agent name
Agent description
Agent purpose / mission
Default system instruction
Agent category
Visibility: private, app_shared, org_shared
Allowed artifact types
Default output style
Memory scope
Policy profile
```

Example:

```text
Name: Finance Report Agent
Description: Creates finance reports, dashboards, and presentations.
Mission: Help finance users analyze business data and generate executive-ready artifacts.
Allowed artifact types: pptx, docx, dashboard, chart, pdf
Memory scope: finance app memory
Policy profile: finance_readonly_default
```

### 7.3 Subagent Creation UI

Fields:

```text
Subagent name
Role
Description
Instruction
Parent main agent
Execution mode
Allowed skills
Allowed datasources
Allowed MCP tools
Output contract
Review rules
```

Example:

```text
Name: Data Analyst Subagent
Role: Data analysis specialist
Parent: Finance Report Agent
Execution mode: delegated
Allowed skills: governed-nl2sql, data-snapshot, chart-generation
Allowed datasource: finance_postgres readonly
Output contract: data_analysis_summary_v1 + DataSnapshot
```

### 7.4 Subagent Tree UI

The user should see a tree or nested list:

```text
Finance Report Agent
  ├── Data Analyst Subagent
  ├── Chart Builder Subagent
  ├── PPT Builder Subagent
  ├── Dashboard Builder Subagent
  └── Reviewer Subagent
```

Each subagent row should show:

```text
status
role
skills count
datasource bindings count
MCP tools count
execution mode
last used
```

### 7.5 Chat UI

The chat UI should allow users to select or open the main agent:

```text
Chat with: Finance Report Agent
```

The user sends messages only to the main agent by default.

During execution, the UI may show:

```text
Finance Report Agent is working...
Data Analyst Subagent created DataSnapshot.
PPT Builder Subagent generated slides.
Reviewer Subagent validated the artifact.
```

This should appear in the Live Execution Timeline.

---

## 8. Agent Execution Modes

Subagents need different execution modes.

### 8.1 Manual Delegate

The main agent asks the user before using the subagent.

Use for sensitive or expensive subagents.

```text
Main Agent: I can use the Compliance Subagent to review this. Continue?
```

### 8.2 Auto Delegate

The main agent can automatically delegate when the task matches the subagent role.

Use for normal specialist work.

### 8.3 Sequential

Subagents run in fixed order.

Example:

```text
Data Analyst → Chart Builder → PPT Builder → Reviewer
```

### 8.4 Parallel

Multiple subagents run at the same time where safe.

Example:

```text
Research Subagent and Data Analyst Subagent work in parallel.
```

### 8.5 Reviewer

A reviewer subagent checks outputs before final response.

Example:

```text
Reviewer Subagent checks slides, facts, formatting, data references, and policy.
```

### 8.6 Critic

A critic subagent challenges assumptions and suggests improvements.

Use for high-quality documents and reports.

### 8.7 Approval Required

The subagent can run only after approval.

Use for:

```text
external sharing
database write actions
restricted data
expensive model use
custom code skills
publishing artifacts
```

---

## 9. Backend Architecture

Add or update these modules:

```text
backend/
  agents_runtime/
    registry.py
    manifest.py
    hierarchy.py
    subagent_service.py
    agent_studio_service.py
    agent_execution_planner.py
    agent_binding_service.py
    agent_policy.py
    agent_invocation.py

  multi_agent/
    team_builder.py
    supervisor.py
    handoff.py
    collaboration_protocol.py
    routing.py
    reviewer.py
    execution_modes.py

  synexia/
    orchestrator.py
    planning/
    decision/
    context/

  skills_runtime/
  mcp_gateway/
  datasources/
  execution/
  artifacts/
```

### 9.1 Required Services

```text
AgentRegistryService
SubagentHierarchyService
AgentBindingService
AgentStudioService
SubagentExecutionPlanner
HandoffPacketService
AgentInvocationService
AgentEvaluationService
```

### 9.2 Runtime Rule

Subagents are not separate backend services in v1. They are database-backed profiles loaded by the runtime.

```text
Main agent profile loaded
→ subagent profiles loaded
→ Synexia creates PlanDAG
→ Layer 5 executes subagent nodes
```

---

## 10. Database Schema

### 10.1 agent_profiles

```sql
CREATE TABLE agent_profiles (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    owner_user_id UUID,
    name TEXT NOT NULL,
    description TEXT,
    agent_type TEXT NOT NULL,
    -- main | subagent | system_template
    category TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 10.2 agent_versions

```sql
CREATE TABLE agent_versions (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    version INT NOT NULL,
    manifest JSONB NOT NULL,
    system_instruction TEXT,
    output_contract JSONB DEFAULT '{}',
    policy_profile_id UUID,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 10.3 agent_hierarchy_edges

```sql
CREATE TABLE agent_hierarchy_edges (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    parent_agent_id UUID NOT NULL REFERENCES agent_profiles(id),
    child_agent_id UUID NOT NULL REFERENCES agent_profiles(id),
    relationship_type TEXT NOT NULL DEFAULT 'subagent',
    execution_mode TEXT NOT NULL DEFAULT 'auto_delegate',
    display_order INT NOT NULL DEFAULT 0,
    is_enabled BOOLEAN NOT NULL DEFAULT true,
    constraints JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(parent_agent_id, child_agent_id)
);
```

### 10.4 agent_skill_bindings

```sql
CREATE TABLE agent_skill_bindings (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    skill_profile_id UUID NOT NULL,
    binding_scope TEXT NOT NULL DEFAULT 'allowed',
    -- allowed | required | blocked
    constraints JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 10.5 agent_data_bindings

```sql
CREATE TABLE agent_data_bindings (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    datasource_id UUID NOT NULL,
    access_mode TEXT NOT NULL DEFAULT 'read_only',
    allowed_tables TEXT[] DEFAULT '{}',
    blocked_tables TEXT[] DEFAULT '{}',
    row_limit INT DEFAULT 10000,
    query_timeout_seconds INT DEFAULT 30,
    constraints JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 10.6 agent_mcp_bindings

```sql
CREATE TABLE agent_mcp_bindings (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    mcp_server_id UUID NOT NULL,
    allowed_tool_names TEXT[] DEFAULT '{}',
    blocked_tool_names TEXT[] DEFAULT '{}',
    constraints JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 10.7 agent_invocations

```sql
CREATE TABLE agent_invocations (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    conversation_id UUID,
    execution_id UUID NOT NULL,
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    parent_invocation_id UUID,
    invocation_role TEXT NOT NULL,
    -- main | subagent | reviewer | critic
    status TEXT NOT NULL DEFAULT 'running',
    input_summary TEXT,
    output_summary TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
```

### 10.8 agent_handoffs

```sql
CREATE TABLE agent_handoffs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    from_agent_id UUID REFERENCES agent_profiles(id),
    to_agent_id UUID NOT NULL REFERENCES agent_profiles(id),
    handoff_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    handoff_payload JSONB NOT NULL,
    required_output_schema TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
```

---

## 11. Agent Manifest Format

Main agent manifest example:

```yaml
agent_id: finance-report-agent
agent_type: main
name: Finance Report Agent
description: Creates finance reports, dashboards, and presentations.
mission: >
  Help finance users analyze approved business data and generate executive-ready artifacts.

memory_scope:
  allowed_scopes:
    - user_private
    - app_shared
  default_scope: app_shared

allowed_artifact_types:
  - pptx
  - docx
  - dashboard
  - chart
  - pdf

subagents:
  - data-analyst-subagent
  - chart-builder-subagent
  - ppt-builder-subagent
  - dashboard-builder-subagent
  - reviewer-subagent

policy_profile: finance_readonly_default
```

Subagent manifest example:

```yaml
agent_id: data-analyst-subagent
agent_type: subagent
name: Data Analyst Subagent
role: data_analysis
parent_agent: finance-report-agent
description: Creates DataSnapshots and data summaries from approved finance data.

allowed_skills:
  - governed-nl2sql
  - data-snapshot
  - chart-generation

allowed_datasources:
  - finance_postgres

forbidden_actions:
  - database_write
  - external_send
  - unrestricted_export

output_contract:
  schema: data_analysis_summary_v1
  must_create_data_snapshot: true
```

---

## 12. API Design

### 12.1 Main Agent APIs

```http
GET    /api/v1/agents
POST   /api/v1/agents
GET    /api/v1/agents/{agent_id}
PATCH  /api/v1/agents/{agent_id}
DELETE /api/v1/agents/{agent_id}
POST   /api/v1/agents/{agent_id}/publish
POST   /api/v1/agents/{agent_id}/test
```

### 12.2 Subagent APIs

```http
GET    /api/v1/agents/{agent_id}/subagents
POST   /api/v1/agents/{agent_id}/subagents
PATCH  /api/v1/agents/{agent_id}/subagents/{subagent_id}
DELETE /api/v1/agents/{agent_id}/subagents/{subagent_id}
POST   /api/v1/agents/{agent_id}/subagents/reorder
```

### 12.3 Binding APIs

```http
GET    /api/v1/agents/{agent_id}/bindings
POST   /api/v1/agents/{agent_id}/skills/{skill_id}/bind
DELETE /api/v1/agents/{agent_id}/skills/{skill_id}/bind
POST   /api/v1/agents/{agent_id}/datasources/{datasource_id}/bind
DELETE /api/v1/agents/{agent_id}/datasources/{datasource_id}/bind
POST   /api/v1/agents/{agent_id}/mcp/{mcp_server_id}/bind
DELETE /api/v1/agents/{agent_id}/mcp/{mcp_server_id}/bind
```

### 12.4 Chat with Main Agent

```http
POST /api/v1/chat/stream
```

Example request:

```json
{
  "conversation_id": "conv_123",
  "main_agent_id": "finance_report_agent",
  "message": "Make a Q2 finance PPT and dashboard from our database.",
  "selected_skill_id": null,
  "attachments": [],
  "selected_artifacts": [],
  "selected_datasources": ["finance_postgres"]
}
```

---

## 13. Execution Flow

User asks main agent:

```text
Make a Q2 finance PPT and dashboard.
```

End-to-end execution:

```text
1. Layer 1 stores user message and RequestEnvelope.
2. Synexia loads main agent profile.
3. Synexia loads enabled subagents under the main agent.
4. Synexia loads allowed skills, datasources, MCP tools, memory scope, and policies.
5. Synexia creates TaskSpec.
6. Synexia creates PlanDAG.
7. PlanDAG includes subagent nodes.
8. Layer 5 starts execution.
9. Data Analyst Subagent creates DataSnapshots.
10. Chart Builder Subagent creates charts.
11. PPT Builder Subagent runs PPT skill in sandbox.
12. Dashboard Builder Subagent creates dashboard artifact.
13. Reviewer Subagent validates outputs.
14. Artifact Preview is generated.
15. Chat shows artifact cards and execution timeline.
```

PlanDAG example:

```json
{
  "plan_id": "plan_123",
  "nodes": [
    {
      "node_key": "analyze_finance_data",
      "node_type": "subagent_call",
      "agent_id": "data_analyst_subagent",
      "expected_output": "data_snapshot"
    },
    {
      "node_key": "create_charts",
      "node_type": "subagent_call",
      "agent_id": "chart_builder_subagent",
      "depends_on": ["analyze_finance_data"]
    },
    {
      "node_key": "build_ppt",
      "node_type": "subagent_call",
      "agent_id": "ppt_builder_subagent",
      "depends_on": ["create_charts"]
    },
    {
      "node_key": "build_dashboard",
      "node_type": "subagent_call",
      "agent_id": "dashboard_builder_subagent",
      "depends_on": ["analyze_finance_data"]
    },
    {
      "node_key": "review_outputs",
      "node_type": "subagent_call",
      "agent_id": "reviewer_subagent",
      "depends_on": ["build_ppt", "build_dashboard"]
    }
  ]
}
```

---

## 14. Handoff Packets

Agents should not communicate through uncontrolled hidden text only.

Use structured handoff packets.

Example:

```json
{
  "handoff_type": "data_analysis_request",
  "from_agent": "finance_report_agent",
  "to_agent": "data_analyst_subagent",
  "goal": "Create Q2 revenue, cost, and margin analysis.",
  "constraints": {
    "datasource_id": "finance_postgres",
    "allowed_tables": ["revenue", "cost", "budget"],
    "access_mode": "read_only",
    "row_limit": 10000
  },
  "required_output": {
    "type": "data_snapshot",
    "schema": "data_analysis_summary_v1"
  }
}
```

Rules:

```text
Every handoff is stored.
Every handoff has a reason.
Every receiving subagent checks its own permissions.
No subagent can expand its own scope.
No subagent can call another subagent outside the approved tree.
High-risk handoff may require approval.
```

---

## 15. Main Agent and Subagent UI Flow

### 15.1 Create Main Agent

```text
Open Agent Studio.
Click Create Agent.
Choose Main Agent.
Enter name, mission, description, and default behavior.
Choose visibility.
Choose allowed output types.
Save draft.
```

### 15.2 Add Subagent

```text
Open the main agent.
Click Add Subagent.
Choose subagent role or template.
Enter instruction and description.
Bind skills.
Bind datasources.
Bind MCP tools.
Choose execution mode.
Save.
```

### 15.3 Test Agent

```text
Click Test Agent.
Open test chat.
Send sample task.
Show execution timeline.
Show which subagents were used.
Show generated artifacts.
```

### 15.4 Publish Agent

```text
Run preflight.
Check missing bindings.
Check policy.
Check skill approval.
Check datasource permissions.
Check sandbox requirements.
Publish if valid.
```

---

## 16. Preflight Rules

Before the main agent can run:

```text
Is the main agent active?
Does the user have permission to use it?
Are required subagents enabled?
Are required skills approved?
Are datasource bindings valid?
Are MCP tools allowed?
Does the task require sandbox?
Does the task require approval?
Are output artifact types allowed?
Is the cost within budget?
```

Preflight states:

```text
ready
warning
blocked
```

Example blocked state:

```text
Blocked: PPT Builder Subagent requires company-finance-ppt skill, but the skill is not approved.
```

---

## 17. Relationship with Slash Skill Picker

The user may type `/` in chat and select a skill manually.

If the user is chatting with a main agent and selects a skill:

```text
1. Check whether the main agent is allowed to use that skill.
2. Check whether any subagent is better suited for that skill.
3. If a suitable subagent exists, route the skill invocation to that subagent.
4. If not, run it under the main agent if allowed.
5. If blocked, show clear error.
```

Example:

```text
User selects /pptx-generation
Current agent: Finance Report Agent
System checks: PPT Builder Subagent has this skill
Execution: route to PPT Builder Subagent
```

---

## 18. Relationship with Database-Connected Agents

Each main agent and subagent can have datasource bindings.

Important rule:

```text
Agents and subagents do not receive raw database credentials.
They request data through Datasource Gateway.
Datasource Gateway creates DataSnapshots.
Sandbox receives DataSnapshots, not credentials.
```

Example:

```text
Data Analyst Subagent requests finance data.
Datasource Gateway validates allowed tables and read-only policy.
SQL validator checks query safety.
Backend runs query.
DataSnapshot is stored.
PPT Builder Subagent receives the snapshot for artifact generation.
```

---

## 19. Relationship with MCP

Main agents and subagents may have MCP bindings.

Rules:

```text
MCP is external connector protocol, not internal brain.
MCP tools must be registered.
MCP tools must be filtered by agent binding.
MCP calls must be audited.
MCP tools cannot bypass policy.
MCP tools cannot expose secrets to agents.
```

Example:

```text
Finance Report Agent can use:
- finance-postgres.readonly_query
- company-drive.read_template

Finance Report Agent cannot use:
- hr-postgres.query
- slack.send_message
- unrestricted_http_fetch
```

---

## 20. Relationship with Artifacts and Inline Preview

Subagents produce outputs that become artifacts.

Example:

```text
PPT Builder Subagent → PPT artifact
Dashboard Builder Subagent → dashboard artifact
Document Subagent → DOCX/MD artifact
Mini App Subagent → mini app artifact
Reviewer Subagent → validation report
```

Artifacts are linked to chat messages through `message_artifacts` and previewed through the inline artifact preview system.

The main agent final response should summarize:

```text
what was created
which subagents worked
which data sources were used
which artifacts are ready
what the user can do next
```

---

## 21. Security Rules

Hard rules:

```text
No agent can access another org's data.
No subagent can run outside its parent agent scope.
No subagent can add permissions to itself.
No agent can call unbound skills.
No agent can call unbound MCP tools.
No sandbox job receives raw database credentials.
No generated artifact is trusted until validation passes.
No external publish/share without approval.
No user-created custom skill runs without sandbox and review.
```

---

## 22. Implementation Phases

### Phase 1: UI and Database Foundation

```text
Add agent_profiles.
Add agent_versions.
Add agent_hierarchy_edges.
Add agent_skill_bindings.
Add agent_data_bindings.
Add agent_mcp_bindings.
Add Agent Studio UI fields.
Add subagent tree UI.
```

### Phase 2: Main Agent Chat

```text
User selects main agent in chat.
Backend loads main agent profile.
Synexia uses main agent context.
Simple main-agent-only tasks run.
```

### Phase 3: Subagent Delegation

```text
Load subagents.
Create subagent_call PlanDAG nodes.
Store agent_invocations.
Store handoff packets.
Show subagent activity in execution timeline.
```

### Phase 4: Skills and Datasources

```text
Bind skills to agents.
Bind datasources to agents.
Run DataSnapshot flow.
Route skill execution to appropriate subagent.
```

### Phase 5: Artifacts and Sandbox

```text
PPT Builder Subagent creates PPT.
Dashboard Subagent creates dashboard.
Reviewer Subagent validates.
Artifacts preview inline.
```

### Phase 6: Advanced Patterns

```text
parallel subagents
reviewer and critic pattern
approval-required subagents
MCP connector routing
ADK adapter if needed
```

---

## 23. Claude Code Build Instructions

Claude Code must follow these instructions:

```text
1. Do not rebuild the UI from zero.
2. Inspect the existing UI and map new features into it.
3. Implement main-agent and subagent storage in PostgreSQL.
4. Add UI for creating main agents and subagents.
5. Add skill, datasource, MCP, memory, and policy bindings.
6. Make the chat use the selected main agent.
7. Implement subagent delegation through Synexia PlanDAG.
8. Show subagent work in the Live Execution Timeline.
9. Route skill execution through Layer 5 and sandbox worker.
10. Store generated outputs as artifacts and preview them inline.
11. Keep Synexia as the only orchestration brain.
12. Use ADK concepts as reference, not as mandatory dependency in v1.
13. Use OpenHarness/Hermes/SQLBot/NL2SQL only as references or optional modules.
```

---

## 24. Acceptance Tests

### Test 1: Create Main Agent

```text
User can create Finance Report Agent.
Agent is stored in PostgreSQL.
Agent appears in Agent Studio.
Agent can be selected in chat.
```

### Test 2: Add Subagent

```text
User can add Data Analyst Subagent under Finance Report Agent.
agent_hierarchy_edges row is created.
Subagent appears under parent in UI tree.
```

### Test 3: Bind Skill

```text
User binds governed-nl2sql skill to Data Analyst Subagent.
Binding is stored.
Skill appears in subagent details.
```

### Test 4: Bind Datasource

```text
User binds finance_postgres datasource to Data Analyst Subagent.
Binding is read-only.
Allowed tables are enforced.
```

### Test 5: Main Agent Delegates

```text
User asks Finance Report Agent for Q2 analysis.
Synexia creates subagent_call node for Data Analyst Subagent.
agent_invocations row is created.
Execution timeline shows Data Analyst Subagent activity.
```

### Test 6: PPT Generation

```text
User asks Finance Report Agent to make PPT.
PPT Builder Subagent is selected.
PPT skill runs in sandbox.
Artifact is created.
Preview appears in chat.
```

### Test 7: Permission Block

```text
Subagent without datasource binding tries to query finance database.
System blocks the action.
User sees clear error.
Audit record is stored.
```

### Test 8: UI Preservation

```text
Existing chat UI remains intact.
New agent/subagent features are integrated without replacing the whole frontend.
```

---

## 25. Final Architecture Principle

Use this as the official design sentence:

**Zhanlu should keep the existing UI and implement a native, ADK-inspired main-agent/subagent architecture. Users create a main agent in the UI, add specialist subagents under it, bind skills, datasources, MCP tools, memory, and policies, then chat with the main agent. Synexia remains the only orchestration brain and routes work to subagents through governed PlanDAG execution. Subagents are bounded specialist harness profiles, not independent uncontrolled brains. OpenHarness, Hermes, SQLBot, NL2SQL, and ADK should be used as references or optional adapters, not as replacements for the Zhanlu platform architecture.**

---

## 26. Research References

- ADK official website: https://adk.dev/
- ADK SequentialAgent documentation: https://adk.dev/agents/workflow-agents/sequential-agents/
- ADK sample agents: https://github.com/google/adk-samples
- OpenHarness GitHub: https://github.com/HKUDS/OpenHarness
- OpenHarness website: https://open-harness.dev/
- DataEase SQLBot GitHub: https://github.com/dataease/SQLBot
- NL2SQL Handbook: https://github.com/hkustdial/nl2sql_handbook
