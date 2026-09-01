# Zhanlu Capabilities Instead of Raw Skills UI

## Purpose

This document defines the final UI decision for agent creation in Zhanlu:

**Normal users should select high-level capabilities, not raw internal skills.**

The existing UI currently shows a technical `Skills` section where users can bind internal skill names such as `vercel-cli`, `agent-browser`, or other native runtime tools. This is too technical for normal business users and may confuse them. The final Zhanlu product should hide raw skill bindings by default and expose friendly capabilities instead.

This document should be followed by Claude Code or any coding agent when connecting the existing UI to the backend.

---

## Core Decision

Replace the normal-user `Skills` section with:

```text
Core Capabilities
Choose what this agent can do.
```

Examples of user-facing capabilities:

```text
Search / Browse
Database Analysis
File Analysis
Make PPT
Make DOCX
Make Markdown Report
Make HTML / Webpage
Make Dashboard
Make Mini App
Generate Charts
Run Scheduled Reports
Auto-Update Dashboard
Use Sandbox Execution
Validate Artifacts
```

The backend maps these user-friendly capabilities to real executable skills.

---

## Why Raw Skills Should Not Be Shown to Normal Users

Raw skill names are implementation details. They may include low-level runtime tools, framework-specific names, or internal developer utilities.

Examples that should not be shown as primary user choices:

```text
vercel-cli
agent-browser
json-render-remotion
streamdown-secure
pptx-generation
governed-nl2sql
artifact-validation
pdf-preview
```

These are useful internally, but they are not meaningful for most users who are creating a business agent.

Normal users think in terms of tasks:

```text
Can this agent analyze my database?
Can it make a PPT?
Can it create a dashboard?
Can it update reports every week?
Can it browse/search?
```

Therefore, the UI should ask users to select capabilities, and Zhanlu should translate capabilities into skills, tools, MCP permissions, sandbox permissions, artifact permissions, and subagent bindings.

---

## Final UI Structure for Agent Creation

For normal users, the agent creation form should include:

```text
1. Role Definition
   - Agent name
   - Description
   - Boundary / responsibility

2. Prompt / Behavior
   - Identity
   - Capability boundary
   - Reasoning framework
   - Tool operation rules
   - Output constraints

3. Core Capabilities
   - Friendly capability tags

4. Data Access
   - Select database / knowledge base from My Space
   - Set read-only access rules
   - Choose whether subagents can use DataSnapshots

5. Delegated Subagents
   - Add existing subagents
   - Create new subagent
   - Set delegation mode

6. Advanced Settings
   - Raw skills
   - MCP tools
   - policy profile
   - sandbox runtime
   - model route
```

The old visible `Skills` section should become an advanced section:

```text
Advanced Skill Bindings
View and edit internal skills used by this agent.
```

By default, this advanced section should be collapsed or hidden unless the user is an admin/developer.

---

## Main Agent, Subagents, Capabilities, and Skills

Use this rule:

```text
Main Agent = user-facing coordinator
Subagents = specialist workers under the main agent
Capabilities = business-friendly abilities selected in UI
Skills = executable backend capability packages
MCP = external connector protocol
Datasource = governed database/KB access
Artifact = generated output
```

Example:

```text
Main Agent: Quality Control Agent

Core Capabilities selected by user:
  - Quality Analysis
  - SPC Charts
  - Data Analysis
  - Make PPT

Delegated Subagents:
  - Data Analyst Subagent
  - SPC Chart Subagent
  - PPT Builder Subagent
  - Reviewer Subagent

Backend skill bindings:
  Data Analyst Subagent:
    - governed-nl2sql
    - data-snapshot

  SPC Chart Subagent:
    - chart-generation
    - python-analysis

  PPT Builder Subagent:
    - pptx-generation
    - pdf-preview
    - artifact-validation

  Reviewer Subagent:
    - artifact-validation
    - source-reference-check
```

The user does not need to see every low-level skill unless they open advanced settings.

---

## Capability-to-Skill Mapping

Claude Code should implement a backend mapping table or registry.

Example mapping:

```json
{
  "make_ppt": {
    "label": "Make PPT",
    "required_skills": [
      "pptx-generation",
      "pdf-preview",
      "artifact-validation"
    ],
    "requires_sandbox": true,
    "artifact_types": ["pptx", "pdf"],
    "suggested_subagent_role": "ppt_builder"
  },
  "database_analysis": {
    "label": "Database Analysis",
    "required_skills": [
      "governed-nl2sql",
      "data-snapshot"
    ],
    "requires_datasource": true,
    "requires_sandbox": false,
    "suggested_subagent_role": "data_analyst"
  },
  "make_dashboard": {
    "label": "Make Dashboard",
    "required_skills": [
      "dashboard-generation",
      "chart-generation",
      "data-snapshot",
      "artifact-validation"
    ],
    "requires_datasource_optional": true,
    "artifact_types": ["dashboard"],
    "suggested_subagent_role": "dashboard_builder"
  },
  "make_docx": {
    "label": "Make DOCX",
    "required_skills": [
      "docx-generation",
      "pdf-preview",
      "artifact-validation"
    ],
    "requires_sandbox": true,
    "artifact_types": ["docx", "pdf"],
    "suggested_subagent_role": "document_builder"
  },
  "make_html": {
    "label": "Make HTML / Webpage",
    "required_skills": [
      "html-generation",
      "html-preview",
      "security-validation"
    ],
    "requires_sandbox": true,
    "artifact_types": ["html"],
    "suggested_subagent_role": "webpage_builder"
  },
  "scheduled_reports": {
    "label": "Run Scheduled Reports",
    "required_skills": [
      "automation-schedule",
      "data-snapshot",
      "notification"
    ],
    "requires_automation_permission": true,
    "suggested_subagent_role": "automation_runner"
  }
}
```

