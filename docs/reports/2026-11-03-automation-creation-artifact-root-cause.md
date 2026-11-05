# Root Cause Report — Automation Creation / Execution / Artifact Lifecycle Bug

**Date:** 2026-11-03 · **Status:** investigation complete, corrections implemented (see §5)
**Scope:** frontend (`/automation/new` → chat), chat orchestration, agent tools, Control Plane, execution runtime, artifact pipeline
**Method:** systematic debugging — read-only lifecycle trace first (two parallel deep investigations, frontend + backend), every claim verified against the current working tree with file:line evidence.

---

## 1. Observed behavior (reproduction)

1. `/automation/new` → "Daily Sales Data Sync", Data Sync, Daily 08:00, output format **HTML report** → **Save & Continue in Chat**.
2. A "Pending Automation" card appears in chat; the message is auto-sent.
3. The agent replies "Automation Task Created Successfully" with task ID, **active** status, and a **datasource name** (`wechat_records_5000_20260930`) the user never configured.
4. An unrelated **HTML artifact** appears (executive summary / methodology / SQL execution info / zero-row warning), titled like the creation message.

## 2. The actual lifecycle (traced)

```
/automation/new form
  └─ NewAutomationTask.jsx:59-75  handleSubmit — NO API call; builds a
     plain-text prefill (CreateResourceDialog.jsx:129-155) embedding
     "Output format：HTML report (html)" and navigates to
     /?prefill=…&newTask=1&agentName=automation_agent
  └─ Chat.jsx:442-505  prefill effect — shows the local-only Pending card
     (AutomationDraftCard.jsx, pure local state, NO server draft, NO confirm
     API) and AUTO-SENDS the prefill (Chat.jsx:481-485)
  └─ POST /apps/{app}/agents/conversations/v3/{id}/messages/stream
     └─ agents.py:12221 intent rebind → automation_agent
        (header regex _AUTOMATION_SETUP_HEADER_RE, agents.py:3045-3058)
     └─ toolset = create_automation, update_automation, execute_automation,
        list_knowledge_bases, …   (system_agents.py:297-322,
        tool_registry.py:448-451 — propose_automation ABSENT)
     └─ LLM calls list_knowledge_bases → project KB list
     └─ LLM calls create_automation (legacy CRUD tool)
        └─ agent_tools.py:1730-2130 → direct AutomationTask INSERT,
           status="active" when cron parses (1764-1778), data_source_id
           = the single bound KB (schema text INSTRUCTS auto-pick:
           agent_prompts.py:2980, 2997-3000, 3040) — no draft, no confirm
     └─ file_turn_guard.should_force_create_file (file_turn_guard.py:304-339,
        invoked agents.py:7868/15317) sees "html" in the user message →
        forces/nudges create_artifact(type='html') — the definition's
        output_format leaking into the creation turn
     └─ (or) execute_automation in the same toolset can run the just-created
        task (automation_chat_tool.py:128-215 → trigger_now) → real NL2SQL →
        "SQL execution info / zero-row warning" content
     └─ finalize_into_artifact (agents.py:8516-8536) can auto-persist the
        HTML Artifact row from report synthesis
  └─ SSE artifact_created / done → Chat.jsx renders the artifact
  └─ Chat.jsx:2760-2770 sees tool name create_automation → dismisses the
     "pending" card + fires zhanlu:automation-created  → task shows Active
```

## 3. Root causes

| # | Root cause | Evidence (file:line) | Violated contract |
|---|---|---|---|
| RC1 | **Legacy `create_automation` tool bypasses the Control Plane.** It inserts an `AutomationTask` directly with `status="active"` when a cron parses — no draft, no confirmation, schedule armed at creation. The R3 `propose_automation`/`confirm_draft` gate exists but is **dead in chat**: the tool is registered (`automation_chat_tool.py:374-383`) yet appears in *no* agent toolset, and `AUTOMATION_AGENT_SYSTEM_PROMPT` (:1564) still instructs the agent to call `create_automation`. | `agent_tools.py:1730-2130` (status active at 1764-1778); toolsets at `agent_prompts.py:2975-3047`, `system_agents.py:297-322`, `tool_registry.py:448-451`; prompt at `agent_prompts.py:1564,1647` | TS §4.2 / §4.1 — confirmation is the only definition-creation gate; "the old `create_automation` tool is removed from the registry after the cutover flag" |
| RC2 | **Implicit datasource selection.** The `create_automation`/`list_knowledge_bases` tool schemas literally instruct the LLM: "if the project has exactly one bound knowledge base, use it directly without asking". Combined with RC1, the never-configured `wechat_records_5000_20260930` is written onto the task. (Execution-time union with authorized project KBs is legitimate capability scoping; creation-time silent selection is not.) | `agent_prompts.py:2980, 2997-3000, 3039-3045`; persistence `agent_tools.py:2048` | Mission invariant — "Never silently select the first datasource"; TS §5 harness enforces authorization, the agent decides |
| RC3 | **`output_format=html` leaks into the chat turn as a file-deliverable request.** `should_force_create_file` matches "html" among `_FILE_FORMATS` in the user message and forces/nudges `create_artifact(type='html')`; the existing automation-scheduling exemption uses loose string matching on the tool args blob and does not cover the setup turn before the tool call. `finalize_into_artifact` additionally auto-persists an HTML Artifact from report synthesis. | `file_turn_guard.py:63, 224-231, 257-258, 304-339, 464-558`; call sites `agents.py:7868, 15317`; `agents.py:8516-8536, 4143-4182` | Mission §3D — definition output_format controls EXECUTION deliverables, never the chat artifact renderer |
| RC4 | **`execute_automation` in the automation_agent toolset** lets the agent run the just-created task inside the creation turn (real NL2SQL, zero-row content in the creation reply). | `system_agents.py:300`, `tool_registry.py:449`; handler `automation_chat_tool.py:128-215` | Mission §3A — configuration phase must not execute the workflow |
| RC5 | **Frontend "Pending" card is non-authoritative.** `Save & Continue in Chat` makes NO API call (config travels as prefill text); the card is local state with no server draft id, no validation status, and **no Confirm/Discard**; it self-destructs when the backend reports `create_automation`. "Active" is just the backend row status. | `NewAutomationTask.jsx:59-75`; `Chat.jsx:442-505, 2760-2770`; `AutomationDraftCard.jsx` (local state only) | Mission §3B/§6 — explicit `confirm_automation(confirmation_key, If-Match)`; Active only after confirmation |
| RC6 | `update_automation` (legacy) accepts `status` and `data_source_id` directly — can activate a task or rebind its datasource with no gate. | `agent_tools.py:2131-2245` (fields at 2161-2166) | Same as RC1 |

