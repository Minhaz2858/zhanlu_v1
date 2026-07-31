# Zhanlu Slash Skill Invocation UI and Custom Skill Runtime Spec

**File purpose:** This file defines how Zhanlu should allow users to select and run skills directly from the chat UI using `/`, similar to a command palette. It also defines how user-created custom skills appear in the UI, how selected skills receive user input, how the backend validates and runs the skill, and how the output appears as an inline artifact or chat response.

**Status:** Final implementation handoff spec for Claude Code.

---

## 1. Core idea

Zhanlu must support direct skill invocation from chat.

When the user types `/`, the chat input opens a **Skill Command Palette**. The user can select a system skill or a custom skill, provide input, attach files or data, and run that skill on the current message.

The selected skill should work on the user input and produce an output, such as:

```text
/pptx Create a 10-slide sales presentation from this data.
/docx Turn this outline into a formal report.
/md Create an architecture note from this conversation.
/html Build a landing page.
/dashboard Create a sales dashboard from my database.
/company-finance-ppt Make a Q2 finance report using the company template.
/my-custom-skill Run this on the attached file.
```

Important principle:

```text
User-selected skill is an explicit instruction, but it is not a permission bypass.
```

Synexia and Layer 5 must respect the selected skill when allowed, but every selected skill still passes permission, policy, sandbox, input schema, datasource, and artifact validation checks.

---

## 2. Product behavior

The user should be able to do three things from the chat input:

```text
1. Type normally and let Synexia choose the best agent and skills.
2. Type `/` and manually choose a skill.
3. Type `/skill-name` directly and pass input to that skill.
```

Examples:

```text
User: Make a PPT for me.
System behavior: Synexia selects PPT-related skills automatically.
```

```text
User: /pptx Make a 10-slide investor pitch deck.
System behavior: User explicitly selected the PPTX skill. Zhanlu should run that skill if allowed.
```

```text
User: /company-finance-ppt Use Q2 revenue data and generate the board report.
System behavior: Zhanlu runs the custom company finance PPT skill if the user, app, agent, and datasource permissions allow it.
```

---

## 3. UI feature name

Recommended product names:

```text
Skill Command Palette
Slash Skill Launcher
Run Skill from Chat
```

In the codebase, use:

```text
SlashSkillLauncher
SkillCommandPalette
SelectedSkillChip
SkillInvocationPanel
```

---

## 4. Chat input behavior

### 4.1 Trigger

When the user types `/` at the start of the message or after whitespace, show the skill picker.

Examples that should open the picker:

```text
/
/p
/ppt
Please use /pptx
```

### 4.2 Skill search

The picker should search by:

```text
skill display name
skill slug
artifact type
description
tags
capabilities
recent usage
custom/system scope
```

Example search results:

```text
PPTX Generation
Create editable PowerPoint presentations
Tags: pptx, slides, presentation, report
Scope: System

Company Finance PPT
Create finance decks using the company template
Tags: finance, pptx, board report
Scope: App Custom

Dashboard Generation
Create interactive dashboards from data snapshots
Tags: dashboard, chart, data
Scope: System
```

### 4.3 Skill result card

Each skill result should show:

```text
icon
name
short description
scope badge: System / Org / App / Private
artifact type badge: PPTX / DOCX / MD / HTML / Dashboard / Mini App
requires sandbox badge
requires datasource badge
approval required badge if relevant
```

Example UI card:

```text
[PPTX] Company Finance PPT
Create finance presentations using the approved company template.
Scope: App Custom · Sandbox · DataSnapshot required
```

### 4.4 Selecting a skill

When user selects a skill, insert a skill chip into the composer:

```text
[/ Company Finance PPT] Generate Q2 board report using latest finance data.
```

The chip should be removable.

Actions:

```text
click skill chip → open skill details / inputs
x on chip → remove selected skill
enter → send message with selected skill invocation
```

### 4.5 Input schema form

If the skill has required structured fields, show an auto-generated form from the skill input schema.

Example for PPTX skill:

```text
Title: [Q2 Finance Report]
Audience: [Board / Sales / Internal]
Slide count: [10]
Template: [Company Finance Template]
Language: [English]
Data source: [finance_postgres / DataSnapshot / uploaded file]
```

