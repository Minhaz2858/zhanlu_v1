# Workflow Builder Chat Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a chat icon in the workflow canvas so users can chat with a `workflow_builder` AI agent that creates a draft workflow graph on the current workflow, while manual editing stays available.

**Architecture:** Reuse the agent_builder pattern: intent routing (`_SYSTEM_AGENT_INTENT_TABLE`), a `:::decision-summary` fenced block emitted by the agent, backend parse → validate → resolve names → save as draft, and a frontend slide-over chat panel that reloads the canvas on `flow_saved`.

**Tech Stack:** Python/FastAPI (backend), React/JSX + xyflow (frontend), pytest (backend tests), source-contract test convention (frontend).

**Spec:** `docs/superpowers/specs/2026-09-01-workflow-builder-chat-design.md`

---

## Task 1: Add `workflow_builder` system agent config

**Objective:** Register the new system agent so `ensure_system_agents()` seeds it on startup.

**Files:**
- Modify: `backend/app/services/system_agents.py:168-211` (add a config block after `agent_builder`'s block, before `skill_agent`)
- Test: `backend/tests/test_system_agents.py` (create if missing; check existing pattern first)

**Step 1: Write failing test**

Check existing test file first:
```bash
ls backend/tests/ | grep -i system
```
If `test_system_agents.py` exists, add a test asserting `workflow_builder` is in the configs; otherwise create it:
```python
from app.services.system_agents import _build_system_agent_configs

def test_workflow_builder_config_present():
    names = [c["name"] for c in _build_system_agent_configs()]
    assert "workflow_builder" in names
    cfg = next(c for c in _build_system_agent_configs() if c["name"] == "workflow_builder")
    assert cfg["is_system"] is True
    assert cfg["flow_mode"] is False or "flow_mode" not in cfg  # must be a REGULAR agent
```

**Step 2: Run test to verify failure**

Run: `cd backend && python -m pytest tests/test_system_agents.py -q -p no:cacheprovider`
Expected: FAIL — `workflow_builder` not in names

**Step 3: Write the config block**

Insert into `_build_system_agent_configs` after the `agent_builder` block (ends line ~211):
```python
        {
            "name": "workflow_builder",
            "description": "Designs AI agent workflows from plain-English descriptions",
            "project": "global",
            "capabilities": ["workflow_creation", "workflow_design"],
            "model": "gpt-4o-mini",
            "agent_type": "sequential",
            "topology": "standalone",
            "status": "active",
            "tool_config": {
                "enabled_tools": _tools_in_registry(registry, [
                    "list_tools", "create_artifact",
                ]),
            },
            **_BASE_HARNESS,
            "manifest_json": {
                "agent_name": "workflow_builder",
                "version": "1.0.0",
                "mission": "Design AI agent workflow graphs from plain-English descriptions and emit them as decision-summary blocks",
                "task_scope": ["workflow_creation", "workflow_design"],
                "boundaries": {
                    "allowed": ["workflow_design"],
                    "forbidden": ["delete_workflow", "publish_workflow", "access_user_data"],
                },
                "risk_tier": "medium",
                "created_by": "system",
            },
            "policy_profile": {
                "risk_tier": "medium",
                "requires_confirmation": False,
                "max_concurrent_calls": 3,
                "rate_limit_per_minute": 30,
                "allowed_domains": [],
                "retention_days": 30,
            },
        },
```

**Step 4: Run test to verify pass**

Run: `cd backend && python -m pytest tests/test_system_agents.py -q -p no:cacheprovider`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/app/services/system_agents.py backend/tests/test_system_agents.py
git commit -m "feat: register workflow_builder system agent"
```

---

## Task 2: Add `workflow_builder` system prompt

**Objective:** Give the agent its mission + node-type registry so it emits valid `:::decision-summary` blocks.

**Files:**
- Modify: `backend/app/services/agent_prompts.py` (add a `workflow_builder` prompt section; follow the `agent_builder` prompt structure at line ~937 area and the `_get_system_prompt` dispatch at ~2106)
- Test: extend the same test file from Task 1 (assert prompt exists / contains node registry mention)

**Step 1: Write failing test**

In `backend/tests/test_system_agents.py` (or the prompts test file if one exists — check `ls backend/tests | grep prompt`):
```python
from app.services.agent_prompts import get_system_prompt

def test_workflow_builder_prompt_has_registry():
    p = get_system_prompt("workflow_builder")
    assert "decision-summary" in p
    assert "trigger_chat" in p
```

**Step 2: Run test to verify failure**

Expected: FAIL — prompt not found / falls back to default

**Step 3: Add the prompt**

Find where system prompts dispatch by agent name (around `agent_prompts.py:2106` — `if prompt is None and agent_name == "agent_builder"`). Add a `workflow_builder` branch that builds a prompt containing:

- Mission: design AI agent workflow graphs from plain-English requests
- Rules:
  1. Emit EXACTLY ONE `:::decision-summary` fenced block in your final message, containing JSON: `{"action": "save_flow", "name": "<workflow title>", "description": "<short>", "flow": {"version": 1, "nodes": [...], "edges": [...]}}`
  2. Use ONLY node types from the registry list embedded below
  3. Every workflow needs exactly one `trigger_chat` node; connect main ports: `sourceHandle: "source-main"`, `targetHandle: "target-main"`
  4. Node shape: `{"id": "n1", "type": "<node_type>", "position": {"x": <num>, "y": <num>}, "data": {"label": "<display name>", "config": {<required config fields>}}}`
  5. Config values that reference entities (database connector, agent, tool, knowledge base) may use their display NAME — the system resolves names to ids
  6. String fields may use template syntax `{{inputs.<nodeId>.<field>}}`
  7. Keep workflows minimal (3-6 nodes)
- Registry context: embed a compact list from `list_node_types()` — name, displayName, required config fields per type
- Non-goals: never publish, never delete, never edit other workflows, always full replacement graph for the current workflow

**Step 4: Run test to verify pass**

Expected: PASS

**Step 5: Commit**

```bash
git add backend/app/services/agent_prompts.py backend/tests/test_system_agents.py
git commit -m "feat: workflow_builder system prompt with node registry"
```

---

## Task 3: Add intent routing for workflow creation

**Objective:** Chat messages like "create a workflow that ..." route to `workflow_builder`.

**Files:**
- Modify: `backend/app/routers/agents.py:2660-2685` (add `_WORKFLOW_CREATION_HEADER_RE` + table row)
- Test: `backend/tests/routers/test_workflows_router.py` or new `backend/tests/test_workflow_builder_intent.py`

**Step 1: Write failing test**

Create `backend/tests/test_workflow_builder_intent.py`:
```python
from app.routers.agents import _detect_system_agent_intent

def test_workflow_creation_intent_en():
    assert _detect_system_agent_intent("create a workflow that checks open orders") == ("workflow_creation", "workflow_builder")

def test_workflow_creation_intent_zh():
    assert _detect_system_agent_intent("帮我做一个工作流，每天汇总销售") == ("workflow_creation", "workflow_builder")

def test_agent_creation_still_routes_to_agent_builder():
    assert _detect_system_agent_intent("create a new agent for support") == ("agent_creation", "agent_builder")
```

**Step 2: Run test to verify failure**

Expected: FAIL — returns None for workflow phrases

**Step 3: Add header regex + table row**

After `_SKILL_CREATION_HEADER_RE` definition (~line 2657), add:
```python
# Header anchors for WORKFLOW creation (rebind target: workflow_builder).
# Must NOT collide with agent/skill headers — the noun is "workflow" /
# "工作流" / "智能体工作流".
_WORKFLOW_CREATION_HEADER_RE = re.compile(
    r"(?imx)"
    r"("
    r"create\s+(?:a\s+|an\s+|new\s+|me\s+|the\s+)*(?:agent\s+)?workflow"
    r"|build\s+(?:me\s+|a\s+|an\s+|new\s+|the\s+)*(?:agent\s+)?workflow"
    r"|make\s+(?:me\s+|a\s+|an\s+|new\s+)*(?:agent\s+)?workflow"
    r"|design\s+(?:a\s+|an\s+|new\s+|the\s+)*(?:agent\s+)?workflow"
    r"|new\s+workflow\b"
    r"|创建\s*(?:[一]?个?\s*)?(?:智能体\s*)?工作流"
    r"|新建\s*(?:[一]?个?\s*)?(?:智能体\s*)?工作流"
    r"|做一个?\s*(?:智能体\s*)?工作流"
    r"|帮我设计\s*(?:[一]?个?\s*)?(?:智能体\s*)?工作流"
    r")"
)
```
And in `_SYSTEM_AGENT_INTENT_TABLE` (line ~2666), add BEFORE `agent_creation` (workflow is more specific):
```python
    (
        "workflow_creation",
        "workflow_builder",
        _WORKFLOW_CREATION_HEADER_RE,
        None,
    ),
```

**Step 4: Run tests to verify pass**

Run: `cd backend && python -m pytest tests/test_workflow_builder_intent.py tests/routers/test_workflows_router.py -q -p no:cacheprovider`
Expected: PASS (both files)

**Step 5: Commit**

```bash
git add backend/app/routers/agents.py backend/tests/test_workflow_builder_intent.py
git commit -m "feat: route workflow-creation intents to workflow_builder"
```

---

## Task 4: Backend decision-summary persistence branch (save_flow)

**Objective:** When `workflow_builder` emits `:::decision-summary {action:"save_flow", ...}`, validate → resolve names → save draft → emit `flow_saved`.

**Files:**
- Modify: `backend/app/routers/agents.py` (near the agent_builder persistence path ~7361 and ~7758; add a `workflow_builder` branch in the decision-summary handling)
- Modify: `backend/app/services/workflow_runtime.py` (expose/reuse `_resolve_entity` — check current visibility; if private, add a small public helper `resolve_entity_references(flow, db) -> list[str]` that walks nodes and resolves kb_id/agent_id/tool_id/server_id by name)
- Test: `backend/tests/test_workflow_builder_persist.py`

**Step 1: Write failing test**

Create `backend/tests/test_workflow_builder_persist.py`. Use the router-direct pattern from `tests/routers/test_workflows_router.py` (StaticPool SQLite, SimpleNamespace user). Test the new helper `save_flow_from_decision_summary(payload, workflow_id, db, user)`:

```python
def test_save_flow_valid_persists_draft(db, monkeypatch):
    payload = {
        "action": "save_flow",
        "name": "Order Summary",
        "description": "Queries open orders",
        "flow": {
            "version": 1,
            "nodes": [
                {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat Trigger", "config": {}}},
                {"id": "n2", "type": "output", "position": {"x": 200, "y": 0}, "data": {"label": "Output", "config": {}}},
            ],
            "edges": [{"id": "e1", "source": "n1", "sourceHandle": "source-main", "target": "n2", "targetHandle": "target-main"}],
        },
    }
    saved = save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert saved["status"] == "flow_saved"
    assert saved["flow"]["nodes"][0]["type"] == "trigger_chat"

def test_save_flow_invalid_returns_errors(db):
    payload = {"action": "save_flow", "name": "Bad", "flow": {"version": 1, "nodes": [], "edges": []}}  # no trigger
    out = save_flow_from_decision_summary(payload, "wf-1", db, _user())
    assert out["status"] == "flow_invalid"
    assert any("trigger" in e.lower() for e in out["errors"])
```

**Step 2: Run test to verify failure**

Expected: FAIL — `save_flow_from_decision_summary` not defined

**Step 3: Implement the helper**

In `backend/app/services/workflow_runtime.py` add:
```python
def save_flow_from_decision_summary(payload: dict, workflow_id: str, db, user) -> dict:
    """Validate + persist a workflow graph emitted by workflow_builder.

    Returns {"status": "flow_saved", "flow": ...} or
    {"status": "flow_invalid", "errors": [...]}.
    """
    from app.routers.workflows import save_workflow, SaveBody
    from app.services.workflow_validation import validate_flow

    name = (payload.get("name") or "Untitled Workflow").strip()
    description = (payload.get("description") or "").strip()
    flow = payload.get("flow")
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), list):
        return {"status": "flow_invalid", "errors": ["flow.nodes must be a list"]}

    errors = validate_flow(flow)
    if errors:
        return {"status": "flow_invalid", "errors": errors}

    # Resolve entity names -> ids (kb_id/agent_id/tool_id/server_id)
    resolve_entity_references(flow, db)

    save_workflow(workflow_id, SaveBody(name=name, description=description, flow=flow), "default-app", db=db, user=user)
    return {"status": "flow_saved", "flow": flow}
