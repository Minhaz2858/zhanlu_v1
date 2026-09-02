# NL2SQL-First Database Node — Implementation Plan

> **For Hermes:** Execute task-by-task with TDD. Direct execution, targeted pytest (RAM-tight).

**Goal:** Make the db_query workflow node usable by non-IT people. The governed NL2SQL engine already exists backend-side (`db_query mode="nl"` → `app.services.nl2sql.ask` with schema context + fallback translator). The blocker is the form: defaults to SQL mode, shows SQL+Question simultaneously, forces a connector dropdown, and piles Limit/Retry/Continue-on-fail on top.

**Approach:** schema-level changes only — no runtime changes, no migration, backward compatible (existing saved configs keep their stored mode; only NEW nodes default to `nl`).

---

## Task 1: Backend — NL-first db_query schema + advanced/conditional prop support (TDD)

**Files:**
- Modify: `backend/app/services/workflow_node_types.py`
- Create: `backend/tests/test_workflow_node_types.py`

**Step 1: Failing tests** (new file, no DB needed — pure registry assertions):

```python
"""Tests for the workflow node registry (workflow_node_types.py)."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services import workflow_node_types as wnt

def _db_query():
    return wnt.get_node_type("db_query")


def test_db_query_defaults_to_natural_language():
    mode = next(p for p in _db_query()["properties"] if p["name"] == "mode")
    assert mode["default"] == "nl"
    assert mode["options"][0]["value"] == "nl"  # NL listed first


def test_db_query_question_and_sql_are_mode_conditional():
    props = {p["name"]: p for p in _db_query()["properties"]}
    assert props["question"]["visible_when"] == {"mode": "nl"}
    assert props["sql"]["visible_when"] == {"mode": "sql"}


def test_db_query_limit_is_advanced():
    assert next(p for p in _db_query()["properties"] if p["name"] == "limit")["advanced"] is True


def test_kb_selector_is_named_database_for_non_it_users():
    kb = next(p for p in _db_query()["properties"] if p["name"] == "kb_id")
    assert kb["displayName"] == "Database"
    assert kb["required"] is True


def test_error_handling_props_are_advanced_on_every_node_type():
    for t in wnt.list_node_types():
        for name in ("retryOnFail", "continueOnFail"):
            p = next(x for x in t["properties"] if x["name"] == name)
            assert p["advanced"] is True, f"{t['name']}.{name} not advanced"


def test_required_config_fields_unchanged_for_db_query():
    # Only kb_id is required — question/sql stay optional (conditional).
    assert wnt.required_config_fields("db_query") == ["kb_id"]
```

**Step 2: Run — expect FAIL** (`'default' mismatch`, `'visible_when'` key absent, etc.).

**Step 3: Implement:**
- Extend `prop()` with `visible_when: dict | None = None` and `advanced: bool = False` kwargs (emit `p["visible_when"]` / `p["advanced"]`).
- Rewrite the `db_query` properties:
  - `notice(...)` guidance line first: "Ask in plain language — the AI searches your tables and writes the SQL for you."
  - `prop("Database", "kb_id", "options", required=True, description="Your connected database — the AI reads its tables automatically. Loaded from your connectors (database-type only).", options=[], load_options_method="listDatabases")`
  - `prop("Mode", "mode", "options", default="nl", options=[{NL}, {SQL}], description="Natural language: ask in plain words, the AI writes the SQL. SQL: write the statement yourself.")`
  - `prop("Question", "question", "string", placeholder="e.g. total revenue by month", description="Ask in plain language — the AI finds the right tables and writes the SQL for you.", visible_when={"mode": "nl"})`
  - `prop("SQL", "sql", "string", placeholder=..., description=..., visible_when={"mode": "sql"})`
  - `prop("Limit", "limit", "number", default=50, description="Maximum rows returned.", advanced=True)`
- Mark both `ERROR_HANDLING_PROPS` entries `advanced=True`.

**Step 4: Run — expect PASS.** Commit.

## Task 2: Frontend — visible_when + Advanced section + single-connector auto-select (TDD)

**Files:**
- Modify: `frontend/src/components/workflow/NodeParameterForm.jsx`
- Create: `frontend/src/components/workflow/NodeParameterForm.test.jsx` (source-contract, repo convention)

**Step 1: Failing source-contract test:**

```js
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './NodeParameterForm.jsx'), 'utf8');

describe('NodeParameterForm.jsx non-IT-first schema support', () => {
  it('hides conditional fields via visible_when (n8n displayOptions.show)', () => {
    expect(SOURCE).toMatch(/visible_when/);
    expect(SOURCE).toMatch(/config\?\.\[k\]\s*===\s*v/); // config[k] === v evaluation
    expect(SOURCE).toMatch(/if\s*\(!visible\)\s*return\s*null/);
  });
  it('renders advanced props in a collapsible Advanced (optional) section', () => {
    expect(SOURCE).toMatch(/Advanced\s*\(optional\)|高级设置/);
    expect(SOURCE).toMatch(/details/);
    expect(SOURCE).toMatch(/\.filter\(\(p\)\s*=>\s*!p\.advanced\)/);
  });
  it('auto-selects the single option in a picker (connect once, no dropdown)', () => {
    expect(SOURCE).toMatch(/options\.length\s*===\s*1/);
    expect(SOURCE).toMatch(/onChange\(options\[0\]\.value\)/);
  });
});
```

**Step 2: Run — expect FAIL.**

**Step 3: Implement in NodeParameterForm.jsx:**
- `SchemaField`: at the top, evaluate `prop.visible_when` against `config` — `Object.entries(prop.visible_when).every(([k, v]) => config?.[k] === v)`; return null when hidden.
- `NodeParameterForm`: split `def.properties` into `basic` / `advanced` (`p.advanced`); render advanced under a native `<details>`/`<summary>` "Advanced (optional)" / "高级设置（可选）" (add ChevronDown import).
- `OptionsInput` (single-select only, not multiOptions): when options resolve to exactly one and value is empty, auto-select it — guarded by a `useRef` so it fires once (avoids onChange-identity infinite loop).

**Step 4: Run — expect PASS** (plus existing agentEnhanced/WorkflowBuilderChat tests untouched). `npm run build`.

## Task 3: Verify on the real workflow + commit

- `docker compose restart backend` (registry served at runtime) — actually registry reads from the bind-mounted file; restart not needed for backend, only frontend build.
- `npm run build` (frontend dist bind-mounted to nginx).
- Browser: open `http://localhost:8088/my-space/workflow/d5288318-bbd1-48d6-9816-e450b90d4707` → open the db_query node → assert: notice line, Database auto-selected (if exactly one connector), Question field only, Limit + Retry/Continue under Advanced.
- Commit.

## Notes
- Existing db_query nodes keep their saved `mode` value — no behavior change for saved workflows.
- The registry is served via `list_node_types()`; the form renders it at runtime, so the backend change flows through with zero API changes.
- Deferred (follow-ups): connector-creation UX simplification, friendlier empty-question validation message at runtime.