The user can still provide natural language input. The form is for required inputs only.

---

## 5. Skill invocation modes

Zhanlu should support three skill invocation modes.

### Mode A: Auto-selected skill

The user writes a normal request. Synexia chooses skills.

```text
User: Make a PPT from this report.
Synexia selects: pptx-generation, pdf-preview, artifact-validation.
```

### Mode B: Explicit direct skill

The user chooses a skill with `/`.

```text
User: /pptx Make a sales pitch deck.
```

Zhanlu should prioritize the selected skill.

### Mode C: Agent-assisted selected skill

The user chooses a skill while using a specific agent.

```text
Selected agent: Finance Agent
User: /company-finance-ppt Generate Q2 board deck.
```

The selected agent gives task context, datasource permissions, memory scope, and business rules. The selected skill performs the executable work.

---

## 6. Backend contract for selected skill

When the user sends a chat message with a selected skill, the frontend must include `explicit_skill_invocation` in the chat request.

Example request:

```json
{
  "conversation_id": "conv_123",
  "app_id": "app_123",
  "message": "Generate Q2 board report using latest finance data.",
  "selected_agent_id": "finance_agent_id",
  "explicit_skill_invocation": {
    "skill_id": "company_finance_ppt",
    "skill_version_id": "skill_version_12",
    "invocation_mode": "agent_assisted",
    "params": {
      "title": "Q2 Finance Board Report",
      "slide_count": 10,
      "language": "English",
      "template_id": "company_finance_template_v3"
    },
    "attachment_ids": [],
    "selected_artifact_ids": [],
    "selected_dataset_ids": ["finance_postgres_binding"]
  }
}
```

Layer 1 should seal this into the RequestEnvelope:

```json
{
  "request_envelope": {
    "id": "env_123",
    "org_id": "org_123",
    "app_id": "app_123",
    "user_id": "user_123",
    "conversation_id": "conv_123",
    "payload": "Generate Q2 board report using latest finance data.",
    "explicit_skill_invocation": {
      "skill_id": "company_finance_ppt",
      "skill_version_id": "skill_version_12",
      "invocation_mode": "agent_assisted",
      "params": {},
      "attachments": [],
      "selected_datasets": []
    }
  }
}
```

---

## 7. Synexia behavior when user selects a skill

Synexia must follow this logic:

```text
If explicit_skill_invocation exists:
  1. Treat selected skill as user intent.
  2. Check whether the selected skill is available in this org/app/user context.
  3. Check whether selected agent is allowed to use this skill.
  4. If no agent is selected, use the default agent or Direct Skill Runner profile.
  5. Create a PlanDAG that includes the selected skill.
  6. Add helper skills only when necessary and allowed.
  7. Never replace the selected skill silently.
  8. If blocked, explain why and suggest allowed alternatives.
```

Example blocked response:

```text
I cannot run Company Finance PPT because this skill requires the Finance Agent and access to finance_postgres. You can switch to Finance Agent or choose the general PPTX skill.
```

---

## 8. Direct Skill Runner

If the user selects a skill but no agent is selected, Zhanlu should use a default controlled profile:

```text
Direct Skill Runner
```

This is not a free agent. It is a minimal harness profile that can only run the selected skill and helper skills needed for preview/validation.

Allowed:

```text
run selected skill
run preview skill
run validation skill
store artifact
show inline preview
```

Not allowed by default:

```text
query enterprise database
send external messages
write memory to app/org scope
publish artifact
use unbound MCP tools
```

If the skill needs enterprise data, Zhanlu should require an agent or explicit datasource binding.

---

## 9. Custom skill visibility in UI

Custom skills should appear in the `/` picker only if all conditions pass:

```text
skill status is approved or published
skill is in user's org/app/private scope
user has permission to use it
selected agent, if any, is allowed to use it
skill is not deprecated or archived
required runtime is available
policy does not block it
```

Skill scopes:

```text
system
org_shared
app_shared
user_private
imported_candidate
```

Only these should be shown by default:

```text
system
org_shared allowed to user
app_shared allowed to user
user_private owned by user
```