```
Also add `resolve_entity_references(flow, db)` (id-first, unique-name fallback, raise on ambiguity — reuse the pattern at `workflow_runtime._resolve_entity` if present, else implement a small loop over node config keys `kb_id`/`agent_id`/`tool_id`/`server_id`).

**Step 4: Wire into agents.py decision-summary path**

Find the agent_builder decision-summary branch (the one handling `awaiting_decision_summary`, ~line 7758) and add a sibling branch: when the conversation's bound agent is `workflow_builder` and the parsed payload has `action == "save_flow"`:
```python
result = save_flow_from_decision_summary(payload, workflow_id, db, user)
if result["status"] == "flow_saved":
    # persist a plain assistant message (stripped) + set a status field the
    # stream can emit: conv.metadata_["workflow_flow_saved"] = True
else:
    # repair loop: append errors to assistant text, do NOT persist graph
```
The `workflow_id` comes from `conv.metadata_.get("workflow_id")` — the canvas panel sets it. If missing, return `flow_save_failed` status with a message.

**Step 5: Run tests to verify pass**

Run: `cd backend && python -m pytest tests/test_workflow_builder_persist.py tests/routers/test_workflow_chat_integration.py -q -p no:cacheprovider`
Expected: PASS

**Step 6: Commit**

```bash
git add backend/app/services/workflow_runtime.py backend/app/routers/agents.py backend/tests/test_workflow_builder_persist.py
git commit -m "feat: persist workflow_builder decision-summary as draft"
```

---

## Task 5: Ensure `metadata.workflow_id` flows through the v3 stream

**Objective:** The canvas chat panel binds the conversation to `workflow_builder` and carries `metadata.workflow_id` so the persistence branch knows which workflow to save.

**Files:**
- Modify: `backend/app/routers/agents.py` — check the v3 stream body handling (`add_message_stream` ~10918 area): the frontend already can pass `metadata`? Verify by reading the body model. If not, add `metadata: dict | None = None` to the v3 request body and merge into conv metadata on message add (mirror how the frontend passes `metadata.workflow_id` in the Chat page's `createAgentConversation`).
- Test: extend `backend/tests/test_workflow_builder_persist.py` with a body-shape test OR verify existing v3 tests cover metadata passthrough.

**Step 1: Read the v3 body model**

Find the v3 endpoint body model (`add_message_stream`) and check whether `metadata` is accepted. Note the exact field name in the plan's code comments.

**Step 2: Write failing test (if metadata not accepted)**

```python
def test_v3_accepts_metadata():
    # POST /agents/conversations/v3/<id>/messages/stream body with metadata.workflow_id
    # assert 200 / metadata persisted
