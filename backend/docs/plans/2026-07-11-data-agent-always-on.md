# Make Data Agent Always-On (No User Choice) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the `ask_data_agent` subagent-delegation tool the **only** way agents reach database data — always injected when KBs are bound, with no UI toggle and no direct granular-tools fallback.

**Architecture:** Strip the `use_data_agent` flag and the granular-tools code path out of the runtime. The Data Agent remains a builtin subagent and is still reachable via `ask_data_agent` from any agent with bound data sources. The 4 granular tools (`list_data_sources`, `describe_schema`, `execute_query`, `answer_from_database`) are **kept registered** (the subagent still calls them) but are **no longer auto-injected** onto other agents. UI checkbox is removed; the system-prompt section no longer branches on a flag.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, stdlib `unittest`, React (JSX).

---

## Task 1: Simplify `data_source_runtime.py` to a single always-data-agent path

**Files:**
- Modify: `backend/app/services/data_source_runtime/data_source_runtime.py` (full rewrite of `prepare_data_source_runtime` + `_build_data_source_prompt_section`; drop `_GRANULAR_DB_TOOLS` and `use_data_agent` reads)
- Test: `backend/app/services/data_source_runtime/_e2e.py` (rewrite the 5 runtime-injection tests for the new always-on behavior)

**Step 1: Write the failing test**

Rewrite the 5 tests in `_RuntimeInjection` so they assert the **new** contract:

```python
# in _e2e.py — replace _RuntimeInjection class

class _RuntimeInjection(unittest.TestCase):
    def setUp(self) -> None:
        # ... (unchanged: file-backed metadata DB + KB + sqlite fixture) ...
        pass  # keep existing setUp/tearDown

    def _names(self, tools):
        return {t["function"]["name"] for t in tools}

    def test_bound_kb_injects_ask_data_agent(self):
        """Any agent with a bound DB KB gets ask_data_agent — no flag, no opt-out."""
        from app.services.data_source_runtime import prepare_data_source_runtime
        agent = _make_agent(self.db, self.kb_id, use_data_agent=None)  # ignore flag
        tools, prompt, extras = prepare_data_source_runtime(
            self.db, agent, base_tools=[], base_system_prompt="BASE",
        )
        names = self._names(tools)
        self.assertIn("ask_data_agent", names)
        # Granular tools are NOT injected onto the calling agent
        for n in ("list_data_sources", "describe_schema", "execute_query", "answer_from_database"):
            self.assertNotIn(n, names)
        self.assertIn("Bound Data Sources", prompt)
        self.assertIn("E2E Sales", prompt)
        self.assertEqual(extras.get("bound_kb_ids"), [self.kb_id])

    def test_no_kb_is_noop(self):
        # ... (unchanged) ...
        pass

    def test_does_not_double_inject(self):
        # ... (unchanged — but no use_data_agent param) ...
        agent = _make_agent(self.db, self.kb_id, use_data_agent=None)
        base_tools = [{
            "type": "function",
            "function": {"name": "ask_data_agent", "description": "pre-existing"},
        }]
        tools, _, _ = prepare_data_source_runtime(
            self.db, agent, base_tools=base_tools, base_system_prompt="BASE",
        )
        count = sum(1 for t in tools if t["function"]["name"] == "ask_data_agent")
        self.assertEqual(count, 1)
```

**Step 2: Run the test to verify it fails**

```bash
cd /root/zhanlu/backend
PYTHONPATH=. ./venv/bin/python -m unittest app.services.data_source_runtime._e2e._RuntimeInjection -v
```

Expected: `test_bound_kb_injects_ask_data_agent` and `test_does_not_double_inject` FAIL with `KeyError: 'use_data_agent'` or similar — because `_make_agent` still expects that kwarg AND the runtime still branches on it.

**Step 3: Simplify the runtime**

In `data_source_runtime.py`:

1. Delete `_GRANULAR_DB_TOOLS` (no longer referenced).
2. Rewrite the module docstring to drop the `use_data_agent` toggle.
3. Simplify `_build_data_source_prompt_section(bound_meta)` — no second arg, always describes `ask_data_agent`.
4. Rewrite `prepare_data_source_runtime` to a single path:
   - If no bound KBs → return base unchanged.
   - Else → inject `ask_data_agent` (idempotent), append the always-data-agent prompt section, return `{"bound_kb_ids": bound_ids}`.
5. Remove the `tool_config.get("use_data_agent", True)` read.

**Step 4: Update `_make_agent` in `_e2e.py`** to drop the `use_data_agent` parameter (or keep it as a no-op backward-compat kwarg that just sets `tool_config` but is ignored by the runtime).

