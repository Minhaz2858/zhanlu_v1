# Workflow Items Pipeline + Data Nodes — Implementation Plan

> **For Hermes:** Execute task-by-task (TDD, commit after each task). Plan author executes directly.

**Goal:** Introduce n8n-style `items[]` as a first-class runtime field, add 5 data nodes (http_request, code, set_fields, split_out, remove_duplicates), and an insert-field expression picker — without breaking existing text-based flows.

**Architecture:** Every node output becomes `{text, items}`. Handlers emit items from structured sources (rows, docs, HTTP JSON, code output); the executor already passes full output dicts through `ctx.outputs`/`current_upstream`, so no executor change is required. Expression resolver gains deep-path support (`items[0].field`). Data nodes live in the schema registry (Data category), handled in `workflow_nodes.py`, backed by new `ExecutionContext` methods in `workflow_runtime.py`.

**Tech Stack:** Python 3.11 (FastAPI), httpx (existing dep), stdlib `threading` + restricted `exec` for the code sandbox, React (NodeParameterForm.jsx).

**Spec:** `docs/superpowers/specs/2026-09-01-workflow-items-pipeline-design.md`

---

## Phase A — Runtime items

### Task 1: Deep-path expression resolver

**Objective:** `_render` resolves `{{inputs.n2.items[0].name}}` (and `{{inputs.n2.text}}`, `{{inputs.n2.items}}`).

**Files:**
- Modify: `backend/app/services/workflow_nodes.py:19-31`
- Test: `backend/tests/test_workflow_nodes.py`

**Step 1: Write failing tests**

```python
def test_render_resolves_item_field_paths():
    ctx = FakeCtx()
    ctx.outputs["n2"] = {"text": "rows", "items": [{"name": "Laptop", "price": 1200}, {"name": "Chair", "price": 150}]}
    assert _render("{{inputs.n2.items[0].name}} costs {{inputs.n2.items[0].price}}", ctx) == "Laptop costs 1200"
    assert _render("{{inputs.n2.items}}", ctx) == "[{'name': 'Laptop', 'price': 1200}, {'name': 'Chair', 'price': 150}]"
    assert _render("{{inputs.n2.text}}", ctx) == "rows"          # back-compat
    assert _render("{{inputs.n2.items[9].name}}", ctx) == ""      # out of range -> ""

def test_render_missing_path_returns_empty():
    ctx = FakeCtx()
    ctx.outputs["n2"] = {"text": "x", "items": [{"a": 1}]}
    assert _render("{{inputs.n2.items[0].missing}}", ctx) == ""
```

**Step 2: Run to verify failure** — `docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_nodes.py -q'` → FAIL (no such attribute / unchanged template).

**Step 3: Implement** — replace `_render` with a resolver that:
- parses `inputs.<node>.<rest>`; `rest` = `text` | `items` | `items[N].<path...>` | `items[N]`
- walks dicts/lists with `.get()`; returns `""` on missing/out-of-range; `str()` the final value
- keeps the existing regex; non-inputs placeholders left untouched

**Step 4: Verify pass** — same command → PASS (plus all existing handler tests).

**Step 5: Commit** — `git add backend/app/services/workflow_nodes.py backend/tests/test_workflow_nodes.py && git commit -m "feat(workflow): deep-path item expressions in _render"`

### Task 2: Item helpers

**Objective:** `first_item(ctx, node_id)` and `all_items(ctx, node_id)` in workflow_nodes.py; `item_text` fallback keeps back-compat.

**Files:** Modify: `backend/app/services/workflow_nodes.py`; Test: `backend/tests/test_workflow_nodes.py`

**Tests:**

```python
def test_first_item_defaults_to_text():
    ctx = FakeCtx(); ctx.outputs["n1"] = {"text": "hi"}
    assert first_item(ctx, "n1") == {"text": "hi"}

def test_all_items_passthrough():
    ctx = FakeCtx(); ctx.outputs["n1"] = {"text": "x", "items": [{"a": 1}]}
    assert all_items(ctx, "n1") == [{"a": 1}]
```

Implement as one-liners (`out = ctx.outputs.get(node_id, {})`; `out.get("items") or [{"text": out.get("text", "")}]`). Commit.

### Task 3: Existing handlers emit items

**Objective:** trigger → `[{"text": input}]`; llm → `[{"text": response}]`; agent/tool → `[{"text": result}]`; kb_search → `[{"text","source","score"}...]` (docs); db_query → rows; mcp_tool → `[{"text": result}]`.

**Files:** Modify: `backend/app/services/workflow_nodes.py` (each handler return gains `"items": [...]`); Test: `backend/tests/test_workflow_nodes.py`

