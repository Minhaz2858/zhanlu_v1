# Workflow Items Pipeline + Data Nodes — Design

Date: 2026-09-01
Status: Approved (design gate passed)
Scope: P0 slice of the n8n comparison roadmap

## 1. Problem

The workflow runtime is text-single-context: every node exchanges a single
`text` string. Database queries return rows that collapse into joined text;
HTTP/Code/transform operations have nowhere to live; there is no way to
process a list of things (items) through a workflow. n8n's core data model is
`items[]` — this design introduces it without breaking existing flows.

## 2. Approach

Add `items` as a first-class field on every node output, keeping `text` for
LLM consumption and backward compatibility. Every handler returns
`{text, items}`. Nodes that produce structured data (db_query, http, code,
kb_search, transform nodes) emit items; consuming nodes (llm, agent) keep
working on text but may reference item fields via expressions.

Three phases:

- Phase A: runtime items propagation (executor + all handlers carry items)
- Phase B: data nodes — HTTP Request, Code (Python sandbox), Set/Edit Fields,
  Split Out, Remove Duplicates
- Phase C: expression picker UI (insert-field popover in template inputs)

## 3. Data model

- Item = plain JSON-serializable dict (matches WorkflowRunStep.output_json).
- Node output = `{"text": str, "items": [dict, ...]}`.
- `ctx.current_upstream[node_id] = {"text": ..., "items": [...]}`.
- Defaults per node type (what each node emits):
  - trigger_* → 1 item `{"text": <input>}`
  - llm / agent → 1 item `{"text": <response>}`
  - db_query → N items (rows); text = JSON-lines (existing behavior)
  - kb_search → N items (docs: text/source/score)
  - http_request → parsed JSON: array → N items, object → 1 item,
    otherwise 1 item `{"body": <raw text>, "status": <code>}`
  - code → whatever the script returns (dict → 1 item, list[dict] → N items)
  - set_fields → N items (merged fields)
  - split_out → N items (one per element of the chosen array field)
  - remove_duplicates → N items (deduped by key)
  - output → renders items as a table when present, else text

Backward compatibility: `text` is always present; every old workflow keeps
running unchanged. No migration of stored flows required.

## 4. Components

### 4.1 Runtime (`backend/app/services/workflow_runtime.py`, `workflow_nodes.py`)

- `ExecutionContext.current_upstream` already carries dicts; extend handlers
  to emit `items`.
- Item helper: `first_item(ctx, node_id)` → upstream item dict (default
  `{"text": upstream["text"]}`); `all_items(ctx, node_id)` → list.
- Expression rendering: `_render()` already resolves `{{inputs.<n>.<field>}}`
  against upstream dicts; extend to resolve dotted paths into item fields
  (`{{inputs.n2.items[0].name}}`). New small resolver: parse `items[i].path`.

### 4.2 HTTP Request node (`http_request`)

Registry (workflow_node_types.py), category Data:
- method: options GET/POST/PUT/PATCH/DELETE (default GET)
- url: string template, required
- headers: collection [{key, value}] (template values)
- body: json template (string), used for non-GET
- auth_type: options none|bearer; bearer_token: string template
- timeout: number, default 15
- response_format: options auto|json|text (default auto)

Handler (`handle_http_request`): renders templates → `ctx.http_request(...)`
→ returns `{text, items, status}`. Non-2xx → `{success: false, text}` (does
not abort the run; downstream sees the error text).

`ctx.http_request` (runtime): uses `httpx` (existing dependency), sync call in
the worker thread already used by the executor. Parse JSON with a cap on
response size (1 MB). Bearer auth adds `Authorization: Bearer <token>`.

### 4.3 Code node (`code`)

Registry, category Data:
- language: options python (only; JS deferred — backend is Python)
- code: string template (long text)
- timeout: number, default 10

Handler (`handle_code`): renders code template → `ctx.run_code(code, item)` →
result becomes items.

`ctx.run_code` (runtime): executes in a `threading.Thread` with timeout.
Restricted globals: `{}` (no `__builtins__` import machinery — provide only
`json`, `math`, `len`, `str`, `int`, `float`, `bool`, `list`, `dict` helpers
via a small allowlist), `input` = the current upstream item (dict).
`exec(code, restricted_globals)`; result read from `output` variable the
script must assign. Output must be JSON-serializable (json.dumps check).
Exceptions → `{"success": false, "text": "Code error: ..."}`; timeout → error
text. No filesystem, no network, no imports.

### 4.4 Transform nodes (registry, category Data)

- `set_fields`: fields: collection [{name, value_template}] → merged onto
  each item (shallow copy).
- `split_out`: field: options (dynamic list from first item's array fields —
  MVP: string input with expression default), item_field: string (name for
  the split element, default "item") → expands array field into items.
- `remove_duplicates`: field: string (item key) → dedupe by `item[field]`.

### 4.5 Expression picker (Phase C, `NodeParameterForm.jsx`)

Template inputs (string/json with {{}}) get an "insert field" button →
popover listing upstream nodes (from edges) and their output fields
(text, items[N].field) → click inserts the expression at the cursor.
No Monaco, no drag-to-map in this phase.

## 5. Error handling

- HTTP: non-2xx is data, not a crash — returned as `{success:false, text}`.
- Code: timeout (default 10s) and exception both produce error text in the
  output; run continues.
- Both are capped (response size 1 MB; code output serializable).
- No per-node retry / continue-on-fail in this phase (P1 item).

## 6. Testing

Unit (backend/tests):
- items propagation: db_query handler returns items=rows; llm passes through;
  backward-compat: old text-only outputs still render.
- expression resolver: `{{inputs.n2.items[0].name}}` resolves from items.
- http_request handler: fake ctx; template rendering; auth header set.
- ctx.http_request: against `httpbin`-style local stub (tests use a local
  stdlib http.server in a thread) — GET/POST, JSON parse, non-2xx path.
- code node: happy path (assign output), json-serializability failure, bad
  code (exception), timeout (infinite loop + short timeout), no-imports
  enforcement.
- transform nodes: set_fields merge, split_out expansion, dedupe by key.
- validation: http_request requires url; code requires code.

E2E (API-level, like the existing node E2E):
- trigger_manual → db_query → code (map rows) → output; assert items flow.
- trigger_manual → http_request → llm → output against a local stub server
  (the same stdlib server as the unit test, exposed on a port).
- Existing E2E flows (db_query, mcp_tool, branch routing) still pass.

Frontend: no automated tests for the picker in this phase (manual verify).

## 7. Scope boundaries (explicit non-goals)

- NO JS code node (needs Node sandbox; deferred)
- NO OAuth / credential vault for HTTP (P1)
- NO per-node retry / error workflows (P1)
- NO drag-to-map, NO Monaco autocomplete (post-P0)
- NO Merge / Loop / Wait / human-in-the-loop (P1/P2)
- NO changes to stored flow schema (backward compatible)

## 8. Decisions (made, per user style)

- Python-only code node MVP
- HTTP MVP = plain requests + bearer token, no connection reuse
- 3 transform nodes (Set/Split/Dedupe), no Merge/Loop
- Expression picker over drag-to-map
- Phase A+B together, C after
