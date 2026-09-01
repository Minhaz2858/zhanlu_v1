# Zhanlu Enterprise Agent, Skill, MCP, Multi-Agent, Swarm and Artifact System

**Version:** Final v1.0  
**Target stack:** Docker, PostgreSQL, Redis, FastAPI, React, Synexia Agent  
**Architecture fit:** Layers 1 to 7 of Zhanlu Enterprise AI Operating System  
**Core principle:** Synexia is the only cognitive controller. Agents, skills, MCP connectors, multi-agent teams, swarms, artifacts, dashboards, and mini apps are governed runtime objects.

---

## 1. Purpose

This document defines how Zhanlu supports modern AI features in an enterprise-safe way:

- Agent Studio
- Skill Studio
- MCP Connector Studio
- Multi-Agent Team Builder
- Governed Agent Swarm
- Artifact Workspace
- PPT, DOCX, MD, HTML, dashboard, chart, and mini app generation
- Inline chat preview
- Partial editing and regeneration
- Dashboard auto-update
- PostgreSQL-backed storage
- Docker sandbox execution
- Enterprise governance, audit, approval, and cost control

The goal is not to build a chaotic open-ended agent system. The goal is to build a controlled enterprise AI operating system where every capability is permission-aware, traceable, auditable, versioned, and recoverable.

---

## 2. Non-negotiable architecture rules

```text
Synexia is the only brain.
Agents are governed Harness Agent profiles.
Skills are executable capability packages.
MCP is an external connector protocol behind Zhanlu Gateway.
Multi-agent and swarm are Synexia-controlled orchestration patterns.
Artifacts are versioned PostgreSQL-backed business outputs.
Layer 5 executes. Layer 6 governs. Layer 7 runs the infrastructure.
```

### 2.1 What must never happen

```text
The LLM must not directly call arbitrary tools.
The LLM must not see every skill or MCP tool.
Agents must not directly access credentials.
Skills must not directly access raw server paths.
MCP servers must not bypass policy.
Swarm agents must not call each other freely without recorded handoff.
Generated files must not live only in Docker container folders.
Redis must not become the source of truth.
Sandbox output must not be trusted until validation passes.
```

### 2.2 What must always happen

```text
Every chat request becomes a sealed RequestEnvelope.
Every execution has execution_id, trace_id, plan_id, events, cost, and audit records.
Every agent invocation is scoped by org_id, app_id, user_id, conversation_id.
Every skill run uses approved inputs and produces validated outputs.
Every MCP call is permission-filtered and logged.
Every artifact has version, source refs, build manifest, validation report, and preview.
Every dashboard auto-update uses approved datasource bindings and DataSnapshots.
Every high-risk action passes policy and approval.
```

---

## 3. Concept model

| Concept | Meaning in Zhanlu | Stored where | Runtime rule |
|---|---|---|---|
| Synexia | Enterprise cognitive core and only orchestration brain | Code plus execution records | Plans and decides, but does not bypass policy |
| Agent | Governed Harness Agent profile | PostgreSQL for real agents, Git for system templates | Provides role, mission, scope, allowed skills, allowed data, allowed MCP tools |
| Skill | Governed capability package | PostgreSQL for custom skills, Git for system templates | Runs through Layer 5 and sandbox workers |
| MCP | Connector protocol to external tools, apps, data, resources | PostgreSQL MCP server registry and bindings | Exposed only through MCP Gateway and permission filters |
| Multi-Agent Team | Several bounded agents working on one plan | PostgreSQL | Synexia supervises agent selection and handoff |
| Swarm | Dynamic but bounded handoff between agents | PostgreSQL | Must have max steps, max cost, max runtime, allowed agents, stop conditions |
| Artifact | Generated output such as PPT, DOCX, MD, HTML, dashboard, chart, mini app | PostgreSQL | Versioned, previewable, editable, auditable |
| DataSnapshot | Immutable evidence from database/query result | PostgreSQL | Data-driven artifacts cite snapshots, not live mutable queries |
| Sandbox | Temporary Docker execution environment | Temporary filesystem only | Destroyed after execution |

---

## 4. Layer fit

```text
Layer 1: Enterprise Interaction and Identity Layer
  Receives user request, stores messages, creates RequestEnvelope, shows inline previews.

Layer 2: Synexia Enterprise Cognitive Core
  Understands goal, selects agent/team mode, creates TaskSpec and PlanDAG.

Layer 3: Enterprise Harness Agent, Skill and Data Runtime
  Loads agent profiles, skill bindings, data bindings, MCP bindings, capability catalog.

Layer 4: Enterprise Memory, Knowledge and Context Intelligence Layer
  Provides ContextManifest, memory, knowledge, DataSnapshots, artifact knowledge.

Layer 5: Enterprise Execution Layer
  Runs workflows, skills, MCP calls, sandbox jobs, artifact builds, approvals, notifications.

Layer 6: Enterprise Platform Services
  Provides identity, policy, cost, observability, AI governance, audit, risk, approval.

Layer 7: Docker, PostgreSQL and Redis Infrastructure Layer
  Runs services, PostgreSQL source of truth, Redis temporary queue/cache, Docker sandbox.
```

---

## 5. Product features

Zhanlu should expose these as first-class product modules:

```text
Agent Studio
Skill Studio
MCP Connector Studio
Multi-Agent Team Builder
Governed Agent Swarm
Artifact Workspace
Dashboard Auto-Update
Sandbox Execution Center
Approval Center
Execution Trace Viewer
```

---

## 6. Agent system

### 6.1 Agent definition

In Zhanlu, every agent is a Harness Agent.

```text
Agent = mission + role + task scope + allowed skills + allowed MCP tools + allowed data + memory scope + policy profile + output contract + evaluation rules
```

