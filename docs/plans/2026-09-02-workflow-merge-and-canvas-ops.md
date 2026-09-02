# Workflow P0 Batch: Merge Node + Canvas Multi-Select & Copy/Paste — Implementation Plan

> **For Hermes:** Execute task-by-task with TDD. Direct execution (no subagent dispatch) — the stack is RAM-tight and targeted pytest runs are required.

**Goal:** Close the two highest-value n8n gaps for Zhanlu workflows: (A) a **Merge** data node that appends items from multiple parallel branches into one list, and (B) the remaining n8n canvas ops — **multi-select** and **copy/paste** (undo/redo + keyboard shortcuts already shipped in WorkflowCanvas.jsx).

**Architecture:** Merge is registry-driven like every other node: one entry in `NODE_TYPES`, one handler in `workflow_nodes.py` that iterates `ctx.current_upstream` (edge-ordered) and concatenates `items`. No executor changes — the executor already supports multiple MAIN edges per target and builds `current_upstream` in edge order. Canvas ops: page-owned selection state (`selectedIds`) synced from ReactFlow `onSelectionChange`; pure paste helper in `workflowGraph.js` (id remap + `{{inputs.*}}` expression remap + position offset) so it's unit-testable without rendering.

**Tech Stack:** Python/FastAPI (backend), React + @xyflow/react (frontend), vitest (frontend unit), pytest (backend).

**Deferred (NOT in this batch):** Expression engine v2 (`$node`/`$json`/date utils) — lowest non-IT value, needs a concrete n8n-functions port list before it earns build time. Propose when the Merge node proves its convergence value.

---

## Part A — Merge Node (backend, TDD)

### Task A1: Write failing registry + handler tests for `merge`

**Objective:** Define the expected behavior of the Merge node before implementation.

**Files:**
- Modify: `backend/tests/test_workflow_nodes.py` (append after the transform-node tests, ~line 330)
- Modify: `backend/tests/test_workflow_executor.py` (append a graph-level test)
- Modify: `backend/tests/test_workflow_validation.py` (merge is valid with 0 or 2 inputs)

**Step 1: Append handler tests to `backend/tests/test_workflow_nodes.py`**

```python
# ---- merge ---------------------------------------------------------------

def test_merge_appends_items_in_edge_order():
    ctx = FakeCtx()
    ctx.current_upstream = {
        "n1": {"text": "A", "items": [{"id": 1}, {"id": 2}]},
        "n2": {"text": "B", "items": [{"id": 3}]},
    }
    node = _node("m1", "merge", {})
    out = handle_merge(ctx, node)
    assert out["items"] == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert "n1" in out["text"]  # _json_summary of first 3 items


def test_merge_falls_back_to_text_items():
    ctx = FakeCtx()
    ctx.current_upstream = {
        "n1": {"text": "hello"},
        "n2": {"text": "world", "items": [{"a": 1}]},
    }
    out = handle_merge(ctx, _node("m1", "merge", {}))
    assert out["items"] == [{"text": "hello"}, {"a": 1}]


def test_merge_single_upstream_is_passthrough():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "only", "items": [{"x": 1}]}}
    out = handle_merge(ctx, _node("m1", "merge", {}))
    assert out["items"] == [{"x": 1}]


def test_merge_no_upstream_returns_empty_items():
    ctx = FakeCtx()
    out = handle_merge(ctx, _node("m1", "merge", {}))
    assert out["items"] == []
```

**Step 2: Append a registry test** (same file or `test_workflow_validation.py`):

```python
def test_merge_in_registry():
    from app.services.workflow_node_types import NODE_TYPES
    t = NODE_TYPES["merge"]
    assert t["category"] == "Data"
    assert t["inputs"] == ["main", "main"]   # two input ports
    assert t["outputs"] == ["main"]
```

**Step 3: Append a graph-level executor test** to `backend/tests/test_workflow_executor.py` — two branches feed one merge, then an output; assert both branches ran (no `skipped`) and the merged items order. Follow the existing fake-handler conventions in that file (read it first; adapt the FakeCtx/handlers shape).

```python
def test_merge_combines_parallel_branches():
    # trigger(n1) -> A(n2 set_fields) -> merge(m1)
    # trigger(n1) -> B(n3 set_fields) -> merge(m1) -> output(n4)
    # Assert: status completed, n2/n3 both in ctx.outputs (not skipped),
    #         final output contains items from BOTH branches.
```

**Step 4: Run the new tests — expect FAIL** (ImportError: `handle_merge` undefined / KeyError `merge`):

