# Chat with Agent Workflows — Implementation Plan

> **For Hermes:** Spec: `docs/superpowers/specs/2026-09-01-workflow-chat-integration-design.md`

**Goal:** Let users pick an Agent Workflow from the main chatbot "+" menu, chat with it in a new reusable session (context-injected multi-turn).

**Architecture:** Backend chat path (agents.py v2/v3) branches on `agent_app.flow_mode` → `run_workflow_agent(trigger="chat", history=...)`. Runtime gains `history` param; `handle_trigger` emits a rendered transcript as `out["history"]` (resolvable via existing `{{inputs.<node>.history}}`). Frontend: dedicated "Agent Workflow" section in PlusMenu (trigger_chat workflows only), selecting one creates a new conversation bound via `agent_name` + `metadata.workflow_id`.

**Tech Stack:** FastAPI/SQLAlchemy (backend), React (frontend), pytest, vitest.

---

### Task 1: Runtime history plumbing

**Objective:** `run_workflow_agent` accepts `history` and stores it on the context.

**Files:**
- Modify: `backend/app/services/workflow_runtime.py` (`run_workflow_agent` signature ~line 418; `WorkflowExecutionContext.__init__` ~line 72)
- Test: `backend/tests/test_workflow_runtime.py`

**Step 1: Write failing test** — add to test_workflow_runtime.py (use existing fixtures/pattern in that file; read it first):
```python
def test_execution_context_stores_trigger_history(...):
    ctx = WorkflowExecutionContext(..., history=[{"role": "user", "content": "hi"}])
    assert ctx.trigger_history == [{"role": "user", "content": "hi"}]
```
(run_workflow_agent-level assert: history reaches ctx — assert via a workflow whose trigger handler echoes `ctx.trigger_history`; simpler: construct ctx directly if the test file has that pattern.)

**Step 2:** Run `pytest backend/tests/test_workflow_runtime.py -q` → FAIL (no history param).

**Step 3:** Implement: add `history: list | None = None` param to `run_workflow_agent`, pass `history=history` into `WorkflowExecutionContext`; in `__init__` add `self.trigger_history = history or []`.

**Step 4:** Run tests → PASS. **Step 5:** Commit.

### Task 2: Trigger emits history transcript

**Objective:** `handle_trigger` output gains `"history"` (last 10 turns, `User:/Assistant:` transcript) when `ctx.trigger_history` exists.

**Files:**
- Modify: `backend/app/services/workflow_nodes.py` (`handle_trigger` ~line 93)
- Test: `backend/tests/test_workflow_nodes.py`

**Step 1:** Test:
```python
def test_trigger_emits_history_transcript():
    ctx = FakeCtx()
    ctx.trigger_history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"}]
    out = handle_trigger(ctx, _node("n1", "trigger_chat", {}))
    assert "User: first" in out["history"]
    assert "Assistant: reply" in out["history"]
    assert out["text"] == "hello"

def test_trigger_without_history_has_no_history_key():
    ctx = FakeCtx()  # no trigger_history attr
    out = handle_trigger(ctx, _node("n1", "trigger_chat", {}))
    assert "history" not in out
```
(update FakeCtx in test_workflow_nodes.py to default `trigger_history = []`)

**Step 2:** Run → FAIL. **Step 3:** Implement transcript builder in handle_trigger. **Step 4:** PASS. **Step 5:** Commit.

### Task 3: trigger_chat registry notice

**Objective:** Document `{{inputs.<node>.history}}` in the registry so the settings form shows it.

**Files:** Modify: `backend/app/services/workflow_node_types.py` (trigger_chat properties ~line 110); Test: extend `backend/tests/test_workflow_nodes.py`.

Add `notice("Chat history (last 10 turns) is available as {{inputs.<node>.history}}.")` to trigger_chat properties. Test: trigger_chat type contains the notice text. Run workflow node tests → PASS. Commit.

### Task 4: Shared chat→workflow helper

**Objective:** One helper both v2/v3 call: resolve, validate trigger_chat, build history, run, return reply text/status.

**Files:**
- Create: `backend/app/services/workflow_chat.py`
- Test: `backend/tests/test_workflow_chat.py` (sqlite pattern from `tests/routers/test_workflows_router.py`)

```python
def run_workflow_for_chat(db, conv, agent_app, user_content, user) -> dict:
    """Returns {"content": str, "status": "completed"|"failed", "run_id": str|None}."""
```
- Validate flow has a `trigger_chat` node → else `{"content": "This workflow has no chat trigger.", "status": "failed"}`.
- Build history from `conv.messages` (list of {role, content}; last 10; exclude current).
- `run_workflow_agent(db, agent_app.org_id, agent_app.app_id, agent_app, user_content, created_by_id=user.id, trigger="chat", history=history)`.
- Return `{"content": result["output"] if completed else f"Workflow failed: {result['error']}", "status": ..., "run_id": ...}`.
- Tests: flow_mode agent with trigger_chat → completed + output; non-chat-trigger flow → failed w/ clear content; history passed (workflow = trigger_chat → code node `output = input` style; assert ctx history via monkeypatched `run_workflow_agent` capture — simplest: monkeypatch `workflow_chat.run_workflow_agent` to record history arg and return fixed output; also one real run with monkeypatched `_sync_text_completion`).