An agent is not an independent LLM service. It is a governed profile executed under Synexia.

### 6.2 Example: Finance Agent

```yaml
agent_id: finance-agent
name: Finance Agent
agent_type: functional
mission: Help users analyze finance data and create finance reports, PPTs, dashboards, and summaries.

scope:
  allowed_domains:
    - finance_reporting
    - budget_analysis
    - revenue_analysis
    - dashboard_generation
  forbidden_domains:
    - hr_salary_analysis
    - legal_advice
    - external_sharing_without_approval

allowed_skills:
  - governed-nl2sql
  - data-snapshot
  - chart-generation
  - company-finance-ppt
  - dashboard-generation
  - pdf-preview
  - artifact-validation

allowed_mcp_tools:
  - finance-postgres.readonly_query
  - finance-postgres.schema_summary
  - company-drive.read_templates

allowed_datasources:
  - finance_postgres

memory_scope:
  user_private: false
  app_shared: true
  org_shared: false

policy_profile:
  risk_tier: medium
  approval_required_for_external_share: true
  approval_required_for_app_publish: true
  database_write_allowed: false

output_contract:
  allowed_artifact_types:
    - pptx
    - docx
    - md
    - dashboard
    - chart
    - pdf
```

### 6.3 Agent storage

System/default agent templates live in Git:

```text
agent_library/
  system/
    executive/
      ceo-briefing-agent/
        AGENT.md
        manifest.yaml
        prompts/
        tests/
    functional/
      finance-agent/
      hr-agent/
      sales-agent/
    specialist/
      data-analyst-agent/
      report-agent/
      compliance-agent/
```

Real user-created agents live in PostgreSQL:

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

### 6.4 Agent lifecycle

```text
draft → testing → review_required → approved → active → deprecated → archived
```

### 6.5 Agent invariants

```text
AGT-0: Every Zhanlu agent is a Harness Agent.
AGT-1: Agents are governed profiles, not independent reasoning services.
AGT-2: Agents cannot expand their own permissions.
AGT-3: Agents can use only bound skills, bound datasources, bound MCP tools, and allowed memory scopes.
AGT-4: User-created agents are stored as PostgreSQL records, not permanent server folders.
AGT-5: Every agent invocation is recorded.
AGT-6: Agent output becomes trusted only after validation, approval, or accepted execution state.
```

---

## 7. Skill system

### 7.1 Skill definition

A skill is a governed capability package.

```text
Skill = capability + input schema + output schema + assets + scripts + validators + permissions + runtime limits + review status
```

Skills do the specialized work:

```text
pptx-generation
docx-generation
markdown-generation
html-generation
dashboard-generation
mini-app-generation
chart-generation
pdf-preview
artifact-validation
governed-nl2sql
data-snapshot
template-extraction
```

### 7.2 Skill is different from agent

```text
Agent = who is doing the work.
Skill = what capability it can use.
```

Example:

```text
Finance Agent can use:
  governed-nl2sql skill
  data-snapshot skill
  chart-generation skill
  company-finance-ppt skill
  dashboard-generation skill
  pdf-preview skill
  artifact-validation skill
```

The Finance Agent does not directly create a PPT. It calls the approved PPT skill through the Tool/Skill Gateway.

### 7.3 Built-in skill package structure

System skills live in Git:

```text
skill_library/
  system/
    artifact/
      pptx-generation/
        SKILL.md
        manifest.yaml
        schemas/
          input.schema.json
          output.schema.json
        scripts/
          build_ppt.py
        validators/
          validate_ppt.py
        assets/
          default_template.pptx
        references/
          slide_mapping.md
          brand_rules.md
        tests/
          sample_input.json
          expected_output.json

      docx-generation/
      markdown-generation/
      html-generation/
      dashboard-generation/
      mini-app-generation/
      pdf-preview/
      artifact-validation/

    data/
      governed-nl2sql/
      data-snapshot/
      metric-resolution/

    visualization/
      chart-generation/
```

### 7.4 Custom skill storage

User-created skills live in PostgreSQL, not permanent server folders.

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

Runtime flow:

```text
skill package in PostgreSQL
→ materialize into temporary sandbox folder
→ run inside Docker sandbox
→ collect output package
→ validate output
→ store artifact output in PostgreSQL
→ delete temporary folder and container
```

### 7.5 SKILL.md versus manifest.yaml

`SKILL.md` is guidance for the skill.

`manifest.yaml` is the enforceable contract.

Example:

```yaml
skill_id: pptx-generation
name: PPTX Generation
version: 1.0.0
artifact_types:
  - pptx

runtime:
  type: sandbox
  entrypoint: scripts/build_ppt.py
  timeout_seconds: 120
  memory_mb: 1024
  network: disabled

input_schema: schemas/input.schema.json
output_schema: schemas/output.schema.json

permissions:
  requires_sandbox: true
  allowed_file_types:
    - pptx
    - json
    - png
  can_access_datasource: false
  can_write_artifact: true
  can_send_external: false

validation:
  validators:
    - validators/validate_ppt.py
  require_preview: true
  require_build_manifest: true
  require_source_refs: true

governance:
  review_required_for_custom_template: true
  approval_required_for_app_publish: true
```

### 7.6 Skill execution flow

```text
1. Synexia creates PlanDAG.
2. PlanDAG includes skill_run nodes.
3. Layer 5 Workflow Engine starts execution.
4. Tool/Skill Gateway checks permissions.
5. Skill preflight runs.
6. Skill package is materialized into sandbox.
7. Skill receives approved input package.
8. Skill runs.
9. Output is collected.
10. Output validation runs.
11. Artifact is stored in PostgreSQL.
12. Inline preview appears in chat.
```