```bash
docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_nodes.py tests/test_workflow_executor.py -q -k merge'
```

### Task A2: Register the `merge` node type

**Objective:** The node appears in the shared registry (palette, form, validation, builder prompt) with zero per-type UI.

**Files:**
- Modify: `backend/app/services/workflow_node_types.py` (insert before `"condition"` entry, ~line 390)

**Step 1: Add the registry entry** — notice-only (append is the single MVP mode; non-IT friendly, mirrors `trigger_chat`'s notice-only form):

```python
"merge": {
    "name": "merge",
    "displayName": "Merge",
    "category": "Data",
    "icon": "🔗",
    "color": "#f59e0b",
    "description": "Combine items from multiple inputs into one list",
    "description_zh": "将多个输入的数据项合并为一个列表",
    "inputs": ["main", "main"],
    "outputs": ["main"],
    "properties": [
        notice("Appends every item from ALL connected inputs, in order. Use it to combine parallel branches into one list before a final LLM or Output node. References upstream items as {{inputs.<node>.items}}."),
    ],
},
```

**Step 2: Update the registry-count assertion if one exists** — search tests for `len(NODE_TYPES)` / `== 18` and bump to 19. Verify:

```bash
docker exec zhanlu-backend sh -c 'cd /app && python3 -c "from app.services.workflow_node_types import NODE_TYPES; print(len(NODE_TYPES), NODE_TYPES[\"merge\"][\"inputs\"])"'
# expected: 19 ['main', 'main']
```

**Step 3: Run the merge tests — expect PASS** for registry test; handler tests still fail (no handler).

### Task A3: Implement `handle_merge`

**Objective:** Concatenate all active upstream items in edge order.

**Files:**
- Modify: `backend/app/services/workflow_nodes.py` (after `handle_remove_duplicates`, ~line 330)

**Step 1: Add the handler:**

```python
def handle_merge(ctx, node):
    """Append items from every active upstream input, in edge order (n8n Merge)."""
    merged: list[dict] = []
    for src in ctx.current_upstream:
        out = ctx.outputs.get(src) or {}
        merged.extend(all_items(ctx, src) or [])
    return {"text": _json_summary(merged), "items": merged}
```

Note: `ctx.current_upstream` is a plain dict built by the executor from the incoming MAIN edges in edge order — iteration order is deterministic. `all_items` already falls back to `[{"text": ...}]` for text-only outputs.

**Step 2: Register in the dispatch table** — add `"merge": handle_merge,` to `NODE_HANDLERS` (line ~427).

**Step 3: Run the merge tests — expect PASS:**

```bash
docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_nodes.py tests/test_workflow_executor.py -q -k merge'
```

### Task A4: Validation + builder-prompt wiring

**Objective:** `validate_flow` accepts merge (no required config), and the AI builder knows the node exists with its multi-input shape.

**Files:**
- Modify: `backend/app/services/workflow_validation.py` only if needed (merge has no required config — likely NO change; verify with the test)
- Modify: `backend/tests/test_workflow_validation.py`
- Modify: `backend/app/services/agent_prompts.py` (workflow_builder system prompt rule)
- Modify: `backend/tests/test_workflow_builder_prompts.py`

**Step 1: Validation test** — a flow `trigger_chat -> set_fields -> merge -> output` with the merge receiving TWO edges validates clean:

```python
def test_merge_flow_validates():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "in", "config": {}}},
            {"id": "n2", "type": "set_fields", "position": {"x": 320, "y": 0}, "data": {"label": "a", "config": {"fields": []}}},
            {"id": "n3", "type": "set_fields", "position": {"x": 320, "y": 200}, "data": {"label": "b", "config": {"fields": []}}},
            {"id": "m1", "type": "merge", "position": {"x": 640, "y": 100}, "data": {"label": "m", "config": {}}},
            {"id": "n4", "type": "output", "position": {"x": 960, "y": 100}, "data": {"label": "out", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e2", "source": "n1", "target": "n3", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e3", "source": "n2", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e4", "source": "n3", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main-1"},
            {"id": "e5", "source": "m1", "target": "n4", "sourceHandle": "source-main", "targetHandle": "target-main"},
        ],
    }
    assert validate_flow(flow) == []
```

**Step 2: Builder rule** — append to the workflow_builder system prompt rules (find the RULES block in `agent_prompts.py`): a short line teaching the builder to use `merge` to converge parallel branches:

```
- merge: appends items from ALL connected inputs in order — use it to combine parallel branches into one list before a final LLM or output node.
```

**Step 3: Builder-prompt test** — assert the rule text appears in `workflow_builder_system_prompt()` (follow the existing test style in `test_workflow_builder_prompts.py`).

**Step 4: Run the full workflow test sweep (targeted):**

```bash
docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_nodes.py tests/test_workflow_executor.py tests/test_workflow_validation.py tests/test_workflow_builder_prompts.py tests/test_workflow_builder_persist.py tests/test_workflow_chat.py -q'
```

**Step 5: Restart backend + commit:**

```bash
docker compose -p zhanlu_sep_1_n8n restart backend
git add backend/app/services/workflow_node_types.py backend/app/services/workflow_nodes.py backend/app/services/agent_prompts.py backend/tests/test_workflow_nodes.py backend/tests/test_workflow_executor.py backend/tests/test_workflow_validation.py backend/tests/test_workflow_builder_prompts.py
git commit -m "feat(workflow): Merge node appends items from parallel branches (n8n parity)"
```

### Task A5: Live API E2E — merge workflow runs end-to-end

**Objective:** Prove the node runs against the real stack (registry served, validation, async run, merged output in steps).

**Files:**
- Create: `backend/scripts/verify_merge_run.py` (follow the `verify_sales_assistant_run.py` pattern: login → create workflow via API → run-async → SSE drain → assert)

**Step 1:** Write the script: build the A4 flow JSON (trigger_chat → set_fields A → merge; trigger_chat → set_fields B → merge → output), create via `POST /api/apps/{app_id}/workflows`, run-async, drain SSE, assert:
- `workflow_run_steps` has `completed` for n2, n3, m1, n4 (NO skipped)
- the merge step's `output_json.items` contains BOTH branch items in edge order

**Step 2:** Run it:

```bash
docker exec zhanlu-backend sh -c 'cd /app && python3 scripts/verify_merge_run.py'
```

Expected: script prints merged item count + PASS.

**Step 3:** Commit the script:

```bash
git add backend/scripts/verify_merge_run.py
git commit -m "test(workflow): live E2E verifier for Merge node parallel-branch convergence"
```

---

## Part B — Canvas Ops: Multi-Select + Copy/Paste (frontend, TDD)

### Task B1: Pure paste helper in workflowGraph.js (failing tests first)

**Objective:** `buildPasteGraph(clipboard, offset)` remaps node/edge ids, offsets positions, and rewrites `{{inputs.<oldId>.` expression refs — pure and unit-testable.

**Files:**
- Modify: `frontend/src/lib/workflowGraph.js`
- Modify: `frontend/src/lib/__tests__/workflowGraph.test.js`

**Step 1: Write failing tests** (follow existing file's test style; `describe`/`it` or `test`):

```js
import { buildPasteGraph } from '@/lib/workflowGraph';

describe('buildPasteGraph', () => {
  it('remaps node and edge ids and offsets positions', () => {
    const clip = {
      nodes: [{ id: 'n_a', type: 'llm', position: { x: 100, y: 100 }, data: { label: 'L', config: {} } }],
      edges: [],
    };
    const { nodes, edges } = buildPasteGraph(clip, { x: 24, y: 24 });
    expect(nodes[0].id).not.toBe('n_a');
    expect(nodes[0].position).toEqual({ x: 124, y: 124 });
    expect(edges).toEqual([]);
  });

  it('keeps internal edges and rewrites their endpoints', () => {
    const clip = {
      nodes: [
        { id: 'n_a', type: 'llm', position: { x: 0, y: 0 }, data: { label: 'A', config: {} } },
        { id: 'n_b', type: 'output', position: { x: 300, y: 0 }, data: { label: 'B', config: {} } },
      ],
      edges: [{ id: 'e1', source: 'n_a', target: 'n_b', sourceHandle: 'source-main', targetHandle: 'target-main' }],
    };
    const { nodes, edges } = buildPasteGraph(clip);
    const idMap = Object.fromEntries(nodes.map((n, i) => [['n_a', 'n_b'][i], n.id]));
    expect(edges).toHaveLength(1);
    expect(edges[0].source).toBe(idMap.n_a);
    expect(edges[0].target).toBe(idMap.n_b);
  });

  it('rewrites {{inputs.<oldId>.}} references in pasted configs', () => {
    const clip = {
      nodes: [{
        id: 'n_a', type: 'llm', position: { x: 0, y: 0 },
        data: { label: 'A', config: { prompt: 'summarize {{inputs.n_a.text}} and {{inputs.n_zz.items[0].x}}' } },
      }],
      edges: [],
    };
    const { nodes } = buildPasteGraph(clip);
    const newId = nodes[0].id;
    expect(nodes[0].data.config.prompt).toContain(`{{inputs.${newId}.text}}`);
    expect(nodes[0].data.config.prompt).toContain('{{inputs.n_zz.items[0].x}}'); // non-copied refs untouched
  });
});
```

**Step 2: Run — expect FAIL:**

```bash
cd frontend && npx vitest run src/lib/__tests__/workflowGraph.test.js
```

**Step 3: Implement in `frontend/src/lib/workflowGraph.js`:**

```js
/**
 * Build a pasted graph from a clipboard snapshot: fresh ids, position offset,
 * internal edges remapped, and {{inputs.<oldId>.}} expression refs rewritten
 * to the pasted ids (references to nodes NOT in the clipboard stay as-is).
 */
export function buildPasteGraph(clipboard, offset = { x: 24, y: 24 }) {
  const nodes = clipboard.nodes || [];
  const edges = clipboard.edges || [];
  const idMap = {};
  const stamp = () => `${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
  const newNodes = nodes.map((n) => {
    const newId = `n_${stamp()}`;
    idMap[n.id] = newId;
    return {
      ...n,
      id: newId,
      position: { x: (n.position?.x || 0) + offset.x, y: (n.position?.y || 0) + offset.y },
      selected: false,
      data: remapExpressions(n.data, idMap),
    };
  });
  const newEdges = edges
    .filter((e) => idMap[e.source] && idMap[e.target])
    .map((e) => ({ ...e, id: `e_${stamp()}`, source: idMap[e.source], target: idMap[e.target], selected: false }));
  return { nodes: newNodes, edges: newEdges };
}

function remapExpressions(value, idMap) {
  if (typeof value === 'string') {
    return value.replace(/\{\{inputs\.([A-Za-z0-9_]+)\./g, (m, id) => (idMap[id] ? `{{inputs.${idMap[id]}.` : m));
  }
  if (Array.isArray(value)) return value.map((v) => remapExpressions(v, idMap));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, remapExpressions(v, idMap)]));
  }
  return value;
}
```

**Step 4: Run — expect PASS.** Commit:

```bash
git add frontend/src/lib/workflowGraph.js frontend/src/lib/__tests__/workflowGraph.test.js
git commit -m "feat(workflow): buildPasteGraph helper — id remap, position offset, expression rewrite"
```

### Task B2: Page + canvas multi-selection

**Objective:** The page tracks ALL selected node ids (not just one); NDV opens only on a plain single click; Delete removes every selected node + its edges.

**Files:**
- Modify: `frontend/src/pages/WorkflowCanvas.jsx`
- Modify: `frontend/src/components/workflow/WorkflowCanvas.jsx`

**Step 1 (canvas):** In `CanvasInner`, add `onSelectionChange` and gate NDV-opening clicks:

```jsx
onSelectionChange={({ nodes: sel }) => onSelectChange?.(sel.map((n) => n.id))}
onNodeClick={(e, node) => {
  // Plain click opens the NDV; modifier clicks are multi-select gestures.
  if (!(e.metaKey || e.ctrlKey || e.shiftKey)) onSelectNode?.(node.id);
}}
```

Prop-thread `onSelectChange` through the wrapper (default `() => {}`).

**Step 2 (page):**
- Add state `const [selectedIds, setSelectedIds] = useState([]);`
- Pass `onSelectChange={setSelectedIds}` to `<WorkflowCanvas … />`.
- Replace `deleteSelectedNode` with a multi-delete:

```jsx
function deleteSelectedNodes() {
  const ids = selectedIds.length ? selectedIds : (selectedNodeId ? [selectedNodeId] : []);
  if (!ids.length) return;
  const idSet = new Set(ids);
  setXyNodes((nds) => {
    const next = (nds || []).filter((n) => !idSet.has(n.id));
    commitStructural(nds || [], xyEdges, next, xyEdges);
    return next;
  });
  setXyEdges((eds) => {
    const next = (eds || []).filter((e) => !idSet.has(e.source) && !idSet.has(e.target));
    commitStructural(xyNodes, eds || [], xyNodes, next);
    return next;
  });
  setSelectedNodeId(null);
  setSelectedIds([]);
  setDirty(true);
}
```

- Update the keydown handler: `Delete/Backspace` → `deleteSelectedNodes()` (keep the typing guard); `Escape` clears both selections.

**Step 3:** `npm run build` — then browser-verify (Task B4).

### Task B3: Copy/paste wiring

**Objective:** ⌘/Ctrl+C copies selected nodes + internal edges; ⌘/Ctrl+V pastes via `buildPasteGraph` with expression remap; toolbar buttons for discoverability.

**Files:**
- Modify: `frontend/src/pages/WorkflowCanvas.jsx`

**Step 1:** Add clipboard ref + handlers:

```jsx
const clipboard = useRef(null);