Commit.

### Task 5: v2 add_message flow_mode branch

**Objective:** `POST /agents/conversations/v2/{id}/messages` runs workflows instead of the agent loop.

**Files:** Modify: `backend/app/routers/agents.py` (~line 6311, right after `agent_app_id = ...`; before planning routing). Test: `backend/tests/routers/test_workflow_chat_integration.py` (new; sqlite pattern).

Insert:
```python
if agent_app and agent_app.flow_mode:
    from app.services.workflow_chat import run_workflow_for_chat
    wf_result = run_workflow_for_chat(db, conv, agent_app, user_content, user)
    # persist assistant message (role assistant, content=wf_result["content"])
    # append to conv.messages, db.commit, return conv dict (mirror the
    # standard add_message response shape — inspect the function's return)
```
Test: create AgentApp(flow_mode=True, flow=trigger_chat→code→output) + AgentConversation(agent_name=wf.name) in sqlite; call `agents.add_message(...)`; assert assistant message content = workflow output; assert a WorkflowRun row exists with trigger="chat". Also non-flow agent still runs the normal path (existing behavior; sanity assert via monkeypatched loop).

**IMPORTANT:** read the v2 function's response construction before inserting (it returns the conversation dict; mirror it). Commit.

### Task 6: v3 stream endpoint flow_mode branch

**Objective:** Streaming chat endpoint replies with the workflow output as a single event.

**Files:** Modify: `backend/app/routers/agents.py` (~line 10894, after `AgentApp.name == conv.agent_name` resolution, before the agent-loop stream start).

Insert flow_mode branch: call `run_workflow_for_chat`; yield the standard assistant message SSE event(s) the frontend consumes with the final content; also emit a `run_id`/status if the shape supports it. Mirror the v3 event format (read the endpoint's existing `yield` statements first).

Test: extend `backend/tests/routers/test_workflow_chat_integration.py` — call the stream endpoint with a flow_mode conv, collect SSE events, assert an assistant content event contains the workflow output. (If the endpoint is async/generator-based, drive it directly.) Commit.

### Task 7: PlusMenu workflow section

**Objective:** "+" menu gains "Agent Workflow" entry listing trigger_chat workflows.

**Files:** Modify: `frontend/src/components/chat/PlusMenu.jsx`; add `onSelectWorkflow` prop; fetch via `listWorkflows()` from `@/api/workflows`; filter `w.flow?.nodes?.some(n => n.type === 'trigger_chat')` (fallback: allow all if nodes missing). New icon (`Workflow` from lucide-react) + view 'workflow' with search (mirror the 'agent' view). Exclude `flow_mode` rows from the existing Agent view: filter `!a.flow_mode`.

Test: `frontend/src/components/chat/PlusMenu.workflow.test.jsx` (vitest + testing-library, mock listWorkflows) — workflow section renders only trigger_chat workflows; agent view excludes flow_mode; selecting calls onSelectWorkflow. Commit.

### Task 8: InvokePicker exclusion

**Objective:** Workflows don't appear as regular agents in the InvokePicker.

**Files:** Modify: `frontend/src/components/chat/InvokePicker.jsx` (line ~34 `setAgents(...)` → filter `!a.flow_mode`). Small change; commit with a one-line test if a test file exists, else skip test (manual verify).

### Task 9: Chat.jsx workflow selection → new session

**Objective:** Selecting a workflow creates a new conversation bound to it and opens it.

**Files:** Modify: `frontend/src/pages/Chat.jsx` (PlusMenu usage; find where onSelectAgent creates/rebinds conversations, mirror for workflow), `frontend/src/lib/ChatSessionContext.jsx` if needed.

Behavior: `onSelectWorkflow(wf)` → create new conversation (`createAgentConversation` with `{title: wf.name, agent_name: wf.name, metadata: {workflow_id: wf.id, mode: 'workflow'}}`) → select/open it (same flow as creating a new chat). Reopening a workflow session: nothing special — `conv.agent_name` resolves it; message send unchanged.

Test: `frontend/src/pages/Chat.workflowSession.test.jsx` — mock API; selecting workflow creates conv with agent_name + metadata.workflow_id and selects it. Commit.

### Task 10: End-to-end verification

1. Backend: `docker exec zhanlu-backend sh -c 'cd /app && python3 -m pytest tests/test_workflow_chat.py tests/routers/test_workflow_chat_integration.py tests/test_workflow_nodes.py tests/test_workflow_runtime.py tests/routers/test_workflows_router.py -q'` → all pass.
2. Frontend: `npx vitest run src/components/chat/PlusMenu.workflow.test.jsx src/pages/Chat.workflowSession.test.jsx` → pass. `npm run build` → clean.
3. `docker restart zhanlu-backend`; browser: create a trigger_chat workflow on the canvas → "+" → Agent Workflow → pick it → new session opens → send message → workflow output as reply → reload page → session persists → continue chat → history visible via `{{inputs.n1.history}}`.