### 7.7 Skill input package

Example PPT input:

```json
{
  "artifact_type": "pptx",
  "title": "Q2 Finance Performance Report",
  "language": "English",
  "template_artifact_id": "template_uuid",
  "data_snapshot_ids": ["snapshot_1", "snapshot_2"],
  "charts": ["chart_artifact_1"],
  "slide_outline": [
    {
      "title": "Executive Summary",
      "purpose": "summarize Q2 performance"
    },
    {
      "title": "Revenue Trend",
      "purpose": "show revenue growth by month"
    }
  ],
  "brand_rules": {
    "font": "Inter",
    "logo_position": "top-right"
  }
}
```

### 7.8 Skill output package

```json
{
  "status": "success",
  "artifact_type": "pptx",
  "files": [
    {
      "kind": "original",
      "file_name": "Q2_Finance_Report.pptx",
      "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    },
    {
      "kind": "preview_pdf",
      "file_name": "Q2_Finance_Report_preview.pdf",
      "mime_type": "application/pdf"
    }
  ],
  "source_refs": {
    "data_snapshots": ["snapshot_1", "snapshot_2"],
    "template": "template_uuid"
  },
  "build_manifest": {},
  "validation_report": {}
}
```

### 7.9 Skill preflight

Before running a skill, Zhanlu checks:

```text
Is the skill approved?
Is this agent allowed to use it?
Is the user allowed to request it?
Does it need sandbox?
Does it need datasource access?
Does it need approval?
Are required inputs present?
Is the template available?
Is the cost acceptable?
Is the output type allowed?
```

Preflight result:

```text
ready
warning
blocked
```

### 7.10 Skill invariants

```text
SKL-0: Skills are governed capability packages.
SKL-1: Skills are not independent agents.
SKL-2: Skills run only through Tool/Skill Gateway.
SKL-3: Custom skills are stored as PostgreSQL package versions.
SKL-4: Skills are materialized only into temporary sandbox folders during execution.
SKL-5: Skills receive handles and approved input packages, not credentials or raw server paths.
SKL-6: Code skills and artifact-building skills require sandbox execution.
SKL-7: Skill output is not trusted until validation passes.
SKL-8: Data-driven skill outputs must link to DataSnapshots.
SKL-9: Every skill run is recorded and auditable.
```

---

## 8. MCP connector system

### 8.1 MCP role in Zhanlu

MCP is an external connector protocol. It should not replace Zhanlu skills and should not sit directly in front of the model.

```text
MCP = standardized connector layer for external tools, resources, data sources, prompts, APIs, and workflows.
```

Correct Zhanlu placement:

```text
Synexia PlanDAG
→ Tool/Skill/MCP Gateway
→ MCP Gateway
→ approved MCP server
→ external tool/data/app
→ result returned
→ ObservationRecord stored
→ audit event stored
```

### 8.2 MCP versus skill

```text
Skill = Zhanlu-native capability package.
MCP = external connector endpoint.
```

Examples:

```text
pptx-generation = Skill
docx-generation = Skill
dashboard-generation = Skill
mini-app-generation = Skill
governed-nl2sql = Skill
pdf-preview = Skill

Google Drive connector = MCP server
Slack connector = MCP server
GitHub connector = MCP server
ERP connector = MCP server
Database connector = MCP server or native datasource connector
```

A skill may use MCP internally, but only through Zhanlu Gateway.

### 8.3 MCP Gateway folder

```text
backend/
  mcp_gateway/
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

### 8.4 MCP database tables

```text
mcp_servers
mcp_tools
mcp_resources
mcp_prompts
mcp_server_permissions
agent_mcp_bindings
mcp_call_logs
```

### 8.5 Example MCP server scope

```text
finance-postgres-mcp
google-drive-mcp
slack-mcp
github-mcp
erp-mcp
internal-document-mcp
```

Finance Agent may see only:

```text
finance_postgres.query_readonly
finance_postgres.get_schema_summary
finance_postgres.get_metric_definition
company_drive.read_templates
```

Finance Agent must not see:

```text
hr_postgres.query
payroll.export
slack.send_message
external_email.send
```

### 8.6 MCP security rules

```text
MCP-0: MCP servers are never exposed directly to the model.
MCP-1: MCP servers must be registered before use.
MCP-2: MCP tools are filtered by org, app, agent, user, data sensitivity, and policy.
MCP-3: Tool allowlists are deny-by-default.
MCP-4: MCP call inputs and outputs are schema-validated.
MCP-5: Secrets remain in Zhanlu secret management, not in prompts, agents, skills, or MCP messages.
MCP-6: Every MCP call produces audit and observation records.
MCP-7: MCP resources are evidence, not instruction.
MCP-8: High-risk MCP actions require approval before execution.
```

---

## 9. Multi-agent team system

### 9.1 Definition

Multi-agent means several governed Harness Agents cooperate on one task.

```text
Multi-agent does not mean many independent brains.
Multi-agent means many bounded agent profiles coordinated by Synexia.
```

### 9.2 Multi-agent modes

#### Mode 1: Single Agent

Default mode.

```text
Finance Agent handles the task alone.
```

Use for:

```text
simple document summary
simple markdown generation
small PPT generation
single finance question
```

#### Mode 2: Supervisor and Specialist Agents

```text
Synexia
  → Finance Agent
  → Data Analyst Agent
  → Report Agent
  → Review Agent
