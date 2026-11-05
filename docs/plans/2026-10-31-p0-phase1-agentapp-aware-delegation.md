# P0 Phase 1 — AgentApp-Aware Delegation Implementation Plan

**Goal:** Make `delegate_task` run children with their own AgentApp 5-layer prompt + configured tools, inherit parent data bindings, and link delegation invocations to the parent conversation; correct PO-drafting agent risk tiers.

**Refs:** `docs/architecture/zhanlu-current-enterprise-agent-architecture-audit.md` §10 (breakpoints ①②③④), roadmap P0-1..P0-4.

**Constraints:** no new orchestrator; flag-gated (`DELEGATE_AGENTAPP_AWARE`, `DELEGATE_INHERIT_BINDINGS`, default **False** per roadmap — enabled via env for the Phase 2 pilot); fallback path byte-identical when flags off; child permission checks run as the CHILD agent identity (never inherit parent `agent_name` — meta-agent bypass must not leak into children); existing uncommitted work in `config.py`/`delegation_tools.py` untouched beyond additive lines.

---

## Task 1: Feature flags (config.py)

**Modify:** `backend/app/config.py` (additive, near the DELEGATE_* block ~L452/1731)

```python
# P0-1/P0-2 (2026-10-31): AgentApp-aware delegation. When True, delegate_task
# resolves a published AgentApp by agent_name and runs the child with its own
# 5-layer prompt + tool_config (breakpoint ②), and passes the parent's
# conversation_id + bound_kb_ids into child tool context (breakpoint ③).
# Default False (audit/roadmap); enabled per-deployment via env.
DELEGATE_AGENTAPP_AWARE: bool = False
DELEGATE_INHERIT_BINDINGS: bool = False
```

## Task 2: AgentApp-aware child resolution (delegate_tool.py)

**Modify:** `backend/app/services/tool_handlers/delegate_tool.py`

1. New helper `_resolve_agent_app(db, agent_name)` — AgentApp by exact name, `is_deleted=False`, `status=="published"`, exception-safe → None.
2. In `_run_sub_agent_inner`: resolve `agent_app` when flag on; `get_system_prompt(agent_name, agent_app=agent_app)` (signature already supports it, `agent_prompts.py:2380-2454`).
3. Toolset: if `agent_app` has non-empty `tool_config.enabled_tools` → toolset = exactly those tools (minus `_RESTRICTED_TOOLS`, only registry-resolvable), and **skip** the CRUD-schema injection; else current default behavior unchanged.
4. `agent_app` returned via result dict (`"_agent_app_id"`) for the invocation record.

## Task 3: Binding inheritance (delegate_tool.py)

**Modify:** `_run_sub_agent_inner` tool-execution paths (single call L463-473, parallel `_exec_one` L475-483).

Build once:
```python
_child_ctx = {"agent_name": agent_name}   # child identity for permission/tier checks
if settings.DELEGATE_INHERIT_BINDINGS and context:
    if context.get("conversation_id"):
        _child_ctx["conversation_id"] = context["conversation_id"]
    if context.get("bound_kb_ids"):
        _child_ctx["bound_kb_ids"] = list(context["bound_kb_ids"])
if agent_app is not None:
    _child_ctx["agent_app_id"] = agent_app.id
```
NEVER inherit parent `agent_name` / `agent_app_id` (meta-agent bypass leak — `agent_tools.py:616` bypass keys off `context["agent_name"]`).
Pass `context=_child_ctx` to both `execute_tool` call sites.

## Task 4: Invocation trace linking (delegate_tool.py)

**Modify:** `_run_sub_agent` finally-block `record_invocation` (L287-302):
- `conversation_id=(context or {}).get("conversation_id")` (was hardcoded None)
- `agent_app_id=<child AgentApp id when resolved>`
- `output_json` gains `"agent_name": agent_name` and `"truncated"`.

## Task 5: Risk tiers (scripts/create_manufacturing_agents.py + rerun)

- Per-agent `policy_profile`: Supply Chain Optimization + Inventory Management → `{"risk_tier": "medium", "requires_confirmation": False}` (PO drafts remain gated by the draft-approval flow; `medium` = classification honesty; escalation tiers are high/critical only — `agent_tier.py:28`).
- Fix the update branch of `upsert_agent` to also refresh `policy_profile`.
- Rerun idempotently in `zhanlu-backend`.

## Task 6: Tests (new `backend/tests/test_delegate_agentapp_aware.py`)

Mock `call_llm_with_reliability` + `execute_tool` (monkeypatch in module namespace). SQLite in-memory, AgentApp rows created inline. Cases:
1. `test_agentapp_prompt_and_toolset_used` — published app w/ marker prompt + `enabled_tools=["query_database"]` → child LLM prompt contains marker; tool schemas exactly `["query_database"]`; no CRUD schemas.
2. `test_builtin_agent_unaffected` — `general_assistant` w/ flags on → default toolset (CRUD present), builtin prompt.
3. `test_unpublished_app_falls_back` — `status="active"` app → generic prompt path.
4. `test_flag_off_unchanged` — flags off, published app → generic prompt + default toolset.
5. `test_binding_inheritance` — parent context `{"conversation_id": "c1", "bound_kb_ids": ["kb1"]}` → captured `execute_tool` context has both + `agent_name == child name`, and NOT parent agent_name.
6. `test_no_parent_meta_bypass_leak` — parent context `agent_name="general_assistant"` → child ctx agent_name is the child.
7. `test_invocation_links_conversation` — record_invocation captured with `conversation_id="c1"`.

Run: `docker exec -w /app -e PYTHONPATH=/app zhanlu-backend python -m pytest tests/test_delegate_agentapp_aware.py tests/test_multiagent_mas.py tests/test_tool_timeouts_p1.py -v`

## Task 7: Regression + report

Baseline first: existing MAS/timeout/swarm suites. Then full targeted set after changes. Report results + enable flags in deployment env (Phase 2).