function copySelection() {
  const ids = selectedIds.length ? selectedIds : (selectedNodeId ? [selectedNodeId] : []);
  if (!ids.length) return;
  const idSet = new Set(ids);
  clipboard.current = {
    nodes: (xyNodes || []).filter((n) => idSet.has(n.id)).map((n) => ({ ...n, selected: false })),
    edges: (xyEdges || []).filter((e) => idSet.has(e.source) && idSet.has(e.target)).map((e) => ({ ...e, selected: false })),
  };
}

function pasteClipboard() {
  if (!clipboard.current) return;
  const pasted = buildPasteGraph(clipboard.current);
  setXyNodes((nds) => {
    const next = [...(nds || []), ...pasted.nodes];
    commitStructural(nds || [], xyEdges, next, xyEdges);
    return next;
  });
  setXyEdges((eds) => {
    const next = [...(eds || []), ...pasted.edges];
    commitStructural(xyNodes, eds || [], xyNodes, next);
    return next;
  });
  setSelectedIds(pasted.nodes.map((n) => n.id));
  setDirty(true);
}
```

**Step 2:** Extend the keydown handler (after the Z/Y branch, before Delete):

```jsx
} else if (mod && e.key.toLowerCase() === 'c' && !typing) {
  e.preventDefault();
  copySelection();
} else if (mod && e.key.toLowerCase() === 'v' && !typing) {
  e.preventDefault();
  pasteClipboard();
}
```

**Step 3:** Toolbar buttons next to undo/redo (import `Copy`, `ClipboardPaste` from lucide-react):

```jsx
<Button size="sm" variant="ghost" onClick={copySelection} disabled={!selectedIds.length && !selectedNodeId} title="⌘C">
  <Copy className="h-3.5 w-3.5" />