```

Use for:

```text
finance PPT
business report
data dashboard
mini app
```

#### Mode 3: Pipeline Agents

```text
Research Agent → Data Agent → Writer Agent → Artifact Agent → Reviewer Agent
```

Use for:

```text
DOCX report
research summary
business proposal
market analysis
```

#### Mode 4: Review and Critic Agents

```text
Report Agent creates PPT.
Compliance Agent checks risk.
Quality Agent checks output.
Finance Agent checks numbers.
```

Use for high-trust enterprise output.

#### Mode 5: Dynamic handoff

```text
Finance Agent notices chart problem
→ handoff to Data Analyst Agent
Data Analyst notices template problem
→ handoff to Report Agent
Report Agent notices policy issue
→ handoff to Compliance Agent
```

Dynamic handoff is allowed only inside the approved team.

### 9.3 Structured handoff packet

Agents must not communicate through uncontrolled natural language only.

Use structured handoff packets:

```json
{
  "handoff_type": "data_analysis_request",
  "from_agent": "finance_agent",
  "to_agent": "data_analyst_agent",
  "goal": "Create Q2 revenue and margin analysis",
  "constraints": {
    "datasource": "finance_postgres",
    "allowed_tables": ["revenue", "cost", "budget"],
    "output": "data_snapshot"
  },
  "required_output_schema": "data_analysis_summary_v1"
}
```

### 9.4 Multi-agent database tables

```text
multi_agent_runs
multi_agent_members
multi_agent_handoffs
multi_agent_messages
multi_agent_decisions
multi_agent_events
multi_agent_reviews
```

### 9.5 Multi-agent invariants

```text
MAG-0: Multi-agent teams are coordinated by Synexia.
MAG-1: Team membership is explicit.
MAG-2: Every handoff is stored.
MAG-3: Every receiving agent checks its own permissions.
MAG-4: No agent can expand its own scope.
MAG-5: No agent can call another agent outside the approved team.
MAG-6: Human approval may pause the handoff if risk is high.
MAG-7: Multi-agent state is stored in PostgreSQL.
```

---

## 10. Governed Agent Swarm

### 10.1 Definition

A swarm is a dynamic multi-agent mode where agents can hand off work to the most suitable specialist, but only inside strict Zhanlu boundaries.

Zhanlu should call this feature:

```text
Governed Agent Swarm
```

### 10.2 Swarm boundaries

```text
Swarm has a swarm_id.
Swarm has one main goal.
Swarm has supervisor_type = synexia.
Swarm has allowed agents only.
Swarm has max steps.
Swarm has max cost.
Swarm has max runtime.
Swarm has allowed skills.
Swarm has allowed MCP tools.
Swarm has stop conditions.
Swarm has required approval points.
Swarm writes all events to PostgreSQL.
```

### 10.3 Bad swarm versus Zhanlu swarm

Bad swarm:

```text
Any agent can call any agent.
Any agent can call any tool.
Agents loop forever.
No audit.
No approval.
No budget.
```

Zhanlu swarm:

```text
Synexia creates SwarmRun.
Only selected agents participate.
Each handoff is a recorded event.
Each agent can use only allowed skills.
Each external action passes policy.
Swarm stops when done, blocked, failed, or budget exceeded.
```

### 10.4 Swarm database tables

```text
swarm_runs
swarm_agents
swarm_handoffs
swarm_messages
swarm_decisions
swarm_budgets
swarm_events
swarm_stop_records
```

Example:

```sql
CREATE TABLE swarm_runs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    goal TEXT NOT NULL,
    supervisor_type TEXT NOT NULL DEFAULT 'synexia',
    status TEXT NOT NULL DEFAULT 'running',
    max_steps INT NOT NULL DEFAULT 20,
    max_cost_cents INT,
    max_runtime_seconds INT NOT NULL DEFAULT 300,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
CREATE TABLE swarm_handoffs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    swarm_run_id UUID NOT NULL,
    from_agent_id UUID,
    to_agent_id UUID NOT NULL,
    reason TEXT NOT NULL,
    handoff_payload JSONB NOT NULL,
    approved_by_policy BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 10.5 Swarm stop conditions

```text
goal_completed
max_steps_reached
max_cost_reached
max_runtime_reached
policy_blocked
approval_required
validation_failed
human_cancelled
system_error
```

### 10.6 Swarm invariants

```text
SWM-0: Swarm is bounded dynamic orchestration, not open-ended autonomy.
SWM-1: Synexia supervises every swarm.
SWM-2: Swarm agents are selected from approved Harness Agent profiles.
SWM-3: Swarm handoffs are structured and stored.
SWM-4: Swarm cannot expand skills, MCP tools, data, memory, or permissions.
SWM-5: Swarm must have max steps, max runtime, max cost, and stop conditions.
SWM-6: Swarm side effects require policy evaluation and approval when necessary.
SWM-7: Swarm events are stored in PostgreSQL and visible in Execution Trace Viewer.
```

---

## 11. Artifact and App Workspace

### 11.1 Everything becomes an artifact

Zhanlu should treat every generated output as a governed Artifact.

Artifact types:

```text
pptx
docx
pdf
md
html
xlsx
chart
dashboard
mini_app
data_view
report
image
```

### 11.2 Artifact lifecycle

```text
draft → building → preview_ready → editing → validated → approved → published/exported → archived
```

### 11.3 Artifact engine components

Inside Layer 5:

```text
Artifact and App Execution Engine
  PPT Builder
  DOCX Builder
  Markdown Builder
  HTML Builder
  Dashboard Builder
  Mini App Builder
  Chart Builder
  Preview Builder
  Inline Editor
  Version Manager
  Validation Engine
  Publishing Engine
  Auto-Update Engine
```

### 11.4 Inline chat preview types