---

## Agent Creation Behavior

When a user selects capabilities, the backend should automatically propose:

```text
1. Required skills
2. Suggested subagents
3. Required permissions
4. Required sandbox access
5. Required artifact types
6. Required datasource access, if applicable
7. Approval rules, if necessary
```

Example:

User creates:

```text
Quality Control Agent
```

User selects:

```text
Quality Analysis
SPC Charts
Data Analysis
Make PPT
```

Zhanlu should propose:

```text
Suggested subagents:
  - Data Analyst Subagent
  - SPC Chart Subagent
  - PPT Builder Subagent
  - Reviewer Subagent

Required skills:
  - governed-nl2sql
  - data-snapshot
  - chart-generation
  - pptx-generation
  - pdf-preview
  - artifact-validation

Required data:
  - choose one datasource from My Space / Databases & KB
```

The user can accept defaults or edit advanced settings.

---

## Slash `/` Skill Picker in Chat

The slash picker should show friendly allowed actions, not internal skill names.

Good:

```text
/analyze-data
/spc-chart
/make-ppt
/make-dashboard
/make-report
/create-html
/summarize-file
/schedule-report
```

Bad:

```text
/pptx-generation
/governed-nl2sql
/json-render-remotion
/vercel-cli
```

Slash commands should be filtered by the current main agent's capabilities and permissions.

Example:

```text
Current main agent: Quality Control Agent
Allowed slash actions:
  /analyze-defects
  /spc-chart
  /make-ppt
  /create-dashboard
  /make-report

Blocked/hidden:
  /query-payroll
  /send-external-email
  /finance-budget-report
```

---

## Database Access and Capabilities

Capabilities should not automatically grant database access.

If a user selects `Database Analysis`, the UI must still ask which datasource this agent can use.

Example:

```text
User has three datasources:
  - Finance Database
  - Customer Business Database
  - Quality Control Database

For Quality Control Agent, user selects only:
  ✓ Quality Control Database

Then the agent and its subagents cannot see or query:
  ✗ Finance Database
  ✗ Customer Business Database
```

A capability can require a datasource, but the user must explicitly bind the datasource.

---

## Backend Data Model Additions

Add a capability registry and agent capability bindings.

Suggested tables:

```sql
CREATE TABLE capability_profiles (
    id UUID PRIMARY KEY,
    capability_key TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    description TEXT,
    category TEXT,
    required_skills JSONB NOT NULL DEFAULT '[]',
    required_permissions JSONB NOT NULL DEFAULT '{}',
    suggested_subagent_role TEXT,
    requires_datasource BOOLEAN NOT NULL DEFAULT false,
    requires_sandbox BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_capability_bindings (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    agent_id UUID NOT NULL,
    capability_key TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Existing skill binding tables remain:

```text
agent_skill_bindings
skill_profiles
skill_versions
skill_package_versions
```

But normal user interaction should use:

```text
agent_capability_bindings
```

The backend then expands capabilities into:

```text
agent_skill_bindings
subagent_skill_bindings
artifact_permissions
sandbox_permissions
automation_permissions
slash_action_permissions
```

---

## API Requirements

Claude Code should implement or support APIs like:

```text
GET  /api/v1/capabilities
GET  /api/v1/capabilities?category=artifact
POST /api/v1/agents/{agent_id}/capabilities
GET  /api/v1/agents/{agent_id}/capabilities
POST /api/v1/agents/{agent_id}/capabilities/resolve
GET  /api/v1/agents/{agent_id}/slash-actions
```

Capability resolution endpoint should return:

```json
{
  "agent_id": "agent_uuid",
  "selected_capabilities": ["database_analysis", "make_ppt"],
  "required_skills": ["governed-nl2sql", "data-snapshot", "pptx-generation", "pdf-preview", "artifact-validation"],
  "suggested_subagents": ["Data Analyst Subagent", "PPT Builder Subagent", "Reviewer Subagent"],
  "requires_datasource": true,
  "requires_sandbox": true,
  "warnings": [
    "Database Analysis requires selecting a datasource."
  ]
}
```

---

## Advanced Mode

Admins/developers can still inspect and edit raw skill bindings.

Advanced UI should show:

```text
Advanced Skill Bindings
  - governed-nl2sql
  - data-snapshot
  - pptx-generation
  - pdf-preview
  - artifact-validation

Advanced Runtime
  - sandbox image
  - skill version
  - MCP tool bindings
  - policy profile
```

But default users should not need this.

---

## Acceptance Tests

Claude Code should satisfy these tests:

1. Normal user creating an agent sees `Core Capabilities`, not raw internal skills.
2. User can select `Make PPT`; backend binds required PPT skills automatically.
3. User can select `Database Analysis`; UI asks user to select a datasource.
4. If user has three datasources and selects one, the agent cannot use the other two.
5. Slash `/` picker shows friendly actions only.
6. Slash `/` picker does not show internal skill IDs by default.
7. Admin/developer can open advanced view and inspect raw skill bindings.
8. Subagents receive skill access based on main agent capabilities and delegated rules.
9. Agent without selected capability cannot run the related skill.
10. Agent without datasource binding cannot run database analysis.

---

## Final Rule

Zhanlu should expose **Capabilities** to normal users and reserve raw **Skills** for advanced users and backend execution. Users creating a main agent choose what the agent can do, such as database analysis, PPT generation, dashboard creation, report writing, HTML generation, and scheduled updates. Zhanlu maps these capabilities to real skills, subagents, sandbox permissions, artifact types, and slash actions. This keeps the UI simple while preserving the full power of the underlying skill runtime.