```

**Step 3: Implement metadata passthrough** (only if missing)

**Step 4: Run tests + commit**

---

## Task 6: Frontend — chat button in canvas header

**Objective:** `WorkflowCanvasPage` shows a chat icon that opens the builder panel.

**Files:**
- Modify: `frontend/src/pages/WorkflowCanvasPage.jsx`
- Test: `frontend/src/pages/WorkflowCanvasPage.builder.test.jsx` (source-contract, per PlusMenu convention)

**Step 1: Write failing source-contract test**

```jsx
const SOURCE = fs.readFileSync(path.join(__dirname, 'WorkflowCanvasPage.jsx'), 'utf8');

it('has a chat button and builder panel wired', () => {
  expect(SOURCE).toMatch(/WorkflowBuilderChat/);
  expect(SOURCE).toMatch(/MessageSquare/);
  expect(SOURCE).toMatch(/onFlowSaved/);
  expect(SOURCE).toMatch(/builderOpen/);
});
```

**Step 2: Run test to verify failure**

Expected: FAIL

**Step 3: Implement**

- Import `MessageSquare` from lucide-react and `WorkflowBuilderChat`.
- Add `const [builderOpen, setBuilderOpen] = useState(false);`
- Add a reload helper (extract the existing load effect body into `reloadWorkflow()` so it can be called on `flow_saved`):
```jsx
const reloadWorkflow = useCallback(() => {
  getWorkflow(id).then((row) => {
    setWf(row);
    setName(row.name || 'Untitled Workflow');
    const loaded = row.flow || { version: 1, nodes: [], edges: [] };
    setSettings((loaded.settings) || {});
    const xy = flowToXY(loaded);
    setXyNodes(xy.nodes);
    setXyEdges(xy.edges);
    setDirty(false);
  }).catch(() => toast.error('Failed to reload workflow'));
}, [id]);
```
- Render the button in the header toolbar (next to Save/Run) and the panel:
```jsx
<Button variant="outline" size="sm" onClick={() => setBuilderOpen(true)} title="Build with AI">
  <MessageSquare className="h-3.5 w-3.5" /> {lang === 'en' ? 'Build with AI' : 'AI 构建'}