| Artifact type | Preview method | Edit method |
|---|---|---|
| PPTX | PDF preview plus slide thumbnails | Edit slide, regenerate slide, export PDF |
| DOCX | PDF preview plus document outline | Edit section, rewrite paragraph, export PDF |
| MD | Rendered markdown preview | Raw markdown editor and chat edit commands |
| HTML | Sandboxed iframe preview | Edit design, section, HTML/CSS/JS |
| Dashboard | Interactive card or iframe | Edit metrics, charts, filters, layout |
| Mini app | Sandboxed iframe runtime | Edit features, data bindings, permissions |
| Chart | Inline chart card | Change chart type, axis, filter, data source |

### 11.5 Artifact card in chat

```text
Q2 Finance Report.pptx

Status: Preview ready
Version: v1
Created by: Finance Agent
Sources: 3 DataSnapshots, 1 template
Actions:
Preview · Edit · Regenerate · Compare Versions · Approve · Publish · Download · Share · Schedule Update
```

### 11.6 Artifact APIs

```text
GET  /api/v1/artifacts/{artifact_id}
GET  /api/v1/artifacts/{artifact_id}/preview
GET  /api/v1/artifacts/{artifact_id}/versions
POST /api/v1/artifacts/{artifact_id}/edit
POST /api/v1/artifacts/{artifact_id}/regenerate
POST /api/v1/artifacts/{artifact_id}/approve
POST /api/v1/artifacts/{artifact_id}/publish
GET  /api/v1/artifacts/{artifact_id}/download
POST /api/v1/artifacts/{artifact_id}/schedule-update
```

### 11.7 Artifact storage tables

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
dashboard_definitions
dashboard_refresh_runs
mini_app_definitions
mini_app_runtime_sessions
artifact_edit_requests
artifact_publish_records
```

Important fields:

```text
artifact_id
artifact_type
version
status
visibility
source_json
build_manifest
validation_report
preview_blob_id
data_snapshot_ids
skill_version_id
template_version_id
created_by_agent_id
created_by_execution_id
```

### 11.8 Artifact invariants

```text
ART-0: Every generated output is a governed Artifact.
ART-1: Artifacts are versioned.
ART-2: Artifacts are previewed through permission-checked APIs, not raw file paths.
ART-3: Artifact binaries and previews are PostgreSQL-backed in strict DB-first mode.
ART-4: Large blob storage may be used later, but PostgreSQL remains source of truth.
ART-5: Data-driven artifacts cite DataSnapshots.
ART-6: Artifacts are not trusted until validation passes.
ART-7: Publishing or external sharing may require approval.
ART-8: Artifact edits create new versions, not silent overwrites.
ART-9: Artifact interactions are logged.
```

---

## 12. Feature: making PPT

User request:

```text
Make a Q2 finance PPT report.
```

Execution flow:

```text
1. Layer 1 stores user message.
2. Layer 1 creates RequestEnvelope.
3. Synexia creates TaskSpec with artifact_type = pptx.
4. Synexia selects Finance Agent or multi-agent team.
5. Synexia creates PlanDAG.
6. Data Analyst Agent runs governed-nl2sql skill if data is needed.
7. DataSnapshot skill stores query result.
8. Chart skill creates charts.
9. PPT skill receives outline, template, charts, data snapshots, brand rules.
10. Sandbox worker generates PPTX.
11. Preview builder converts PPTX to PDF and thumbnails.
12. Validation engine checks file, placeholders, sources, and safety.
13. PostgreSQL stores original PPTX, preview PDF, thumbnails, build manifest, validation report.
14. Assistant message is created with message_artifacts link.
15. Frontend receives artifact.preview_ready event.
16. User previews inline, edits, regenerates, approves, downloads, or publishes.
```

Supported edit commands:

```text
Make slide 3 simpler.
Change this to a 5-slide version.
Use more business style.
Replace chart on slide 4.
Translate to Chinese.
Use company template.
Add speaker notes.
Export as PDF.
```

---

## 13. Feature: making DOCX

DOCX flow:

```text
1. Generate report structure.
2. Build sections.
3. Add tables, figures, citations, appendix if needed.
4. Use company DOCX template.
5. Generate DOCX in sandbox.
6. Convert to PDF preview.
7. Store DOCX and preview in PostgreSQL.
8. Show inline document preview.
```

Supported edit commands:

```text
Make the introduction more formal.
Add an executive summary.
Shorten section 2.
Add table of contents.
Use company report template.
Export as PDF.
```

---

## 14. Feature: making MD

Markdown should be first-class because it is easy to diff, edit, version, convert, and regenerate.

MD flow:

```text
1. Generate markdown content.
2. Store artifact_type = md.
3. Render markdown as HTML preview.
4. Allow inline editing in chat/canvas.
5. Export to DOCX, PDF, HTML, or PPT if needed.
```

Use cases:

```text
architecture documents
meeting notes
project specs
research summaries
technical reports
README files
product requirements
```

---

## 15. Feature: making HTML

HTML flow:

```text
1. Generate HTML/CSS/JS.
2. Store source as artifact version.
3. Run security scan.
4. Render inside sandboxed iframe.
5. Disable unsafe scripts unless allowed.
6. Allow user to edit/regenerate sections.
7. Export as static page or PDF.
```

Security rule:

```text
HTML preview must run in a sandboxed iframe with restricted network access.
```

---

## 16. Feature: dashboards

### 16.1 Dashboard types

```text
static dashboard
interactive dashboard
live dashboard
scheduled-refresh dashboard
real-time dashboard
```

### 16.2 Dashboard flow

```text
1. User asks for dashboard.
2. Synexia identifies KPIs and data sources.
3. Datasource engine creates semantic queries.
4. DataSnapshots or DataViews are created.
5. Dashboard definition is generated.
6. Charts, cards, tables, and filters are rendered.
7. Inline preview appears in chat.
8. User edits layout or metrics.
9. User approves and publishes.
```

### 16.3 Dashboard definition

```json
{
  "artifact_type": "dashboard",
  "title": "Q2 Finance Dashboard",
  "data_sources": ["finance_postgres"],
  "data_snapshot_ids": ["snapshot_1"],
  "cards": [
    {
      "id": "revenue_card",
      "type": "kpi",
      "metric": "total_revenue"
    },
    {
      "id": "margin_chart",
      "type": "line_chart",
      "metric": "gross_margin",
      "group_by": "month"
    }
  ],
  "filters": ["department", "month"],
  "refresh_policy": {
    "mode": "scheduled_refresh",
    "schedule": "every Monday 08:00"
  }
}
```

---

## 17. Feature: automatic dashboard update

User request:

```text
Make a sales dashboard and update it every morning.
```

Auto-update flow:

```text
1. Dashboard artifact is created.
2. Datasource binding is approved.
3. Refresh policy is stored.
4. Automation Engine schedules refresh.
5. Every refresh creates a new DataSnapshot.
6. Dashboard refresh skill updates dashboard version.
7. Notification skill alerts user if important changes appear.
```

Update modes:

```text
manual_refresh
scheduled_refresh
event_driven_refresh
```

Dashboard auto-update rules:

```text
Dashboard refresh uses approved datasource bindings.
Dashboard refresh creates new DataSnapshots.
Dashboard refresh does not mutate historical snapshots.
Dashboard version changes are stored.
Important changes can trigger notifications.
Publishing dashboard to app_shared may require approval.
```

---

## 18. Feature: mini apps

A mini app is a generated interactive tool, not just a file.

Examples:

```text
ROI calculator
finance report generator
student lesson planner
HR policy Q&A app
sales forecast simulator
inventory risk dashboard
customer support triage tool
```

Mini app flow:

```text
1. User describes the app.
2. Synexia creates app specification.
3. Mini App Builder generates frontend/backend logic.
4. Sandbox runs the app.
5. Preview appears in sandboxed iframe.
6. User edits through chat.
7. Validation and security checks run.
8. User approves.
9. Mini app becomes app_shared artifact if published.
```

Mini app artifact stores:

```text
source code
app manifest
input/output schema
permissions
data bindings
runtime image or runtime spec
preview URL or preview blob
version history
validation report
published status
```

Mini app manifest example:

```yaml
mini_app_id: roi-calculator
runtime: sandboxed_iframe
permissions:
  network: disabled
  datasource_access: none