Do not show:

```text
skill candidates waiting for review
blocked skills
archived skills
deprecated skills unless user enables "show deprecated"
skills requiring unavailable runtime
```

---

## 10. Custom skill lifecycle

User-created custom skills follow this lifecycle:

```text
draft
candidate
testing
review_required
approved
published
deprecated
archived
blocked
```

A custom skill can be invoked from chat only when:

```text
status = approved or published
```

Exception: the owner may test a draft skill in a private sandbox test mode if policy allows.

---

## 11. Skill preflight before execution

Before running any selected skill, Layer 5 must run preflight.

Preflight checks:

```text
Is the skill found?
Is the skill version active?
Is the user allowed to run it?
Is the selected agent allowed to use it?
Does it require approval?
Does it require sandbox?
Does it require datasource access?
Are required inputs present?
Are selected attachments allowed?
Are selected artifacts allowed?
Is runtime image available?
Is cost within budget?
Is network access required?
Is network access allowed?
Is this skill allowed to produce this artifact type?
```

Preflight result:

```json
{
  "status": "ready",
  "skill_id": "company_finance_ppt",
  "requires_approval": false,
  "requires_sandbox": true,
  "missing_inputs": [],
  "warnings": [],
  "estimated_cost": {
    "tokens": 12000,
    "sandbox_seconds": 90
  }
}
```

Blocked result:

```json
{
  "status": "blocked",
  "reason": "Selected agent is not allowed to use this skill.",
  "suggested_actions": [
    "Switch to Finance Agent",
    "Choose the general PPTX Generation skill"
  ]
}
```

---

## 12. Skill execution flow

Full flow when user selects `/company-finance-ppt`:

```text
1. User types `/company-finance-ppt Generate Q2 board report`.
2. Frontend sends explicit_skill_invocation to backend.
3. Backend stores user message.
4. Layer 1 creates RequestEnvelope.
5. Synexia creates TaskSpec and PlanDAG using selected skill.
6. Layer 5 runs skill preflight.
7. Datasource Gateway creates DataSnapshot if the skill needs data.
8. Sandbox-worker materializes skill package into temporary folder.
9. Skill runs in temporary Docker sandbox.
10. Skill output is validated.
11. Artifact is stored in PostgreSQL.
12. Event stream sends sandbox progress to chat.
13. Artifact Preview Card appears inline.
14. User can edit, regenerate, approve, download, publish, or schedule update.
```

---

## 13. Event stream for selected skill

Events should clearly show that a user-selected skill is running.

Example events:

```json
{
  "event_type": "skill.selected",
  "execution_id": "exec_123",
  "skill_id": "company_finance_ppt",
  "skill_name": "Company Finance PPT",
  "selected_by": "user",
  "visibility": "public"
}
```

```json
{
  "event_type": "skill.preflight_completed",
  "execution_id": "exec_123",
  "skill_id": "company_finance_ppt",
  "status": "ready",
  "visibility": "business"
}
```

```json
{
  "event_type": "skill.run_started",
  "execution_id": "exec_123",
  "skill_run_id": "run_456",
  "skill_id": "company_finance_ppt",
  "visibility": "business"
}
```

```json
{
  "event_type": "sandbox.command_stdout",
  "execution_id": "exec_123",
  "skill_run_id": "run_456",
  "text": "WROTE Q2_Finance_Report.pptx",
  "visibility": "developer"
}
```

```json
{
  "event_type": "artifact.preview_ready",
  "execution_id": "exec_123",
  "artifact_id": "artifact_789",
  "artifact_type": "pptx",
  "visibility": "public"
}
```

---

## 14. Chat UI display example

User input:

```text
/company-finance-ppt Generate Q2 board report using latest finance data.
```

Assistant response UI:

```text
I will run the Company Finance PPT skill using your approved finance data and company template.

[Live Execution Timeline] Hide
✓ Skill selected: Company Finance PPT
✓ Checking permissions
✓ Creating finance DataSnapshot
✓ Running skill in sandbox
  Command: python build_ppt.py
  Output: WROTE Q2_Finance_Report.pptx
✓ Generating PDF preview
✓ Validating artifact

[Artifact Card]
Q2 Finance Report.pptx
Preview · Edit · Regenerate · Download · Approve
```