**Tests** (one per handler):

```python
def test_trigger_emits_item():
    out = handle_trigger(FakeCtx(), _node("n1", "trigger_chat", {}))
    assert out["items"] == [{"text": "hello"}]

def test_db_query_emits_rows_as_items():
    ctx = FakeCtx(db_result={"text": "a", "rows": [{"a": 1}]})
    out = handle_db_query(ctx, _node("n6", "db_query", {"kb_id": "k", "mode": "sql", "sql": "S"}))
    assert out["items"] == [{"a": 1}]

def test_kb_search_emits_docs_as_items():
    ctx = FakeCtx(kb_results=[{"text": "d1", "source": "s1", "score": 0.9}])
    out = handle_kb_search(ctx, _node("n5", "kb_search", {"kb_id": "k", "query": "q"}))
    assert out["items"][0]["source"] == "s1"
```

Run all workflow tests → PASS. Commit.

### Task 4: Executor pass-through check

**Objective:** Confirm no executor change needed (output dicts already flow whole).

**Step:** Run full workflow suite: `docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_*.py tests/test_mcp_proxy.py -q'` → all PASS. Commit any fixes if red (none expected).

---

## Phase B — Data nodes

### Task 5: Registry — 5 new node types

**Objective:** `http_request`, `code`, `set_fields`, `split_out`, `remove_duplicates` in `workflow_node_types.py`, category Data, before `condition`. Node count 13 → 18.

**Files:** Modify: `backend/app/services/workflow_node_types.py`; Test: `backend/tests/test_workflow_registry.py` (or existing registry test file)

**Registry entries (exact):**

```python
"http_request": {
    "name": "http_request", "displayName": "HTTP Request", "category": "Data",
    "icon": "🌐", "color": "#0ea5e9",
    "description": "Call a REST API", "description_zh": "调用 REST API",
    "inputs": ["main"], "outputs": ["main"],
    "properties": [
        prop("Method", "method", "options", default="GET",
             options=[{"name": n, "value": n} for n in ("GET", "POST", "PUT", "PATCH", "DELETE")]),
        prop("URL", "url", "string", default="", required=True,
             placeholder="https://api.example.com/items?q={{inputs.n1.items[0].text}}"),
        prop("Headers", "headers", "collection", default=[],
             options=[prop("Key", "key", "string"), prop("Value", "value", "string")],
             itemLabel="Header"),
        prop("Body (JSON)", "body", "json", default="",
             placeholder='{"q": "{{inputs.n1.text}}" }'),
        prop("Auth", "auth_type", "options", default="none",
             options=[{"name": "None", "value": "none"}, {"name": "Bearer token", "value": "bearer"}]),
        prop("Bearer token", "bearer_token", "string", default="", placeholder="{{inputs.n1.text}}"),
        prop("Timeout (s)", "timeout", "number", default=15),
        prop("Response format", "response_format", "options", default="auto",
             options=[{"name": "Auto", "value": "auto"}, {"name": "JSON", "value": "json"}, {"name": "Text", "value": "text"}]),
    ],
},
"code": {
    "name": "code", "displayName": "Code", "category": "Data",
    "icon": "⌨️", "color": "#10b981",
    "description": "Run Python to transform the input item", "description_zh": "运行 Python 转换输入数据",
    "inputs": ["main"], "outputs": ["main"],
    "properties": [
        prop("Language", "language", "options", default="python",
             options=[{"name": "Python", "value": "python"}]),
        prop("Code", "code", "string", default="", required=True,
             placeholder='output = {"name": input.get("name"), "total": input.get("price", 0) * 2}'),
        prop("Timeout (s)", "timeout", "number", default=10),
    ],
},
"set_fields": {
    "name": "set_fields", "displayName": "Set Fields", "category": "Data",
    "icon": "✏️", "color": "#f59e0b",
    "description": "Add or overwrite fields on each item", "description_zh": "为每个数据项添加或覆盖字段",
    "inputs": ["main"], "outputs": ["main"],
    "properties": [
        prop("Fields", "fields", "collection", default=[],
             options=[prop("Name", "name", "string"), prop("Value (template)", "value", "string")],
             itemLabel="Field"),
    ],
},
"split_out": {
    "name": "split_out", "displayName": "Split Out", "category": "Data",
    "icon": "✂️", "color": "#f59e0b",
    "description": "Expand an array field into one item per element", "description_zh": "将数组字段拆分为多条数据",
    "inputs": ["main"], "outputs": ["main"],
    "properties": [
        prop("Field", "field", "string", default="items",
             placeholder="{{inputs.n1.items[0].tags}}"),
        prop("Item field name", "item_field", "string", default="item"),
    ],
},
"remove_duplicates": {
    "name": "remove_duplicates", "displayName": "Remove Duplicates", "category": "Data",
    "icon": "🧹", "color": "#f59e0b",
    "description": "Remove items with duplicate values for a key", "description_zh": "按字段去重",
    "inputs": ["main"], "outputs": ["main"],
    "properties": [prop("Key field", "field", "string", default="id")],
},
```