inputs:
  - investment_amount
  - expected_return
  - period
outputs:
  - roi_percentage
  - summary
```

Mini app security rule:

```text
Mini apps cannot access enterprise data unless they have explicit approved data bindings.
```

---

## 19. Inline editing and partial regeneration

Modern AI should support partial editing, not only full regeneration.

User commands:

```text
Edit slide 4.
Regenerate only the conclusion.
Change this chart.
Make this dashboard card bigger.
Add a filter for department.
Make the HTML more professional.
Turn this markdown into a DOCX.
Turn this dashboard into a PPT.
```

Structured artifact parts:

```text
PPT:
  slides, placeholders, charts, tables, speaker notes, theme, sources

DOCX:
  sections, paragraphs, tables, figures, references, appendices

Dashboard:
  cards, charts, filters, data_views, layout, refresh_policy

Mini app:
  pages, components, state, actions, data bindings, permissions
```

Zhanlu should store:

```text
artifact_source_json
artifact_source_parts
```

This allows regeneration of one slide, one section, one chart, or one dashboard card without rebuilding the entire artifact.

---

## 20. End-to-end example: PPT plus dashboard plus weekly update

User says:

```text
Make a Q2 finance PPT, create a dashboard from the same data, and update the dashboard every Monday morning.
```

Execution:

```text
Synexia selects multi-agent team:
  Finance Agent
  Data Analyst Agent
  Report Agent
  Dashboard Agent
  Review Agent

Finance Agent:
  defines finance KPIs and business meaning

Data Analyst Agent:
  uses governed-nl2sql skill
  uses finance-postgres MCP tool
  creates DataSnapshots

Report Agent:
  uses company-finance-ppt skill
  creates PPTX artifact

Dashboard Agent:
  uses dashboard-generation skill
  creates dashboard artifact
  sets refresh policy

Review Agent:
  uses artifact-validation skill
  checks data references and formatting

Automation Engine:
  schedules weekly dashboard refresh

Artifact system:
  stores PPT, preview PDF, dashboard, chart data, versions

Chat UI:
  shows inline PPT preview and dashboard preview
```

Final result:

```text
PPT is ready.
Dashboard is ready.
Weekly auto-update is scheduled.
All sources are linked to DataSnapshots.
```

---

## 21. Database-first storage model

### 21.1 Git repository stores

```text
system code
system prompts
system policies
system skill templates
system agent templates
example packages
Docker and infrastructure files
documentation
```

### 21.2 PostgreSQL stores

```text
user data
organizations
apps
conversations
messages
request_envelopes
executions
plan_node_runs
agent profiles
custom agent versions
skill profiles
custom skill packages
MCP server configs
MCP bindings
multi-agent runs
swarm runs
memory and knowledge
DataSnapshots
artifacts
artifact blobs
artifact previews
artifact versions
audit logs
cost ledger
policy decisions
approval records
```

### 21.3 Redis stores temporary data only

```text
queues
locks
stream coordination
worker heartbeat
rate limit
temporary cache
```

Redis must never store permanent business truth.

### 21.4 Docker sandbox stores temporary files only

```text
temporary skill package materialization
temporary input package
temporary output files
temporary preview conversion files
```

Sandbox files are deleted after execution.

---

## 22. Core database table groups

```text
identity:
  organizations, users, groups, app_grants, sessions

chat:
  conversations, messages, request_envelopes, message_artifacts