</Button>
...
<WorkflowBuilderChat workflowId={id} open={builderOpen} onClose={() => setBuilderOpen(false)} onFlowSaved={() => { setBuilderOpen(false); reloadWorkflow(); }} />
```

**Step 4: Run test to verify pass**

Run: `cd frontend && npx vitest run src/pages/WorkflowCanvasPage.builder.test.jsx`
Expected: PASS

**Step 5: Commit**

```bash
git add frontend/src/pages/WorkflowCanvasPage.jsx frontend/src/pages/WorkflowCanvasPage.builder.test.jsx
git commit -m "feat: AI build button in workflow canvas header"
```

---

## Task 7: Frontend — WorkflowBuilderChat slide-over panel

**Objective:** Chat panel that streams with `workflow_builder`, closes on `flow_saved`.

**Files:**
- Create: `frontend/src/components/workflow/WorkflowBuilderChat.jsx`
- Modify: `frontend/src/api/workflows.js` (add `startBuilderConversation()` if needed, or reuse `streamAgentResponse` from agentEnhanced.js)
- Test: `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx`

**Step 1: Write failing source-contract test**

```jsx
const SOURCE = fs.readFileSync(path.join(__dirname, 'WorkflowBuilderChat.jsx'), 'utf8');

it('streams to workflow_builder with workflow_id metadata', () => {
  expect(SOURCE).toMatch(/workflow_builder/);
  expect(SOURCE).toMatch(/workflow_id/);
  expect(SOURCE).toMatch(/streamAgentResponse/);
  expect(SOURCE).toMatch(/onFlowSaved/);
});
```

**Step 2: Run test to verify failure**

Expected: FAIL — component missing

**Step 3: Implement the component**

```jsx
import { useRef, useState, useEffect } from 'react';
import { useLanguage } from '@/lib/LanguageProvider';
import { streamAgentResponse } from '@/api/agentEnhanced';
import { Button } from '@/components/ui/button';
import { Send, X, Loader2, Sparkles } from 'lucide-react';

