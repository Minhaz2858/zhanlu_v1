# Workflow Session Recognition — Implementation Plan

> **For Hermes:** Execute task-by-task with TDD. Direct execution (no subagent dispatch) — RAM-tight stack, targeted pytest runs.

**Goal:** Make workflow sessions recognizable per workflow. Both conversation paths ALREADY write `metadata.workflow_id` at creation (Chat.jsx `handlePickWorkflow` → `createAgentConversation(wf.name, {workflow_id: wf.id})`; WorkflowBuilderChat → `createAgentConversation('workflow_builder', {workflow_id})`). Nothing queries by it. This adds: (1) a `workflow_id` filter to `list_conversations`, (2) Build-with-AI session RESUME (reopen = same session + history), (3) a "New session" escape hatch in the panel.

**Architecture:** Backend — one JSON-path filter on the existing `metadata` JSON column (`cast(metadata_['workflow_id'], String) == id`), NO migration, cross-dialect (PG: `metadata -> 'workflow_id'` cast to text; SQLite tests: `json_extract`). Frontend — new `listAgentConversations(agentName, {workflowId, limit})` API wrapper; WorkflowBuilderChat's session-start effect becomes start-or-resume (latest conv for this workflow_id, else create).

**Deferred (NOT in this batch):** the canvas "Chat sessions" drawer listing all workflow conversations — the API now supports it; UI is a follow-up.

---

## Task 1: Backend `workflow_id` filter on list_conversations (TDD)

**Objective:** `GET /agents/conversations?agent_name=...&workflow_id=...` returns only conversations whose metadata.workflow_id matches.

**Files:**
- Modify: `backend/app/routers/agents.py` (list_conversations, ~line 6119)
- Modify: `backend/tests/test_workflow_builder_persist.py` (append tests)

**Step 1: Write failing tests** (same file conventions as `test_create_conversation_persists_workflow_id_metadata`: `db` fixture, `asyncio.run(agents.list_conversations(...))`):

```python
def test_list_conversations_filters_by_workflow_id(db):
    for wf in ("wf-1", "wf-2"):
        asyncio.run(agents.create_conversation(
            app_id="default-app",
            body={"agent_name": "workflow_builder", "title": "Builder",
                  "metadata": {"workflow_id": wf}},
            db=db, user=_user(),
        ))
    # Resume query: agent_name + workflow_id → ONLY wf-1
    rows = asyncio.run(agents.list_conversations(
        app_id="default-app", agent_name="workflow_builder", workflow_id="wf-1",
        db=db, user=_user(),
    ))
    assert len(rows) == 1
    assert rows[0]["metadata"]["workflow_id"] == "wf-1"
    # Backward compat: no workflow_id → both
    rows_all = asyncio.run(agents.list_conversations(
        app_id="default-app", agent_name="workflow_builder", db=db, user=_user(),
    ))
    assert len(rows_all) == 2


def test_list_conversations_workflow_id_filter_is_agent_scoped(db):
    """workflow_id alone must not leak other agents' conversations."""
    asyncio.run(agents.create_conversation(
        app_id="default-app",
        body={"agent_name": "workflow_builder", "title": "B", "metadata": {"workflow_id": "wf-1"}},
        db=db, user=_user(),
    ))
    asyncio.run(agents.create_conversation(
        app_id="default-app",
        body={"agent_name": "general_assistant", "title": "G", "metadata": {"workflow_id": "wf-1"}},
        db=db, user=_user(),
    ))
    rows = asyncio.run(agents.list_conversations(
        app_id="default-app", agent_name="workflow_builder", workflow_id="wf-1",
        db=db, user=_user(),
    ))
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "workflow_builder"
```

**Step 2: Run — expect FAIL** (`TypeError: list_conversations() got an unexpected keyword argument 'workflow_id'`).

**Step 3: Implement** — add the query param + filter:

```python
@router.get("/apps/{app_id}/agents/conversations")
async def list_conversations(
    app_id: str,
    agent_name: str | None = Query(None),
    workflow_id: str | None = Query(None),
    limit: int | None = Query(None),
    skip: int | None = Query(None),
    sort_by: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    ...
    if agent_name:
        query = query.filter(AgentConversation.agent_name == agent_name)
    if workflow_id:
        # JSON-path filter on the metadata column — cross-dialect (PG:
        # metadata -> 'workflow_id' cast to text; SQLite tests: json_extract).
        # cast(..., String) avoids `.astext`, which SQLite's JSON comparator
        # does not implement.
        query = query.filter(
            cast(AgentConversation.metadata_["workflow_id"], String) == workflow_id
        )
```

Add `from sqlalchemy import cast, String` to agents.py imports (check existing imports first — `String` is very likely already imported; `cast` may need adding).

**Step 4: Run — expect PASS.** Commit.

## Task 2: Frontend `listAgentConversations` API wrapper (TDD)