execution:
  executions, plan_runs, plan_node_runs, execution_events, workflow_runs

agents:
  agent_profiles, agent_versions, agent_skill_bindings, agent_data_bindings, agent_mcp_bindings, agent_invocations

skills:
  skill_profiles, skill_versions, skill_package_versions, skill_runs, skill_validation_reports

mcp:
  mcp_servers, mcp_tools, mcp_resources, mcp_prompts, agent_mcp_bindings, mcp_call_logs

multi-agent:
  multi_agent_runs, multi_agent_members, multi_agent_handoffs, multi_agent_events

swarm:
  swarm_runs, swarm_agents, swarm_handoffs, swarm_events, swarm_budgets

artifacts:
  artifacts, artifact_versions, artifact_blobs, artifact_previews, artifact_source_parts, artifact_interactions

dashboards:
  dashboard_definitions, dashboard_refresh_runs

mini apps:
  mini_app_definitions, mini_app_runtime_sessions

knowledge:
  memory_items, knowledge_items, data_snapshots, context_manifests, decision_memory, experience_entries

governance:
  policy_decisions, approval_requests, approval_records, audit_logs, risk_events, cost_ledger
```

---

## 23. Event model

Zhanlu should emit events for UI streaming, audit, debugging, and recovery.

Example event:

```json
{
  "id": "event_uuid",
  "type": "artifact.preview_ready",
  "source": "zhanlu.execution",
  "time": "2026-07-08T10:30:00Z",
  "org_id": "org_uuid",
  "app_id": "app_uuid",
  "conversation_id": "conversation_uuid",
  "execution_id": "execution_uuid",
  "node_run_id": "node_run_uuid",
  "data": {
    "artifact_id": "artifact_uuid",
    "artifact_version_id": "artifact_version_uuid",
    "preview_kind": "pdf"
  }
}
```

Important event types:

```text
execution.started
execution.node_started
execution.node_completed
execution.node_failed
execution.paused_for_approval
approval.requested
approval.approved
approval.rejected
skill.started
skill.completed
skill.failed
mcp.call_started
mcp.call_completed
mcp.call_blocked
swarm.started
swarm.handoff
swarm.stopped
sandbox.started
sandbox.completed
artifact.created
artifact.preview_ready
artifact.validation_failed
artifact.published
dashboard.refresh_started
dashboard.refresh_completed
mini_app.preview_ready
execution.completed
execution.failed
```

---

## 24. Project folder updates

Add these folders to the monorepo:

```text
zhanlu/
  backend/
    agents_runtime/
    skills_runtime/
    mcp_gateway/
    multi_agent/
    swarm_runtime/
    artifacts/
    execution/
    sandbox/

  agent_library/
    system/
    examples/

  skill_library/
    system/
    examples/

  mcp_library/
    system/
    examples/

  prompts/
    agent_factory/
    skill_factory/
    multi_agent/
    swarm/
    artifact_generation/

  policies/
    default.yaml
    artifact.yaml
    skill_review.yaml
    datasource.yaml
    memory.yaml
    mcp.yaml
    multi_agent.yaml
    swarm.yaml
    model_routing.yaml
```

### 24.1 Runtime code folders

```text
backend/agents_runtime/
  registry.py
  manifest.py
  studio_service.py
  invocation.py
  evaluation.py
  hooks.py

backend/skills_runtime/
  registry.py
  manifest_parser.py
  skill_md_parser.py
  factory_service.py
  runner.py
  validation.py
  sandbox_adapter.py
  hooks.py

backend/mcp_gateway/
  registry.py
  server_manager.py
  tool_mapper.py
  permission_filter.py
  call_executor.py
  audit.py

backend/multi_agent/
  supervisor.py
  team_builder.py
  handoff.py
  collaboration_protocol.py
  agent_router.py
  critic_review.py
  policy.py

backend/swarm_runtime/
  swarm_run.py
  swarm_state.py
  swarm_handoff.py
  swarm_budget.py
  swarm_events.py
  swarm_stop_conditions.py
```

---

## 25. API surface

### 25.1 Agent APIs

```text
GET    /api/v1/agents
POST   /api/v1/agents
GET    /api/v1/agents/{agent_id}
POST   /api/v1/agents/{agent_id}/test
POST   /api/v1/agents/{agent_id}/publish
POST   /api/v1/agents/{agent_id}/bind-skill
POST   /api/v1/agents/{agent_id}/bind-datasource
POST   /api/v1/agents/{agent_id}/bind-mcp
```

### 25.2 Skill APIs

```text
GET    /api/v1/skills
POST   /api/v1/skills
GET    /api/v1/skills/{skill_id}
POST   /api/v1/skills/{skill_id}/test
POST   /api/v1/skills/{skill_id}/review
POST   /api/v1/skills/{skill_id}/publish
POST   /api/v1/skills/import
POST   /api/v1/skills/create-from-template
```

### 25.3 MCP APIs

```text
GET    /api/v1/mcp/servers
POST   /api/v1/mcp/servers
GET    /api/v1/mcp/servers/{server_id}/tools
POST   /api/v1/mcp/servers/{server_id}/test
POST   /api/v1/mcp/servers/{server_id}/approve
POST   /api/v1/mcp/bind-agent
```

### 25.4 Multi-agent and swarm APIs

```text
POST   /api/v1/multi-agent/team
POST   /api/v1/multi-agent/run
GET    /api/v1/multi-agent/runs/{run_id}