**Non-causes verified:** the frontend sends exactly ONE creation request (no duplicate-call path in the agent flow; the legacy `base44.entities.AutomationTask.create` fallback requires agent resolution failure); `create_automation` does NOT itself query ERP data or trigger a run — the SQL content comes from RC4 (same-turn `execute_automation`) and/or a rebind-miss turn on `general_assistant` (data-capable toolset) plus RC3's forced artifact. The Control Plane itself is sound: `confirm_draft` is exactly-once, `If-Match`-checked, audited, and creates the definition `paused` with no execution side effects (`control_plane.py:191-222`) — it was simply never reachable from the chat path.

## 4. Corrections (implemented)

| Root cause | Correction | Flag |
|---|---|---|
| RC1 | `AUTOMATION_CREATION_V2` cutover (default ON): `propose_automation` + new `update_automation_draft` replace `create_automation` in every automation_agent toolset + prompt; legacy `create_automation`/`update_automation` handlers refuse (deterministic, not prompt-level) when the flag is on | `AUTOMATION_CREATION_V2` (env-overridable, default True) |
| RC2 | Tool-schema text rewritten: never auto-pick a datasource; a missing datasource becomes a draft validation warning + a clarify question | same |
| RC3 | `should_force_create_file` / nudge: automation-configuration turns (rebind to automation_agent) are exempt from format-keyword forcing; definition output_format never reaches the turn guard | — |
| RC4 | `execute_automation` removed from the automation_agent toolset (Run-Now remains a user action via the Center/API; `general_assistant` keeps it for the Run-Now handoff) | same |
| RC5 | `Save & Continue in Chat` creates a **server draft** (`POST /api/automations/drafts`) before navigating; the Pending card renders server draft id + validation; explicit **Confirm & Activate** (`POST …/confirm?activate=true`) and **Discard** buttons; card no longer auto-dismisses on tool detection | — |
| RC6 | `update_automation` under V2: `status` activation refuses; `update_automation_draft` (draft-only) is the agent's editing path | same |
| confirm | `POST /automations/drafts/{id}/confirm` accepts `activate=true` **inside the confirmation transaction** (definition born active) so the frontend's Confirm & Activate is one authoritative action | — |

## 5. Root-cause → test acceptance matrix

| Root cause | Regression tests |
|---|---|
| RC1 | `test_automation_creation_lifecycle.py::test_create_automation_refuses_under_v2` (no row created), `::test_automation_agent_toolset_has_propose_not_create`, `::test_update_automation_draft_writes_draft_only` |
| RC2 | `::test_propose_automation_without_datasource_flags_warning_not_silent_pick`, schema-text assertions in `::test_automation_agent_toolset_has_propose_not_create` |
| RC3 | `test_file_turn_guard_automation_setup.py::test_automation_setup_turn_not_forced_to_artifact` (html keyword present, no force), `::test_normal_file_request_still_forced` (no regression) |
| RC4 | toolset assertions in `test_automation_creation_lifecycle.py` |
| RC5 | frontend: draft creation on Save & Continue; Confirm & Activate calls confirm API; card reflects server validation (manual + component tests where infra exists) |
| RC6 | `::test_update_automation_cannot_activate_under_v2` |
| confirm semantics | `::test_confirm_activate_creates_active_definition`, `::test_confirm_idempotent_and_no_execution` |

## 6. Architectural invariant restored

`CREATE DRAFT ≠ CONFIRM DEFINITION ≠ EXECUTE AUTOMATION ≠ GENERATE ARTIFACT`

- **Create draft:** `propose_automation` (chat) / `POST /automations/drafts` (form) — validation only, no task row, no datasource selection, no execution, no artifact.
- **Confirm:** `POST /automations/drafts/{id}/confirm` (+`activate=true`) — exactly-once, If-Match, audited, zero execution side effects.
- **Execute:** only dispatcher-claimed occurrences (scheduled) or explicit user Run-Now; via the Agent Invocation Contract / production harness.
- **Artifact:** generated from an execution's real result by the executor's configured output format — never from a creation conversation.
