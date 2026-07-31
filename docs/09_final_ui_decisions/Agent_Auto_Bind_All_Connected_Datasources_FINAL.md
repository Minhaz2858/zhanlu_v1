# Agent → Datasource Auto-Bind (DATA-CORE-3 Opt-In) — FINAL

> Authoritative spec for the workspace-level opt-in flag that lets **every
> agent in a workspace read from every connected database KnowledgeBase**
> without having to bind each KB to each agent manually.

This spec resolves the user report: *"the agent has database access
configured in the UI but can't actually answer from the database when
I click Run Agent."* The fix has three parts:

1. The `Chat.jsx` "Run Agent" path now routes through the v3 conversations
   endpoint (which calls `prepare_data_source_runtime`) instead of the
   raw `InvokeLLMStream` proxy.
2. The agent's bound KnowledgeBases are auto-injected as the
   `ask_data_agent` tool schema so the LLM has a function-calling
   surface to actually query the database.
3. A new workspace-level opt-in flag lets the user extend the bound set
   to **every connected database KB** in the same `(org, app)`.

The default is OFF — DATA-CORE-3 ("each agent can only use datasources
explicitly selected by the user") is preserved unless the user opts in.

---

## 1. The bug (root cause)

The previous flow:

1. `AgentConfig.jsx:213-215` — "Run Agent" → `navigate('/?agent={id}')`
2. `Chat.jsx:48-64` — picks up `?agent=...`, loads the `AgentApp`
3. `Chat.jsx:247-256` — calls
   `POST /api/apps/{appId}/integration-endpoints/Core/InvokeLLMStream`
   with body `{ prompt, response_json_schema, model, file_urls }`
   — **no `agent_id`, no `tools` field**.
4. `integrations.py:77-163` (`invoke_llm_stream`) is a raw LLM proxy.
   It forwards `prompt` + `model` to the upstream LLM. It does NOT
   load the `AgentApp`, does NOT run `prepare_data_source_runtime`,
   and does NOT pass any tools.

The LLM therefore has no function-calling surface for the database.
The `AgentApp.knowledge_bases` field is correctly stored, but the
chat path bypasses every tool wiring code.

The fix is to route through the v3 conversations endpoint
(`/apps/{app_id}/agents/conversations/v3/{cid}/messages/stream`),
which already calls `prepare_data_source_runtime` and exposes the
`ask_data_agent` tool. See `agents.py:1580-1586` for the existing
runtime prep.

## 2. Architecture (DATA-CORE-3 preserved)

```
┌────────────────────────────────────────────────────────────────────┐
│  User-facing chat page (Chat.jsx)                                  │
│  - detect activeAgent                                              │
│  - POST /agents/conversations { agent_name, metadata } -> conv_id  │
│  - streamAgentResponse(conv_id, ...)                               │
│      -> POST /agents/conversations/v3/{conv_id}/messages/stream    │
│                                                                    │
│  Backend (agents.py v3 endpoint)                                   │
│  - load AgentApp by conv.agent_name                                │
│  - tools, system_prompt, data_ctx_extras =                         │
│        prepare_data_source_runtime(db, agent_app, ...)            │
│      |- bound_ids = agent_app.knowledge_bases                      │
│      |- bound_ids = _maybe_extend_with_workspace_auto_bind(        │
│      |       db, agent_app, bound_ids)                             │
│      |     -> workspace_settings_service.get_bool(                 │
│      |            "auto_bind_all_datasources")                     │
│      |          -> False (default) | True (opt-in)                 │
│      |- inject ask_data_agent schema (idempotent)                  │
│      |- prepend anti-hallucination directive                       │
│      |- append "Bound Data Sources" section                        │
│  - run agent loop (system_prompt, memory, skills, tool calling)    │
└────────────────────────────────────────────────────────────────────┘
```

### DATA-CORE-3 invariant

> Each main agent can only use datasource handles explicitly selected
> by the user.

- **Default**: `auto_bind_all_datasources = false`. The bound set is
  exactly `agent_app.knowledge_bases`. No new access is granted.
- **Opt-in**: the user flips the toggle in My Space -> Settings ->
  "Agents & Datasources" -> "Allow every agent to read from every
  connected database". The bound set is then unioned with every
  `KnowledgeBase` row in the same `(org_id, app_id)` where
  `source_kind = 'database'` and `is_deleted = false`. This is a
  **workspace-wide** authorization — DATA-CORE-3 still holds
  because the user explicitly opted in at the workspace level.

### DATA-CORE-5 invariant

> All unselected user databases are invisible and blocked for that
> agent, its subagents, skills, MCP tools, and sandbox jobs.

- Tool handlers (`db_tools._require_kb_id`) check the per-call
  `bound_kb_ids` set in the call context. A request for a KB id not
  in the set returns a clear error and never reaches the
  `SchemaService` / `QueryService` / `NLAnswerService` gateway.
- This enforcement is unchanged by the new flag — the flag only
  expands the **set** of bound KBs, it does not bypass the
  enforcement. Tests `test_require_kb_id_rejects_unbound_kb_even_with_opt_in`
  and `test_require_kb_id_accepts_bound_kb` pin this.

### DATA-CORE-6 invariant

> All database access must go through the Datasource Gateway. No
  agent, subagent, skill, MCP tool, or sandbox may receive raw
  database credentials.

- The 4 DB tools (`list_data_sources`, `describe_schema`,
  `execute_query`, `answer_from_database`) all go through the
  `SchemaService` / `QueryService` / `NLAnswerService` gateway, which
  holds the connection credentials. The user-facing agent never
  receives the raw `db_zhanlu_no2` connection string.
- The new flag does not change this — it only widens the set of KBs
  the agent can call the gateway for.

---

## 3. Files changed

### Backend

- `app/models/workspace_settings.py` — new `WorkspaceSetting` model
  (key/value, per `(org_id, app_id, key)`, soft-deletable)
- `app/services/workspace_settings_service.py` — typed get/set with
  5s in-process memoization
- `app/services/data_source_runtime/data_source_runtime.py` —
  extended `prepare_data_source_runtime` to consult the opt-in flag
  and union the bound set when it is on
- `app/routers/workspace_settings.py` — `GET` / `PUT
  /api/workspace-settings` endpoints
- `app/models/__init__.py`, `main.py` — wire the new model and router
- `alembic/versions/010_workspace_settings.py` — schema migration
- `tests/test_agent_datasource_wiring.py` — 7 tests pinning the
  contracts (opt-in on unions all DBs, opt-in off is default, tool
  handlers reject unbound KBs even with opt-in, `ask_data_agent`
  schema is present iff at least one KB is bound)
- `tests/test_workspace_settings.py` — 8 tests for the service and
  the `/api/workspace-settings` router

### Frontend

- `src/api/agentEnhanced.js` — added `createAgentConversation` helper
  (POSTs to `/agents/conversations` to create a conversation bound
  to the active agent)
- `src/pages/Chat.jsx` — when `activeAgent` is set, create a backend
  conversation, then stream through the v3 endpoint via
  `streamAgentResponse` (preserving the existing SSE delta /
  tool_progress / done event handling); also resolves and passes
  `boundKbs` to the input for the chip + quick action
- `src/components/chat/ChatInput.jsx` — new "DB: ..." chip showing
  the bound database(s) and a "Read from my database" quick action
  that pre-fills a clear NL question and sends it
- `src/components/settings/WorkspaceDataSection.jsx` — opt-in toggle
  for `auto_bind_all_datasources`, default off
- `src/pages/Settings.jsx` — added a new "Agents & Datasources"
  section that mounts `WorkspaceDataSection`
- `src/lib/translations.js` — added bilingual labels/descs for the
  new section

---

## 4. UI surface

### Chat input — DB chip + quick action

When the active agent has at least one bound database KB, the chat
input renders a green "DB: ..." chip next to the agent chip. Hovering
the chip on a multi-DB agent shows a tooltip listing all bound
databases. A small "Read from my database" button pre-fills the
input with a clear question and sends it, which exercises the
`ask_data_agent` tool path end-to-end.

The button text and chip label are bilingual (zh / en). On narrow
viewports the chip and button collapse to icons to preserve input
width.

### Settings — opt-in toggle

Path: `My Space -> Settings -> Agents & Datasources`.

A single switch with explanatory copy:

> Allow every agent to read from every connected database
> (DATA-CORE-3 opt-in). When on, every database KnowledgeBase in
> the workspace is automatically added to every agent's bound
> list at runtime. Default is OFF.

The switch is off by default. The user must opt in to get the
"all agents -> all DBs" behavior.

---

## 5. Tests (must pass)

```
$ cd backend && source venv/bin/activate
$ python -m pytest tests/test_agent_datasource_wiring.py tests/test_workspace_settings.py -v
================ 15 passed in 0.7s ================
```

The contracts pinned:

1. `test_opt_in_flag_unions_all_connected_db_kbs` — opt-in ON unions
   every connected DB into the bound set
2. `test_opt_in_flag_default_off_only_uses_agent_bound_kbs` —
   default (no row) leaves the bound set unchanged
3. `test_opt_in_flag_explicitly_off_same_as_default` — explicit
   `value=false` is the same as no row
4. `test_require_kb_id_rejects_unbound_kb_even_with_opt_in` — tool
   handlers still reject un-bound KBs
5. `test_require_kb_id_accepts_bound_kb` — tool handlers accept
   bound KBs
6. `test_ask_data_agent_in_tool_list_when_kbs_bound` — tool schema
   is injected iff at least one KB is bound
7. `test_ask_data_agent_NOT_in_tool_list_when_no_kbs_bound` —
   inverse
8. `test_get_bool_returns_default_when_no_row` — default is False
9. `test_set_value_then_get_bool_round_trip` — round-trip true/false
10. `test_falsy_string_variants` — "0", "no", "off", "" all read False
11. `test_get_str_returns_none_when_not_set` — missing key -> None
12. `test_scopes_are_isolated` — different (org, app) scopes don't
    leak
13. `test_set_value_upserts` — same key upserts in place
14. `test_router_get_returns_default` — HTTP GET returns False
15. `test_router_put_round_trip` — HTTP PUT round-trips through the
    service

---

## 6. Operational notes

- The workspace setting is cached for **5 seconds** in process so
  that a tight agent loop (which calls `prepare_data_source_runtime`
  once per message) does not hit the DB on every iteration.
- The flag is workspace-scoped (`org_id` x `app_id`). The default
  for any (org, app) that has no row is `false`.
- The flag is a boolean (`true` / `false`). Falsy strings
  (`"0"`, `"no"`, `"off"`, `""`, `"False"`, `"FALSE"`) all read as
  `false`. Everything else is `true`.
- When the flag is on, the bound set is the union of the agent's
  explicit `knowledge_bases` and the auto-bound DB KBs (every
  connected database KB in the same workspace that the agent
  didn't already have). Auto-bound KBs are appended in stable
  name order so the prompt section is deterministic.
- The opt-in toggle UI is in My Space -> Settings -> Agents &
  Datasources. It's a single switch, no other configuration.
- The flag is **per workspace**, not per user. There is no per-user
  override; the user must belong to a workspace where the admin has
  enabled the flag.

## 7. Migration / rollout

- Run `alembic upgrade head` to add the `workspace_settings` table.
- The table is empty by default, so the flag is `false` for every
  workspace until someone explicitly flips the toggle.
- No data migration is needed for existing `AgentApp.knowledge_bases`
  rows — those are unchanged.
- The `Chat.jsx` change is backward compatible: when no agent is
  active, the old `InvokeLLMStream` path is used exactly as before.
- The `agents.py` v3 endpoint change is **no-op** — it already
  called `prepare_data_source_runtime`. The bug was that the chat
  page never reached it.

## 8. Future work

- Per-row policy on each `KnowledgeBase` to mark it as
  "always-auto-bound" so the opt-in can be partial.
- Per-agent override of the workspace opt-in (e.g. "this agent is
  excluded from auto-bind even when the workspace opt-in is on").
- A "preview" mode that lists which KBs an agent would have access
  to if the flag were on, without actually flipping it.