If the user hides the timeline, only show:

```text
Creating Q2 Finance Report.pptx...

[Artifact Card]
Q2 Finance Report.pptx
Preview ready
```

---

## 15. Skill picker API

Required APIs:

```http
GET /api/v1/skills
GET /api/v1/skills/search?q=ppt&app_id=...
GET /api/v1/skills/{skill_id}
GET /api/v1/skills/{skill_id}/versions
POST /api/v1/skills/{skill_id}/preflight
POST /api/v1/skills/{skill_id}/run
GET /api/v1/skills/recent
GET /api/v1/skills/pinned
POST /api/v1/skills/{skill_id}/pin
DELETE /api/v1/skills/{skill_id}/pin
```

Chat API should also accept explicit skill invocation:

```http
POST /api/v1/chat/stream
```

With body field:

```json
{
  "explicit_skill_invocation": {
    "skill_id": "pptx_generation",
    "skill_version_id": "...",
    "invocation_mode": "direct",
    "params": {},
    "attachment_ids": [],
    "selected_artifact_ids": [],
    "selected_dataset_ids": []
  }
}
```

---

## 16. Database additions

Add or confirm these tables.

### skill_invocations

```sql
CREATE TABLE skill_invocations (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    user_id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    message_id UUID NOT NULL,
    agent_id UUID,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    invocation_mode TEXT NOT NULL,
    -- auto_selected | direct | agent_assisted | test_mode
    selected_by TEXT NOT NULL,
    -- user | synexia | automation
    input_text TEXT,
    params JSONB NOT NULL DEFAULT '{}',
    preflight_status TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

### user_skill_preferences

```sql
CREATE TABLE user_skill_preferences (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    user_id UUID NOT NULL,
    skill_id UUID NOT NULL,
    is_pinned BOOLEAN NOT NULL DEFAULT false,
    last_used_at TIMESTAMPTZ,
    usage_count INT NOT NULL DEFAULT 0,
    default_params JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### skill_access_grants

```sql
CREATE TABLE skill_access_grants (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID,
    skill_id UUID NOT NULL,
    subject_type TEXT NOT NULL,
    -- user | group | role | agent | app
    subject_id UUID NOT NULL,
    permission TEXT NOT NULL,
    -- view | run | test | edit | publish | admin
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 17. Frontend components

Add these components:

```text
frontend/src/features/chat/components/SkillCommandPalette.tsx
frontend/src/features/chat/components/SelectedSkillChip.tsx
frontend/src/features/chat/components/SkillInvocationPanel.tsx
frontend/src/features/chat/components/SkillPreflightResult.tsx
frontend/src/features/chat/hooks/useSkillSearch.ts
frontend/src/features/chat/hooks/useSelectedSkill.ts
```

Suggested component logic:

```tsx
<ChatComposer>
  <SelectedSkillChip skill={selectedSkill} onRemove={clearSkill} />
  <textarea onChange={handleSlashDetection} />
  {showSkillPalette && (
    <SkillCommandPalette
      query={slashQuery}
      appId={currentAppId}
      agentId={selectedAgentId}
      onSelect={setSelectedSkill}
    />
  )}
</ChatComposer>
```

On send:

```tsx
sendChatMessage({
  conversation_id,
  message,
  selected_agent_id,
  explicit_skill_invocation: selectedSkill
    ? {
        skill_id: selectedSkill.id,
        skill_version_id: selectedSkill.version_id,
        invocation_mode: selectedAgentId ? 'agent_assisted' : 'direct',
        params: skillParams,
        attachment_ids: selectedAttachmentIds,
        selected_artifact_ids: selectedArtifactIds,
        selected_dataset_ids: selectedDatasetIds,
      }
    : null,
});
```

---

## 18. Skill capability metadata

Every skill shown in the palette must expose metadata:

```json
{
  "skill_id": "pptx_generation",
  "slug": "pptx",
  "name": "PPTX Generation",
  "description": "Create editable PowerPoint presentations.",
  "scope": "system",
  "artifact_types": ["pptx"],
  "tags": ["slides", "presentation", "deck", "report"],
  "requires_sandbox": true,
  "requires_datasource": false,
  "requires_approval": false,
  "input_schema": {},
  "status": "published"
}
```

Custom skill example:

```json
{
  "skill_id": "company_finance_ppt",
  "slug": "company-finance-ppt",
  "name": "Company Finance PPT",
  "description": "Create finance board-report decks using the company template.",
  "scope": "app_shared",
  "artifact_types": ["pptx"],
  "tags": ["finance", "pptx", "board report"],
  "requires_sandbox": true,
  "requires_datasource": true,
  "requires_approval": false,
  "status": "published"
}
```

---

## 19. Security and governance rules

Mandatory rules:

```text
1. `/` skill selection cannot bypass permissions.
2. Custom skills must be approved or explicitly in private test mode.
3. Skills run through Layer 5, not directly from frontend.
4. Skills run through Tool/Skill Gateway.
5. Code skills and artifact skills run in sandbox.
6. Skills receive approved handles and input packages, not credentials.
7. Skills do not get raw database passwords.
8. Database-connected skills use DataSnapshots by default.
9. Every skill invocation is logged.
10. Every skill output is validated before artifact preview.
11. Raw command logs are developer/admin visibility only.
12. Private prompts, secrets, environment variables, and full internal paths are never shown in chat.
```

---

## 20. Error handling

### Skill not found

```text
I could not find a skill named `/financeppt`. Try `/company-finance-ppt` or `/pptx`.
```

### Skill blocked

```text
This skill is not available in this app. Ask an admin to grant access or choose another skill.
```

### Missing input

```text
This skill needs a template and slide count before it can run.
```

### Agent binding missing

```text
The selected agent cannot use this skill. Switch to Finance Agent or choose a general skill.
```

### Datasource binding missing

```text
This skill needs finance data, but no approved datasource is connected to this agent.
```

### Sandbox failed

```text
The skill failed while building the artifact. I saved the logs and can retry or repair the build.
```

---

## 21. Acceptance tests

Claude Code must implement tests for these cases:

```text
1. Typing `/` opens the Skill Command Palette.
2. Searching `ppt` returns PPT-related system and custom skills.
3. Selecting a skill inserts a SelectedSkillChip into the composer.
4. Sending a message with a selected skill includes explicit_skill_invocation in the request.
5. Backend stores skill_invocation row.
6. Synexia creates a PlanDAG containing the selected skill.
7. Skill preflight blocks unauthorized skill use.
8. A permitted skill runs through Layer 5 and sandbox-worker.
9. Skill output creates artifact records.
10. Artifact preview appears inline in chat.
11. User can hide the execution timeline without losing audit events.
12. Custom skill appears only when approved/published and user has access.
13. Unapproved custom skill does not appear in the picker.
14. Skill requiring datasource is blocked if agent has no datasource binding.
15. Developer logs are not shown to normal users.
```

---

## 22. MVP implementation order

Build in this order:

```text
Phase 1:
Add skill search API and frontend slash picker.

Phase 2:
Add SelectedSkillChip and explicit_skill_invocation payload.

Phase 3:
Store skill_invocations in PostgreSQL.

Phase 4:
Make Synexia respect selected skill in PlanDAG.

Phase 5:
Run selected skill through existing Skill Runtime and sandbox-worker.

Phase 6:
Connect skill output to Artifact Preview Card.

Phase 7:
Add custom skill visibility, pinned skills, and recent skills.

Phase 8:
Add input schema form and preflight UI.
```

---

## 23. Final design rule

**Zhanlu must let users invoke skills directly from chat using `/`. The slash skill picker shows only skills the user is allowed to view and run, including system skills and approved custom skills. When a user selects a skill, the chat request carries an explicit skill invocation that Synexia must honor unless policy blocks it. The selected skill runs through Layer 5, Tool/Skill Gateway, sandbox-worker, validation, and artifact storage. Skill selection is a user experience feature, not a security bypass. User-created custom skills are stored in PostgreSQL, appear in the picker only when approved and accessible, run with approved input packages, and produce versioned PostgreSQL-backed artifacts with inline chat previews.**