**Objective:** The frontend can query conversations by agent + workflow_id.

**Files:**
- Modify: `frontend/src/api/agentEnhanced.js` (near createAgentConversation, ~line 194)
- Modify: `frontend/src/api/agentEnhanced.test.js`

**Step 1: Write failing test** — follow the existing mock-fetch style in that file:

```js
it('listAgentConversations queries /agents/conversations with agent_name + workflow_id', async () => {
  // arrange mock fetch returning [{id: 'c1', metadata: {workflow_id: 'wf-1'}}]
  // act: const rows = await listAgentConversations('workflow_builder', { workflowId: 'wf-1' });
  // assert: fetch called with URL containing `agent_name=workflow_builder` and `workflow_id=wf-1` (URL-encoded)
});
```

**Step 2: Run — expect FAIL** (`listAgentConversations is not a function`).

**Step 3: Implement** in agentEnhanced.js:

```js
/**
 * List agent conversations, optionally filtered by workflow_id (the
 * per-workflow session key written into metadata at creation). Used by the
 * Build-with-AI panel to resume the latest session for a workflow.
 */
export async function listAgentConversations(agentName, { workflowId, limit } = {}) {
  const params = new URLSearchParams();
  if (agentName) params.set('agent_name', agentName);
  if (workflowId) params.set('workflow_id', workflowId);
  if (limit) params.set('limit', String(limit));
  const qs = params.toString();
  return authFetch(`${API_BASE}/agents/conversations${qs ? `?${qs}` : ''}`);
}
```

(Check `authFetch` vs the wrapper createAgentConversation uses — match the file's existing fetch helper exactly.)

**Step 4: Run — expect PASS.** Commit.

## Task 3: Build-with-AI session resume + New session button

**Objective:** Reopening the panel resumes the latest build session for this workflow (history visible, same convId); a "New session" button starts fresh.

**Files:**
- Modify: `frontend/src/components/workflow/WorkflowBuilderChat.jsx`
- Modify: `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx` (update mocks if it mocks the agentEnhanced module)

**Step 1: Import** `listAgentConversations` alongside `createAgentConversation`.

**Step 2: Replace the session-start effect** (lines 48-85) with start-or-resume:

- New state: `const [resumed, setResumed] = useState(false);` and `const [startingSession, setStartingSession] = useState(false);`
- On open: `listAgentConversations('workflow_builder', { workflowId, limit: 1 })`:
  - row found → `convIdRef.current = row.id; setConvId(row.id); setResumed(true);` and hydrate messages from `row.messages` (map to `{role, content}`, keep user/assistant with string content; drop tool/system/empty).
  - none → `createAgentConversation('workflow_builder', { workflow_id: workflowId }, 'Workflow builder')` exactly as today (`setResumed(false)`).
- Keep the `cancelled` cleanup + abort logic and the error bubble.
- `startNewSession()`: guarded by `startingSession`; calls createAgentConversation, resets `messages=[]`, `convId`, `setResumed(false)`.

**Step 3: Header button** — when `resumed && !streaming`, render next to Close:

```jsx
<Button variant="ghost" size="sm" onClick={startNewSession} disabled={startingSession} title={isEn ? 'Start a new session' : '新建会话'}>
  <RotateCcw className="h-3.5 w-3.5" /> {isEn ? 'New' : '新建'}
</Button>
```

Import `RotateCcw` from lucide-react (check it's exported).

**Step 4: Update WorkflowBuilderChat.test.jsx** — if it mocks `createAgentConversation`, add `listAgentConversations` to the mock (return `[]` → falls through to create; one test asserting the resume path returns `[{id, messages}]` → convId set).

**Step 5:** `npx vitest run src/components/workflow/WorkflowBuilderChat.test.jsx src/api/agentEnhanced.test.js` then `npm run build`.

## Task 4: Backend restart, E2E verify, commit

**Step 1:** `docker compose -p zhanlu_sep_1_n8n restart backend`

**Step 2: API E2E (PG path — proves the JSON cast on Postgres):**

```bash
# login → create a workflow → create 2 workflow_builder convs for it (POST /agents/conversations with metadata.workflow_id)
# → GET /agents/conversations?agent_name=workflow_builder&workflow_id=<id>
# expect: only that workflow's convs, messages included
```

**Step 3: Browser verify** — open any workflow canvas → Build with AI → send "create a simple two-node workflow" → close → reopen → history present (resumed) → click New → fresh empty panel. Then full pytest sweep of the touched files.

**Step 4: Commit** (backend + frontend together or two commits).

## Notes / decisions

- `workflow_id` is a GROUPING key, not unique — a workflow may have many build sessions and many chat sessions (n8n-like). Resume = latest only; "New session" for intent to rebuild.
- Deferred: canvas "Chat sessions" drawer (backend supports it now; UI follow-up).
- Main-chat sessions already store workflow_id — no change needed there; they become queryable via the same filter.