**Test:** registry count == 18, each new type has `required_config_fields` (http_request → `["url"]`, code → `["code"]`).

### Task 6: HTTP Request — runtime + handler

**Objective:** `ctx.http_request(method, url, headers, body, timeout, auth_type, bearer_token, response_format)` + `handle_http_request`.

**Files:**
- Modify: `backend/app/services/workflow_runtime.py` (add method to ExecutionContext)
- Modify: `backend/app/services/workflow_nodes.py` (handler + NODE_HANDLERS entry)
- Create: `backend/tests/test_http_node.py` (runtime test uses a local stdlib HTTP stub in a thread)

**Runtime impl (exact):**

```python
def http_request(self, method, url, headers, body, timeout=15,
                 auth_type="none", bearer_token="", response_format="auto"):
    import httpx
    hdrs = {h["key"]: str(h["value"]) for h in (headers or []) if h.get("key")}
    if auth_type == "bearer" and bearer_token:
        hdrs["Authorization"] = f"Bearer {bearer_token}"
    data = None
    if method != "GET" and body and body.strip():
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = body
    resp = httpx.request(method, url, headers=hdrs, json=data if isinstance(data, dict) else None,
                         content=data if isinstance(data, str) else None,
                         timeout=timeout, follow_redirects=True)
    text = resp.text[:1_000_000]
    items = None
    if response_format != "text" and resp.headers.get("content-type", "").startswith("application/json"):
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            items = None
    if items is None:
        items = [{"body": text, "status": resp.status_code}]
    ok = 200 <= resp.status_code < 300
    return {"text": text, "items": items, "status": resp.status_code, "success": ok}
```

**Handler:** render url/headers/body/bearer templates → call ctx.http_request → return `{text, items, status, success}`; on exception return `{"text": f"HTTP error: {exc}", "items": [], "status": 0, "success": False}`.

**Tests:**
- unit (fake ctx): template rendering; headers collection → dict; bearer adds Authorization.
- runtime (real httpx): start `http.server` stub in thread serving `/json` (returns `[{"id":1},{"id":2}]` JSON) and `/fail` (404); assert items parsed, non-2xx → success False.

### Task 7: Code node — sandbox + handler

**Objective:** `ctx.run_code(code, item, timeout)` + `handle_code`.

**Files:** Modify: `backend/app/services/workflow_runtime.py`, `backend/app/services/workflow_nodes.py`; Create: `backend/tests/test_code_node.py`

**Runtime impl (exact):**

```python
def run_code(self, code: str, item: dict, timeout: int = 10) -> dict:
    import threading, json as _json
    restricted = {
        "json": _json, "math": __import__("math"),
        "len": len, "str": str, "int": int, "float": float,
        "bool": bool, "list": list, "dict": dict, "input": item,
    }
    box = {}
    def _run():
        try:
            exec(compile(code, "<code>", "exec"), restricted, box)
        except Exception as exc:  # noqa: BLE001
            box["_error"] = repr(exc)
    t = threading.Thread(target=_run, daemon=True)
    t.start(); t.join(timeout)
    if t.is_alive():
        return {"success": False, "text": f"Code timed out after {timeout}s"}
    if "_error" in box:
        return {"success": False, "text": f"Code error: {box['_error']}"}
    if "output" not in box:
        return {"success": False, "text": "Code must assign an `output` variable"}
    out = box["output"]
    try:
        _json.dumps(out)  # JSON-serializability gate
    except TypeError:
        return {"success": False, "text": "output is not JSON-serializable"}
    if isinstance(out, dict):
        return {"success": True, "text": _json.dumps(out, ensure_ascii=False), "items": [out]}
    if isinstance(out, list):
        return {"success": True, "text": _json.dumps(out, ensure_ascii=False), "items": out}
    return {"success": True, "text": str(out), "items": [{"value": out}]}
```

**Handler:** render code template → `ctx.run_code(code, first_item(ctx, upstream_id))`; upstream = first node in ctx.current_upstream.