export default function WorkflowBuilderChat({ workflowId, open, onClose, onFlowSaved }) {
  const { lang } = useLanguage();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [convId, setConvId] = useState(null);
  const abortRef = useRef(null);

  // create conversation on first open — mirror createAgentConversation in Chat.jsx
  // (use base44.entities.AgentConversation.create with agent_name 'workflow_builder'
  //  and metadata {workflow_id})
  // then streamAgentResponse(convId, {role:'user', content}, signal)
  // on events: token → append text; 'done' → stop; if metadata indicates
  // flow_saved (or conv.metadata_?.workflow_flow_saved) → onFlowSaved()
}
```

Panel: fixed right slide-over `w-[380px]` with message list (auto-scroll), input row, close button. Streaming state disables send. Abort on close.

**Step 4: Run tests + build**

Run: `cd frontend && npx vitest run src/components/workflow/WorkflowBuilderChat.test.jsx && npm run build`
Expected: PASS + build OK

**Step 5: Commit**

```bash
git add frontend/src/components/workflow/WorkflowBuilderChat.jsx frontend/src/api/workflows.js frontend/src/components/workflow/WorkflowBuilderChat.test.jsx
git commit -m "feat: workflow builder chat slide-over panel"
```

---

## Task 8: End-to-end verification in browser

**Objective:** Prove the full loop: open canvas → chat → agent emits graph → canvas reloads.

**Steps:**

1. Backend up, frontend rebuilt. Login as admin.
2. Open `http://localhost:8088/my-space/workflow/edceaf0f-29ab-42c5-8f3f-3b61b3f22c61` (CEO ERP Command Center).
3. Click the "Build with AI" chat icon.
4. Type: "create a workflow that queries open orders and outputs a summary".
5. Verify: agent streams a response; on `flow_saved` the panel closes; canvas shows the new draft nodes/edges; status shows Draft (not Published).
6. Manually add a node via palette → still works (manual path intact).
7. Check the workflow was saved as draft: `GET /api/apps/local-zhanlu-app/workflows/<id>` → `status == "draft"` (if previously published, note publish is untouched — the save is a draft-save).

**Verify pitfalls:**
- workflow_builder must NOT be flow_mode (verify in DB: `SELECT name, flow_mode, is_system FROM agent_apps WHERE name='workflow_builder'`)
- The v3 flow_mode branch must NOT capture workflow_builder (it's a regular agent)
- Chunk-hash verification after rebuild (pitfall 13): fetch the built JS chunk and grep for a string literal like "Build with AI"

---

## Task 9: Update the workflow-designer skill

**Objective:** Document the new capability + any pitfalls hit.

**Files:**
- Modify: `~/.hermes/skills/zhanlu/zhanlu-workflow-designer/SKILL.md` (add workflow_builder to the architecture list and a new pitfall if any was hit)

**Steps:** Patch the skill with the workflow_builder pattern (intent row, decision-summary save_flow branch, metadata.workflow_id, canvas chat panel). Commit not needed (skill lives outside repo).

---

## Review checklist

- [ ] Tasks sequential, bite-sized
- [ ] Exact file paths throughout
- [ ] Backend tests follow router-direct pattern (SimpleNamespace user, StaticPool SQLite)
- [ ] Frontend tests follow source-contract convention
- [ ] workflow_builder is a REGULAR agent (flow_mode=false) — never hits the workflow runtime branch
- [ ] Draft-only persistence — never auto-publish
- [ ] Repair loop for invalid graphs (validate_flow errors feed back)