</Button>
<Button size="sm" variant="ghost" onClick={pasteClipboard} disabled={!clipboard.current} title="⌘V">
  <ClipboardPaste className="h-3.5 w-3.5" />
</Button>
```

**Step 4:** `npm run build`.

### Task B4: Browser verification + commit

**Objective:** Prove multi-select, copy, paste, and delete behave on the live canvas (undo/redo regression included).

**Files:** none (manual browser verification)

**Step 1:** Ensure backend restarted (Task A4 Step 5), frontend rebuilt (`npm run build`, dist is bind-mounted).

**Step 2:** Open `http://localhost:8088` → My Space → Agent Workflow → open the CEO Command Center (or any multi-node workflow). Verify:
1. Shift-click two nodes → both show selection rings.
2. ⌘C then ⌘V → pasted copies appear offset +24px with NEW ids (check `xyToFlow` output via a Save — edge ids normalized, no nulls).
3. A pasted LLM node's config prompt referencing `{{inputs.nX.text}}` now references the pasted id (open NDV, inspect the field).
4. Delete with multi-selection removes all selected + their edges; ⌘Z restores them; ⇧⌘Z re-deletes.
5. No dirty-flag false positive on load; NDV does NOT open on shift-click.

**Step 3:** Commit:

```bash
git add frontend/src/pages/WorkflowCanvas.jsx frontend/src/components/workflow/WorkflowCanvas.jsx
git commit -m "feat(workflow): canvas multi-select + copy/paste with expression remap (n8n canvas ops)"
```

---

## Verification envelope (run at the end)

```bash
docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_nodes.py tests/test_workflow_executor.py tests/test_workflow_validation.py tests/test_workflow_builder_prompts.py -q'
cd frontend && npx vitest run src/lib/__tests__/workflowGraph.test.js && npm run build
```

Then the browser sweep (B4) + `backend/scripts/verify_merge_run.py`.

## Notes / decisions

- Merge MVP = append-only, notice-only form (non-IT friendly). Combine/join-by-key modes are future work (YAGNI now).
- `ctx.current_upstream` order = incoming MAIN edge order (executor builds it that way) — deterministic merge order, no executor change.
- Pasted expression refs to nodes NOT in the clipboard are left untouched (documented: re-wire manually).
- Expression engine v2 (P0c) deferred — see header.