**Tests:** happy dict; happy list; missing output var; exception in code; timeout (infinite loop, timeout=1); non-serializable output; `import os` blocked (ImportError raised because `__builtins__` has no import — assert error text contains "import" or "name 'os' is not defined").

### Task 8: Transform handlers

**Objective:** `handle_set_fields`, `handle_split_out`, `handle_remove_duplicates` + NODE_HANDLERS entries.

**Files:** Modify: `backend/app/services/workflow_nodes.py`; Test: `backend/tests/test_workflow_nodes.py`

**Exact implementations:**

```python
def handle_set_fields(ctx, node):
    config = node["data"].get("config") or {}
    items = [dict(it) for it in all_items(ctx, _upstream_id(ctx, node))]
    for it in items:
        for f in config.get("fields") or []:
            if f.get("name"):
                it[f["name"]] = _render(str(f.get("value", "")), ctx)
    return {"text": _json_summary(items), "items": items}
```

`_upstream_id(ctx, node)` = first key of `ctx.current_upstream` (fallback: first key of ctx.outputs). `_json_summary(items)` = `json.dumps(items[:3], ensure_ascii=False)` if items else "0 items". split_out: for each item, read `item.get(field)` (rendered) if list → one output item `{**item, item_field: element}`; else keep item. remove_duplicates: seen keys by `item.get(field)` (render field first). Tests: merge overwrite; split expands N items; dedupe keeps first occurrence.

### Task 9: Validation tests

**Files:** Modify: `backend/tests/test_workflow_validation.py`

Tests: http_request without url → error; code without code → error; flow with http_request + set_fields validates clean when configured.

### Task 10: Frontend — NODE_DEFS + nodeTypes map

**Files:** Modify: `frontend/src/lib/workflowGraph.js`, `frontend/src/components/workflow/workflowNodeTypes.jsx`

Add 5 fallback defs (group 'data', icons 🌐 ⌨️ ✏️ ✂️ 🧹, required fields) + add the 5 types to the `workflowNodeTypes` map. Palette needs no change (Data category exists).

---

## Phase C — Expression picker

### Task 11: Insert-field popover in NodeParameterForm

**Objective:** template inputs (string/json) get an "insert field" button opening a popover listing upstream nodes (from the page's edges) and their output fields (`text`, `items[N].field`), inserting the expression at the cursor.

**Files:** Modify: `frontend/src/components/workflow/NodeParameterForm.jsx`

- New prop `upstreamFields` (list of `{nodeId, label, fields: [{path, label}]}`) computed in `WorkflowCanvasPage` from `edges` + last run outputs (or node defs).
- TemplateInput gains a `▤` button → popover (simple absolute-positioned div) → click inserts `{{inputs.<nodeId>.<path>}}` at cursor via ref.
- Only for props whose description/placeholder contains `{{`.

**Verify:** manual — open a db_query node's SQL field, insert `{{inputs.n1.items[0].text}}`, save, run, confirm substitution.

---

## E2E + Delivery

### Task 12: E2E items + data nodes (API-level)

**Files:** Create: `/tmp/e2e_items_nodes.py` (mirror `/tmp/e2e_data_nodes.py` pattern)

Scenarios:
1. trigger_manual → db_query (Demo Sales DB) → code (map rows: `output = [{"product": i["name"], "half": i["price"]/2} for i in (input_items if False else [])]` — use `input` as the FIRST item; simpler: transform each item via a loop over `input` — see Task 7 contract: `input` = first upstream item. For row mapping use split_out first: db_query → split_out (field=items) → code (input=one row) → set_fields → output.
2. trigger_manual → http_request (local stub on a port) → llm → output; assert text contains stub payload.
3. Re-run the existing db_query/mcp_tool E2E (regression).

### Task 13: Deploy + verify

1. `docker compose -p zhanlu_sep_1_n8n restart backend`
2. `cd frontend && npm run build`
3. `docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_*.py tests/test_http_node.py tests/test_code_node.py tests/test_mcp_proxy.py -q'` → all PASS
4. Browser: open Sales DB Demo, run; build a quick items chain (db_query → split_out → code → output) on canvas, live-run, confirm per-node items.
5. Patch `zhanlu-workflow-canvas` skill with items model + 5 node types + sandbox notes.

---

## Verification checklist (final)

- [ ] 18 node types in registry (13 + 5)
- [ ] All tests green (workflow + http + code + mcp)
- [ ] Existing flows unchanged (back-compat suite passes)
- [ ] E2E items chain + http chain pass
- [ ] Frontend builds; palette shows 5 new nodes under Data
- [ ] Expression picker inserts working expressions
- [ ] Skill updated