POST   /api/v1/swarms/run
GET    /api/v1/swarms/{swarm_id}
POST   /api/v1/swarms/{swarm_id}/cancel
```

### 25.5 Artifact APIs

```text
GET    /api/v1/artifacts/{artifact_id}
GET    /api/v1/artifacts/{artifact_id}/preview
GET    /api/v1/artifacts/{artifact_id}/versions
POST   /api/v1/artifacts/{artifact_id}/edit
POST   /api/v1/artifacts/{artifact_id}/regenerate
POST   /api/v1/artifacts/{artifact_id}/approve
POST   /api/v1/artifacts/{artifact_id}/publish
GET    /api/v1/artifacts/{artifact_id}/download
POST   /api/v1/artifacts/{artifact_id}/schedule-update
```

---

## 26. Governance and preflight

Every high-impact capability goes through preflight.

Preflight checks:

```text
user permission
agent permission
skill permission
MCP permission
datasource binding
memory scope
data sensitivity
policy requirements
approval requirements
cost budget
sandbox requirement
network requirement
output artifact type
validation requirement
```

Preflight result:

```text
ready
warning
blocked
```

Example blocked message:

```text
Blocked: This mini app requires access to finance_postgres, but the selected agent is not bound to that datasource.
```

---

## 27. Implementation phases

### Phase 1: Artifact foundation

```text
MD artifact
HTML artifact
inline preview card
artifact versioning
PostgreSQL storage
basic skill runner
```

### Phase 2: Office artifact generation

```text
PPTX generation
DOCX generation
PDF preview
thumbnail generation
download/export
artifact validation
```

### Phase 3: Agent and skill studio

```text
agent_library
skill_library
Agent Studio
Skill Studio
custom agent storage
custom skill package storage
skill review flow
```

### Phase 4: Dashboard artifacts

```text
governed-nl2sql
data snapshots
chart generation
dashboard generation
scheduled refresh
dashboard refresh runs
```

### Phase 5: MCP connector system

```text
MCP Gateway
MCP registry
MCP permission filters
MCP call logs
agent_mcp_bindings
connector testing
```

### Phase 6: Multi-agent and swarm

```text
Multi-Agent Team Builder
structured handoffs
review agent
critic agent
Governed Agent Swarm
swarm budget and stop conditions
Execution Trace Viewer
```

### Phase 7: Mini app system

```text
mini app builder
sandboxed iframe preview
mini app validation
publishing workflow
app_shared artifacts
permissioned data bindings
```

---

## 28. Full system invariants

```text
SYS-0: Synexia is the only cognitive controller.
SYS-1: Agents, skills, MCP, multi-agent teams, and swarms cannot bypass Synexia.
SYS-2: Agents are Harness Agent profiles, not uncontrolled autonomous services.
SYS-3: Skills are governed capability packages.
SYS-4: MCP servers are external connectors behind Tool/Skill/MCP Gateway.
SYS-5: Multi-agent teams are explicit and bounded.
SYS-6: Swarms are dynamic but governed by max steps, max runtime, max cost, allowed agents, and stop conditions.
SYS-7: PostgreSQL is the source of truth for user data, agents, skills, MCP configs, executions, artifacts, memory, audit, and governance.
SYS-8: Redis is temporary infrastructure only.
SYS-9: Docker sandbox filesystem is temporary only.
SYS-10: Every generated output is a versioned Artifact.
SYS-11: Inline preview is served through permission-checked APIs, not raw file paths.
SYS-12: Data-driven artifacts cite DataSnapshots, not live mutable queries.
SYS-13: Side-effect actions require policy evaluation.
SYS-14: High-risk actions require approval.
SYS-15: Every agent invocation, skill run, MCP call, swarm handoff, artifact build, and dashboard refresh is auditable.
```

---

## 29. Final architecture principle

Zhanlu supports Skills, MCP, Agents, Multi-Agent Teams, Governed Agent Swarms, and modern Artifact generation through one governed orchestration model.

Synexia remains the only cognitive controller. Agents are Harness Agent profiles with bounded missions, data bindings, skill bindings, MCP bindings, memory scope, and policy profiles. Skills are versioned capability packages executed through Layer 5 and Docker sandbox workers. MCP servers are external connector endpoints registered behind the Tool/Skill/MCP Gateway. Multi-agent teams and swarms are Synexia-controlled execution patterns with structured handoffs, budgets, stop conditions, approvals, audit logs, and PostgreSQL-backed state.

PPT, DOCX, MD, HTML, dashboards, charts, and mini apps are not just files. They are governed Artifacts: versioned, previewed inline in chat, editable, regenerable, linked to DataSnapshots, templates, skills, agents, executions, validation reports, and audit records.

No agent, skill, MCP server, multi-agent team, swarm, dashboard, mini app, or artifact can bypass identity, policy, memory, execution, storage, or governance controls.

---

## 30. References and implementation inspirations

These external systems inspired parts of the design, but Zhanlu should implement its own enterprise-governed version.

- Model Context Protocol official documentation: https://modelcontextprotocol.io/docs/getting-started/intro
- OpenAI Agents SDK documentation: https://openai.github.io/openai-agents-python/
- OpenAI Agents guide: https://developers.openai.com/api/docs/guides/agents
- LangGraph Multi-Agent Supervisor: https://reference.langchain.com/python/langgraph-supervisor
- LangGraph Multi-Agent Swarm: https://reference.langchain.com/python/langgraph-swarm
- LangGraph framework: https://github.com/langchain-ai/langgraph
- Claude file creation and editing: https://support.claude.com/en/articles/12111783-create-and-edit-files-with-claude
- Claude Artifacts: https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them
- Gotenberg document conversion: https://gotenberg.dev/
- ONLYOFFICE document editors: https://www.onlyoffice.com/
- Streamlit data apps: https://streamlit.io/
- PostgreSQL LISTEN/NOTIFY: https://www.postgresql.org/docs/current/sql-notify.html
- Materialize live data layer: https://github.com/MaterializeInc/materialize
