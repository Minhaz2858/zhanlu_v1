# Data Agent E2E Verification — 2026-07-11

## What was verified

A scripted end-to-end test (`app/services/data_source_runtime/_e2e.py`)
covers the full path from the agent chat runtime down to a real SQLite
query and back. All 6 tests pass.

> **Behavior:** The Data Agent is always-on. When an agent has a bound
> database KB, the runtime auto-injects the `ask_data_agent` tool (which
> delegates to the builtin `data_agent` subagent). The 4 granular DB
> tools (`list_data_sources`, `describe_schema`, `execute_query`,
> `answer_from_database`) are no longer auto-injected onto the
> user-facing agent — they are still registered and called internally
> by the `data_agent` subagent.

### Test breakdown

| # | Test | Asserts |
|---|------|---------|
| 1 | `test_bound_kb_injects_ask_data_agent` | Agent with a bound DB KB → `ask_data_agent` in tools, 4 granular tools NOT in tools, "Bound Data Sources" section in prompt, `bound_kb_ids` in context. |
| 2 | `test_no_kb_is_noop` | Agent with `knowledge_bases=[]` → function returns base tools + base prompt unchanged, no context extras. |
| 3 | `test_does_not_double_inject` | If `ask_data_agent` is already in `base_tools`, it's added exactly once. |
| 4 | `test_handler_with_mocked_llm` | `_ask_data_agent` with a mocked LLM that emits `execute_query` then a final text reply → returns `{success, answer, rows (2 rows, EU=350, US=675), sql, source_id, source_name, iterations≥2}`. |
| 5 | `test_handler_rejects_unbound_kb` | Calling `_ask_data_agent` with a `data_source_id` not in `bound_kb_ids` → `success=false`, error mentions "not bound". |
| 6 | `test_main_agent_uses_ask_data_agent_to_get_data` | **Live agentic E2E** — main agent with bound KB sees `ask_data_agent` (no granular tools) in its tool list, gets the "Bound Data Sources" section in its prompt, calls `ask_data_agent` exactly once, and the final assistant content includes the data the user asked for ("350" and "675"). Mocks both the main-agent LLM and the subagent LLM. |

### Result

```
Ran 6 tests in 1.274s — OK
```

The DB services module's own 15 unit tests also pass:

```
Ran 15 tests in 0.261s — OK
```

Total: 21/21 tests pass.

### How to re-run

```bash
cd /root/zhanlu/backend
PYTHONPATH=. ./venv/bin/python -m unittest \
  app.services.db._test_kb_runner \
  app.services.data_source_runtime._e2e
```

## What was NOT verified in this script

- **Real LLM end-to-end** — the `ask_data_agent` test mocks the LLM
  response. A live LLM run would need network + a configured LLM
  endpoint; out of scope for this verification gate.
- **Other dialects** — MySQL/Postgres/MSSQL/Oracle connectors are
  defined and follow the same `BaseConnector` protocol, but were not
  exercised against live servers (none available in this environment).
- **Frontend toggle removal** — the "Use Data Agent" checkbox in
  `DataSourcesSection.jsx` has been removed. End-to-end save/load
  through the API was not exercised (would need a running backend + LLM).

## Migration (2026-07-11)

The `tool_config.use_data_agent` field is now ignored at runtime.
Alembic migration `002_drop_use_data_agent.py` removes the key from
every existing `agent_apps.tool_config` JSON row. Other keys in the
JSON (e.g. `enabled_tools`, `disabled_tools`) are preserved. Tested
on SQLite; the file contains PostgreSQL and MySQL branches for
production deploys.