**Step 5: Run the test to verify it passes**

```bash
cd /root/zhanlu/backend
PYTHONPATH=. ./venv/bin/python -m unittest app.services.data_source_runtime._e2e -v
```

Expected: all 7 tests in the file pass (5 in `_RuntimeInjection` + 2 in `_AskDataAgentHandler`).

---

## Task 2: Remove the "Use Data Agent" UI checkbox

**Files:**
- Modify: `frontend/src/components/agent/DataSourcesSection.jsx` (drop the checkbox block + the `use_data_agent` write)
- Modify: (no frontend i18n file changes required — the `t.agentConfig.useDataAgent*` keys are read; setting them to empty strings in i18n is a follow-up if needed; for now they're unreferenced and harmless)

**Step 1: Write the failing assertion (visual diff / grep)**

Add a temporary grep test inline (manual, but documented):

```bash
cd /root/zhanlu
grep -n "use_data_agent" frontend/src/components/agent/DataSourcesSection.jsx
```

Expected: 4 lines currently match (lines 66, 68, 75, 76).

**Step 2: Remove the checkbox block**

In `DataSourcesSection.jsx`, delete the entire `{selected.length > 0 && (<label>…</label>)}` block (lines 61–80 in the current file). Keep the chip list above and the picker button below.

**Step 3: Verify the grep now returns zero matches**

```bash
cd /root/zhanlu
grep -n "use_data_agent" frontend/src/components/agent/DataSourcesSection.jsx
```

Expected: no output.

---

## Task 3: Drop `use_data_agent` from the runtime path that the agents router uses

**Files:**
- Modify: `backend/app/services/data_source_runtime/data_source_runtime.py` — already done in Task 1.
- Modify: `backend/app/routers/agents.py` — no change needed; the call to `prepare_data_source_runtime` already only passes `(db, agent_app, tools, system_prompt)` and never reads `use_data_agent` directly. The `tool_config` is read only in `prepare_data_source_runtime`.

**Step 1: Confirm no other code reads `use_data_agent`**

```bash
cd /root/zhanlu
grep -rn "use_data_agent" backend/app/ frontend/src/
```

Expected after Tasks 1 & 2: only `data_source_runtime.py` references remain — and only in the docstring/import areas, not in active logic. If any active code path remains, remove it.

---

## Task 4: Update the verification doc

**Files:**
- Modify: `backend/docs/plans/2026-07-11-data-agent-e2e.md` (rewrite the "What was verified" + test breakdown table to reflect the new contract — only `ask_data_agent` is injected; the granular tools are still called by the subagent but not auto-injected)

**Step 1: Edit the test breakdown table**

Replace the two `use_data_agent=true|false` rows with one row:

```
| 1 | `test_bound_kb_injects_ask_data_agent` | Agent with a bound DB KB → `ask_data_agent` in tools, 4 granular tools NOT injected, "Bound Data Sources" section in prompt, `bound_kb_ids` in context. |
```

Drop row 2 (`test_use_data_agent_false_adds_granular_tools`) and row 3 (`test_default_uses_data_agent`) — they're obsolete.

Add a short note:

> The 4 granular tools (`list_data_sources`, `describe_schema`, `execute_query`, `answer_from_database`) are still registered and called by the `data_agent` subagent via `ask_data_agent`. They are no longer auto-injected onto the calling agent's tool list — the calling agent always delegates to the Data Agent instead.

**Step 2: Update the "How to re-run" command** if test class names changed.

---

## Task 5: Final verification

**Step 1: Run all data-agent tests**

```bash
cd /root/zhanlu/backend
PYTHONPATH=. ./venv/bin/python -m unittest \
  app.services.db._test_kb_runner \
  app.services.data_source_runtime._e2e -v
```

Expected: all 22 tests pass (15 DB + 7 E2E).

**Step 2: Smoke-check the runtime import path**

```bash
cd /root/zhanlu/backend
PYTHONPATH=. ./venv/bin/python -c "
from app.services.data_source_runtime import prepare_data_source_runtime
from app.services.tool_handlers import db_tools, delegation_tools
print('OK — runtime + tools import cleanly')
print('ask_data_agent registered:', delegation_tools.ASK_DATA_AGENT_SCHEMA['function']['name'])
"
```

Expected: prints `OK — runtime + tools import cleanly` and `ask_data_agent registered: ask_data_agent`.

**Step 3: Grep final state**

```bash
cd /root/zhanlu
grep -rn "use_data_agent" backend/app/ frontend/src/ backend/docs/ 2>/dev/null
```

Expected: zero matches (or only the docstring/comment lines we chose to keep for historical context — ideally zero).
