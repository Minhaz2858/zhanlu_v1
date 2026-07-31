# Skill Agent as a Chat Agent Mode — Design

**Date:** 2026-07-28
**Status:** Draft — awaiting user review
**Author:** CodeBuddy (brainstorming session)
**Brainstorming source:** 2026-07-28 chat, options 1+2+3+strategy resolved in conversation

**Related code:**
- `backend/app/services/system_agents.py` (system agent registry)
- `backend/app/services/agent_prompts.py` (system prompts + tool list)
- `backend/app/services/agent_tools.py` (system-agent helpers)
- `backend/app/models/agent_conversation.py` (legacy skill_agent session table)
- `backend/app/models/agent_app.py` (AgentApp + skills field)
- `frontend/src/pages/SkillAgent.jsx` (the dedicated page being retired)
- `frontend/src/pages/Chat.jsx` (the host that gains the skill_agent mode)
- `frontend/src/components/chat/InvokePicker.jsx` (gains the pinned "Skill Agent" row)
- `frontend/src/lib/ChatSessionContext.jsx` (gains an AgentConversation merge)
- `frontend/src/components/skillagent/SkillFilePanel.jsx` (reused as-is)
- `frontend/src/components/skillagent/SkillUploadButton.jsx` (reused as-is)
- `frontend/src/components/skillagent/SkillMessageBubble.jsx` (reused as-is)
- `frontend/src/lib/systemAgents.js` (already lists `skill_agent`)
- `frontend/src/pages/Toolkit.jsx` (callsites to rewrite)
- `frontend/src/App.jsx` (route to delete)

---

## 1. Problem

Today `skill_agent` is the platform's specialized agent for creating, editing, collecting, and productizing Skills. It lives behind a dedicated `/skill-agent` page with a narrow toolset (skill-management helpers, `web_search`, `web_extract`, `create_artifact`). Users who want to do general work while a skill-creation session is open (e.g. inspect an existing file with `read_file`, run a quick `code_execution` to verify a SKILL.md example, or look up something via the memory tool) have to switch agents, lose their skill context, then come back.

Concretely, today `skill_agent` lacks: `memory`, `todo`, `code_execution`, `file_operations`, `image_generation`, `browser automation` / `agent_browser`, `read_file`, `write_file`, `delegate_task`, `ask_data_agent`, `run_sandbox_skill`, `load_skill_body`, and the `Skill` system-helper that other specialized agents use. It is the only system agent on `gpt-4o-mini` (every other one is `gpt-4o`).

The user wants two things at once:

1. **`skill_agent` should work like a normal AI agent** — the full zhanlu toolset, so it can do anything a general agent can do *while still being specialized for skill creation*.
2. **The dedicated `/skill-agent` page should go away** — `skill_agent` becomes a mode/picker option inside the existing Chat page, so all chat UX (sidebar, sessions, streaming, traces, panels) is unified. The dedicated page is redundant once the picker is wired.

---

## 2. Design

### 2.1 High-level model

```text
User opens /chat
   │
   ▼
Chat.jsx — sees activeAgent from picker
   │
   ├─ if activeAgent?.name === 'skill_agent'
   │      │
   │      ▼
   │   <SkillAgentMode>
   │      • owns currentSkill, panelOpen, pendingFiles, prefilledHint
   │      • empty state: 4-suggestion landing + SkillUploadButton
   │      • active state: messages + SkillFilePanel sidebar (resizable)
   │      • permission-mode selector (default | plan | full_auto)
   │      • deep-link handlers for ?action=create, ?skill=X, ?files=...
   │
   └─ else
          normal Chat UI (unchanged)
```

### 2.2 Locked design decisions (from brainstorming)

| Decision | Choice |
|---|---|
| How to surface `skill_agent` in Chat's agent picker | **Pinned "Skill Agent" row at the top of the picker** (above the user's own agents, marked as "specialized") |
| How to show the empty state when `skill_agent` is picked and the conversation is empty | **Re-render the existing 4-suggestion landing + SkillUploadButton** (centered, like the current `/skill-agent` page) |
| What to do with old `/skill-agent` deep links and existing `agent_name='skill_agent'` sessions | **Redirect `/skill-agent/*` → `/chat?agent=skill_agent[&...]`; old `AgentConversation` rows are merged into the unified sidebar via a small adapter** |
| Implementation strategy | **Option B — Extract `<SkillAgentMode>` wrapper component.** Encapsulates skill-specific state in one file; Chat.jsx delegates the whole chat pane to it when `activeAgent?.name === 'skill_agent'`. |
| Backend toolset | **Expand to match `general_assistant`** (full zhanlu toolset, not just skill-management). |
| Model upgrade | **`gpt-4o-mini` → `gpt-4o`** to match the other system agents. |

### 2.3 Why extract `<SkillAgentMode>` instead of inlining in Chat.jsx

The skill-mode UI has its own state (currentSkill, panelOpen, pendingFiles, prefilledHint), its own deep-link handlers (`?action=create`, `?skill=X`, `?files=...`), and a non-trivial scan over message tool_calls to keep `currentSkill` in sync with `create_skill` / `update_skill` results. Inlining this in `Chat.jsx` (which is already ~1900 lines and 100+ state vars) would:

- Make the existing `if (activeId || messages.length > 0)` empty-state branch turn into a triple-nested conditional (chat-mode empty / chat-mode active / skill-mode empty / skill-mode active).
- Force every future maintainer of Chat.jsx to learn the skill-mode state machine.
- Make skill-mode-specific tests impossible to write in isolation.

`<SkillAgentMode>` is a self-contained component that takes the props it needs (activeAgent, activeConv, t, etc.) and renders the entire chat pane. Chat.jsx does a single `if (activeAgent?.name === 'skill_agent') return <SkillAgentMode ... />` delegation. Easy to test, easy to revert, easy to extend if we add more specialized modes later.

---

## 3. Frontend changes

### 3.1 New component: `frontend/src/components/skillagent/SkillAgentMode.jsx`

Encapsulates everything currently in `pages/SkillAgent.jsx` (state, streaming, deep-link handlers, suggestion landing, SkillFilePanel sidebar, permission-mode selector, SkillUploadButton), but:

- Takes the surrounding page's props instead of creating its own conversation list (the unified sidebar already provides that).
- Does not own its own session sidebar (the Chat page's sidebar does that).
- Reuses `SkillFilePanel`, `SkillUploadButton`, `SkillMessageBubble`, `PrefilledHintPill` as-is.
- Renders the messages + input area + (optional) SkillFilePanel sidebar in the same shape Chat.jsx does for normal agents.

State owned by `<SkillAgentMode>`:
- `currentSkill` — scanned from `messages` for the most recent successful `create_skill` / `update_skill` tool result.
- `panelOpen` — whether the right-side SkillFilePanel is visible.
- `pendingFiles` — files attached to the next outgoing message.
- `prefilledHint` — whether a suggestion was applied to the input (shows `<PrefilledHintPill>`).
- `permissionMode` — `default` | `plan` | `full_auto`, persisted on the conversation metadata via `setPermissionMode`.
- Local streaming state (`loading`, `streamingContent`, abort controller) — same as current page.

State that the surrounding Chat.jsx already provides (NOT re-owned):
- `activeId`, `activeConv`, `messages`, `conversations`, `inputValue`, `setInputValue` (drafts persist via `draftManager`), `streamingId`, `streamingConvId`.
- `ChatSessionContext`'s `selectSession`, `newChat`, `prependSession`, `touchSession`, `deleteSession`, `renameSession`.

### 3.2 Modify: `frontend/src/components/chat/InvokePicker.jsx`

Add a single pinned "Skill Agent" row at the top of the agent dropdown, before the existing "Your agents" list.

```jsx
// Inside the filteredAgents rendering:
<div className="px-2 pt-1.5 pb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
  {t.chat.systemAgents}
</div>
<button onClick={onPickSkillAgent} className="...">
  <Wrench className="h-3 w-3" />
  <span>{t.skillAgent.title}</span>
  <span className="ml-auto text-[10px] text-muted-foreground">specialized</span>
</button>
<div className="my-1 border-t border-border" />
<div className="px-2 pb-1.5 pt-1 text-[10px] uppercase tracking-wider text-muted-foreground">
  {t.chat.yourAgents}
</div>
{filteredAgents.map(...)}
```

`onPickSkillAgent` is a new prop. It:
1. Resolves the system `skill_agent` row via `base44.entities.AgentApp.list({ name: 'skill_agent' })` (or a dedicated backend endpoint) — the same lookup Chat.jsx already does for `general_assistant` (search: `fetchAgent` / `setActiveAgent`).
2. Calls the existing `onSelectAgent(agent)` so the rest of Chat's flow handles the new active agent.

We add a small cache so the system-agent lookup only fires once per session, not every time the dropdown opens.

### 3.3 Modify: `frontend/src/pages/Chat.jsx`

Single insertion point. The chat-pane return (the `Panel id="chat-panel"` body, around line 1730) gets a top-level branch:

```jsx
if (activeAgent?.name === 'skill_agent') {
  return (
    <Panel id="chat-panel" order={1} defaultSize={100} minSize={30}>
      <SkillAgentMode
        activeAgent={activeAgent}
        activeId={activeId}
        activeConv={activeConv}
        messages={messages}
        loading={loading}
        streamingContent={streamingContent}
        streamState={streamState}
        inputValue={inputValue}
        setInputValue={setInputValue}
        // ... other shared handlers
      />
    </Panel>
  );
}
```

The rest of Chat.jsx (artifact panel, scheduled panel, trace panel, files modal, agent-context banner, etc.) is unchanged. The empty-state and active-state branches for the normal agent case stay where they are. The deep-link handler at the top of Chat.jsx (currently reads `?skill=` for a different purpose) gets a sibling branch for the new query params (`?agent=skill_agent&action=create`, `?agent=skill_agent&skill=X`, `?agent=skill_agent&files=...`).

The existing `handleAgentSend`, `handleUploadFile`, `handleBatchClarifySubmit`, etc. are not duplicated — `<SkillAgentMode>` either receives the bound handlers as props or (for skill-specific flows like the prefill-edit prompt) handles its own internal sending.

### 3.4 Modify: `frontend/src/lib/ChatSessionContext.jsx`

Add a small adapter that fetches `AgentConversation` rows where `agent_name = 'skill_agent'` and merges them into the `sessions` list. This keeps old sessions discoverable in the unified sidebar after the dedicated page is gone.

```jsx
// In ChatSessionProvider, alongside the existing initial-load effect:
useEffect(() => {
  let cancelled = false;
  base44.agents.listConversations({ agent_name: 'skill_agent' })
    .then((rows) => {
      if (cancelled) return;
      // Map AgentConversation → ChatSession shape for the sidebar:
      //   id           = AgentConversation.id
      //   title        = AgentConversation.title (or metadata.name)
      //   last_message_at = AgentConversation.updated_date
      //   agent_name   = 'skill_agent'   (so SessionList can badge it)
      //   _legacy_agent_conv = true       (so delete uses agents API, not ChatSession API)
      const mapped = (rows || []).map((r) => ({ ...r, _legacy_agent_conv: true }));
      setSessions((prev) => mergeLegacyAgentConvs(prev, mapped));
    })
    .catch(() => {/* best-effort */});
  return () => { cancelled = true; };
}, []);
```

`mergeLegacyAgentConvs` deduplicates by `id` (in case a row exists in both tables during a transition window) and prepends the legacy sessions sorted by `last_message_at`. The sidebar renders them like any other session, with a small "Skill Agent" badge on the row.

`deleteSession` is taught to soft-delete `_legacy_agent_conv = true` rows via `base44.agents.updateConversation(id, { metadata: { _deleted: true } })` (the existing convention used by `SkillAgent.jsx:341` and `useAgentBuilder.js:465`; the backend's `is_deleted == False` filter hides them on next list) and `base44.entities.ChatSession.delete(id)` for the rest.

### 3.5 Modify: `frontend/src/pages/Toolkit.jsx`

Rewrite the two callsites that navigate to `/skill-agent`:

```jsx
// Before
function openBuildWithAgent() { navigate('/skill-agent?action=create'); }
function openEditSkill(item) { navigate(`/skill-agent?skill=${item.id}`); }

// After
function openBuildWithAgent() { navigate('/chat?agent=skill_agent&action=create'); }
function openEditSkill(item) { navigate(`/chat?agent=skill_agent&skill=${item.id}`); }
```

The same pattern (probably) exists in other places that deep-link to `/skill-agent` — `grep -r '/skill-agent' frontend/src/` at implementation time.

### 3.6 Modify: `frontend/src/App.jsx`

Delete the `import SkillAgent` and the `<Route path="/skill-agent" element={<SkillAgent />} />` line.

Add a small `<SkillAgentRedirect>` component that preserves the query string when forwarding to `/chat?agent=skill_agent`:

```jsx
import { Navigate, useLocation } from 'react-router-dom';

function SkillAgentRedirect() {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  // The agent key is fixed; carry the rest of the params through
  // (?action=create, ?skill=X, ?files=...).
  const target = new URLSearchParams({ agent: 'skill_agent' });
  for (const [k, v] of params.entries()) {
    if (k !== 'agent') target.set(k, v);
  }
  return <Navigate to={`/chat?${target.toString()}`} replace />;
}

// ...
<Route path="/skill-agent" element={<SkillAgentRedirect />} />
<Route path="/skill-agent/*" element={<SkillAgentRedirect />} />
```

### 3.7 Delete: `frontend/src/pages/SkillAgent.jsx` (and its tests)

After the wrapper component ships and Chat.jsx is wired, the dedicated page and its direct-attach tests are redundant. Move the `currentSkill` sync test to `SkillAgentMode.test.jsx`, keep the `applySuggestion` test, and drop the file-level `SkillAgent.jsx` (it has no remaining callers once Toolkit.jsx is rewritten and App.jsx drops the route).

---

## 4. Backend changes

### 4.1 Expand `skill_agent` toolset (`backend/app/services/agent_prompts.py`)

Today: `SKILL_AGENT_TOOLS` is a narrow list — only skill-management helpers + `web_search` + `web_extract` + `Skill` + `create_artifact` (lines 1486-1500 in `agent_prompts.py`).

After: union the existing `SKILL_AGENT_TOOLS` with `GENERAL_ASSISTANT_TOOLS`. The cleanest is to change the function at line 1671-1672 from `return SKILL_AGENT_TOOLS` to `return _dedupe_tools(SKILL_AGENT_TOOLS + GENERAL_ASSISTANT_TOOLS)`. `_dedupe_tools` already exists (or we add a tiny helper) — tools with the same name are kept once.

This gives `skill_agent`:
- Memory (`memory`, `todo`)
- Code execution (`code_execution`, `file_operations`)
- Image generation (`image_generation`)
- Browser automation (`browser automation`, `agent_browser`)
- File operations (`read_file`, `write_file`, `list_files`)
- Delegation (`delegate_task`, `ask_data_agent`, `run_sandbox_skill`, `load_skill_body`)
- Plus the existing skill tools (`create_skill`, `update_skill`, `list_tools`, `search_skills`, the skill-management helpers, `Skill`, `create_artifact`)

### 4.2 Upgrade model to `gpt-4o` (`backend/app/services/system_agents.py`)

Change the `skill_agent` row's `model` field from `gpt-4o-mini` to `gpt-4o` so it matches `general_assistant`. The risk tier stays at `low` (creating a skill is reversible; the user can re-edit or delete).

### 4.3 System prompt: keep specialization, acknowledge the new tools

`SKILL_AGENT_SYSTEM_PROMPT` (`agent_prompts.py:500-609`) is rewritten to:

- Keep the "you specialize in creating, collecting, and productizing skills" identity at the top.
- Add a paragraph: "You also have the full zhanlu toolset (memory, code execution, file operations, image generation, browser automation, delegation). Use them freely to support your skill-creation work — e.g. read files before distilling them into a skill, run code to verify a SKILL.md example, browse a GitHub repo to extract a methodology."
- Keep the existing behavior rules (default to action, don't ask for trivial configuration, etc.).

No new prompt is needed for `<SkillAgentMode>` — the system prompt changes are enough; the frontend just stops hiding the rest of the toolset from the user.

### 4.4 No schema changes

- `AgentConversation` and `ChatSession` stay as-is.
- `AgentApp` gets a model bump via `ensure_system_agents` re-run on next boot (it already upserts and updates model/tools when the registry changes).
- `agents.py:1863` list endpoint stays; the frontend adapter calls it.

---

## 5. State / data flow

### 5.1 New skill_agent session lifecycle (in Chat)

```text
User clicks "Skill Agent" in InvokePicker
   → resolveAgent('skill_agent') → activeAgent
   → handlePickAgent(agent) → setActiveAgent(agent)
   → Chat.jsx sees activeAgent.name === 'skill_agent' → <SkillAgentMode>
   → empty state: 4-suggestion landing

User picks "Create new"
   → applySuggestion(t.skillAgent.suggestions.create) → inputValue
   → user edits, hits Enter
   → handleSend (parent's) → ensureSession → createAgentConversation('skill_agent', { description: '...' })
   → ChatSession row created; AgentConversation row created
   → messages stream, tool calls land
   → SkillAgentMode scans tool_calls → setCurrentSkill from create_skill result
   → SkillFilePanel sidebar populates
```

### 5.2 Old session lifecycle (AgentConversation, legacy)

```text
Old session exists in agent_conversations (agent_name='skill_agent')
   → ChatSessionContext initial-load adapter fetches and merges into sessions
   → SessionList shows it (with a small "Skill Agent" badge)
   → User clicks it
   → selectSession(id) sets activeId; on click, Chat.jsx also hydrates AgentConversation's messages
   → SkillAgentMode picks up messages, scans for currentSkill
```

Hydration: the existing `?conv=<id>` deep-link path in Chat.jsx is extended to recognize `_legacy_agent_conv` rows and call `base44.agents.getConversation(id)` instead of `base44.entities.ChatMessage.filter({ session_id })`. If the legacy row is opened mid-session, we additionally create a new ChatSession row + an "adopted from legacy conv X" note in metadata.

### 5.3 URL contract

| Old URL | New URL | Effect |
|---|---|---|
| `/skill-agent?action=create` | `/chat?agent=skill_agent&action=create` | Open Chat, auto-pick Skill Agent, show the 4-suggestion landing with the "Create new" suggestion pre-filled |
| `/skill-agent?skill=<id>` | `/chat?agent=skill_agent&skill=<id>` | Open Chat, auto-pick Skill Agent, create a new session, prefill the "Edit existing" prompt for that skill |
| `/skill-agent?files=url1,url2` | `/chat?agent=skill_agent&files=url1,url2` | Open Chat, auto-pick Skill Agent, create a new session, send the "process files" prompt |
| `/skill-agent` (bare) | `/chat?agent=skill_agent` | Open Chat, auto-pick Skill Agent, show landing |

The old `/skill-agent` route becomes a `<Navigate>` that preserves query params.

---

## 6. Files changed / created / deleted

### Created
- `frontend/src/components/skillagent/SkillAgentMode.jsx`
- `frontend/src/components/skillagent/SkillAgentMode.test.jsx` (move the `currentSkill` sync test + `applySuggestion` test from SkillAgent.test.jsx)
- `docs/superpowers/plans/2026-07-28-skill-agent-as-chat-mode.md` (the implementation plan, written by the writing-plans skill)

### Modified
- `frontend/src/components/chat/InvokePicker.jsx` — pinned "Skill Agent" row
- `frontend/src/pages/Chat.jsx` — delegate to `<SkillAgentMode>` when `activeAgent.name === 'skill_agent'`; handle new `?agent=skill_agent&...` deep-link
- `frontend/src/lib/ChatSessionContext.jsx` — merge legacy `AgentConversation` rows into the sessions list; teach `deleteSession` to call the right API
- `frontend/src/pages/Toolkit.jsx` — rewrite two deep-link callsites
- `frontend/src/App.jsx` — delete the `SkillAgent` import + route, add a `<Navigate>` redirect
- `backend/app/services/system_agents.py` — bump `skill_agent` model to `gpt-4o`
- `backend/app/services/agent_prompts.py` — union `SKILL_AGENT_TOOLS + GENERAL_ASSISTANT_TOOLS`; rewrite `SKILL_AGENT_SYSTEM_PROMPT`
- `frontend/src/lib/translations.js` — add `t.chat.systemAgents` / `t.chat.yourAgents` section headers; add any new keys the wrapper needs (e.g. permission-mode labels)

### Deleted
- `frontend/src/pages/SkillAgent.jsx`
- `frontend/src/pages/SkillAgent.currentSkillSync.test.jsx`
- `frontend/src/pages/SkillAgent.applySuggestion.test.jsx`
- (after the rewrite) Any other `import` of `SkillAgent` (grep at impl time)

---

## 7. Testing strategy

### Unit (frontend)
- `<SkillAgentMode>`: empty state renders the 4 suggestions + SkillUploadButton; landing → user types → handleSend; `currentSkill` syncs from `create_skill` / `update_skill` tool results; permission mode selector persists on conv metadata; `?action=create` and `?skill=X` deep links apply the correct prefill.
- `InvokePicker`: pinned "Skill Agent" row appears even with zero user agents; clicking it resolves and calls `onSelectAgent`; existing user-agent list still works below the divider.
- `ChatSessionContext`: legacy `AgentConversation` rows merge in; `_legacy_agent_conv` rows soft-delete via `agents.updateConversation(id, { metadata: { _deleted: true } })` on delete.

### Unit (backend)
- `agent_prompts.py`: `_apply_agent_def_tool_filter('skill_agent', all_schemas)` returns a list that includes all `general_assistant` tools (snapshot the union).
- `system_agents.py`: `skill_agent` registry entry has `model == 'gpt-4o'` and `risk_tier == 'low'`.

### Regression
- Existing Chat tests: not touched, the skill-mode branch is additive.
- Existing skill-agent page tests: deleted with the page. Their coverage moves to `SkillAgentMode.test.jsx`.
- `agents.listConversations({ agent_name: 'skill_agent' })` test: still passes (we use it from the merge adapter).

### Manual
- Open `/chat`, pick "Skill Agent" → landing appears → click "Create new" → input is prefilled → send → agent responds → SkillFilePanel shows the new skill.
- Open `/chat?agent=skill_agent&skill=<id>` → edit flow prefill is sent → agent shows the current skill.
- Reload an old `/skill-agent?action=create` URL → redirects to `/chat?agent=skill_agent&action=create` → landing appears.
- Open the unified sidebar → see both new ChatSession rows and the merged legacy AgentConversation rows; click a legacy row → messages load.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Toolset expansion makes `skill_agent` more expensive / slower | We don't enable tools the user didn't ask for. The expansion is *available* — the agent still defaults to skill creation in its prompt. Model upgrade to `gpt-4o` matches the other agents and is what the user asked for. |
| Existing `/skill-agent` bookmarks break | The `<Navigate>` redirect preserves query params, so `?action=create`, `?skill=X`, `?files=...` all flow through. Worst case: a user lands on the bare landing in Chat instead of the old dedicated page — but they still get the same affordance. |
| Old `AgentConversation` rows are inaccessible | The merge adapter in `ChatSessionContext` lists them. If merge proves complex, fall back to: keep a hidden `/skill-agent` archive page (the 3rd migration option) — but the redirect + merge is the user's chosen path. |
| `<SkillAgentMode>` is too tightly coupled to Chat.jsx's state | Component takes a flat list of props (activeAgent, activeId, messages, inputValue, etc.) and uses callbacks. Easy to refactor later if the state shape changes. |
| Sidebar's SessionList assumes a `ChatSession` shape | `SessionList` reads `id`, `title`, `last_message_at`, `project` from each row — all of which we provide in the merge adapter's mapped shape. If a legacy row has no `title`, fall back to `metadata.name` then `Untitled`. |
| Permission-mode selector state collides with Chat's existing permission model | Chat.jsx doesn't currently expose a permission-mode selector in its UI. `<SkillAgentMode>` owns its own. No collision. |
| `create_skill` runs even on `full_auto` for non-skill-creation tasks | The system prompt's specialization is the safeguard: it tells the agent to ask before running `create_skill` outside a clear skill-creation context. The existing permission-mode gates (default/plan/full_auto) apply. |

---

## 9. Out of scope

- One agent per project. We keep the global `skill_agent` system agent.
- Multi-tenant isolation beyond `org_id` / `app_id`.
- A "Skill Agent lite" mode for the free tier.
- Per-mode UI for other system agents (`agent_builder`, `automation_agent`, `power_user`). If we ever need those, the `<SkillAgentMode>` pattern is reusable.
- A visual "specialized" badge across all UI surfaces — only the agent picker surfaces the badge in this iteration.
- Deleting the `AgentConversation` table. Old rows live in it forever; new skill_agent sessions go to `ChatSession`.
- Migrating legacy `AgentConversation` rows to `ChatSession` (the merge adapter is enough for the sidebar; we don't rewrite the data).

---

## 10. Open questions for the user

1. **Header label for the pinned row in the agent picker**: "Skill Agent" (matches page title), "技能助手", or "Minha's Skill Agent" (matches `t.skillAgent.madeBy`)? Default: "Skill Agent".
2. **Legacy sidebar badge**: render a small "legacy" pill on the merged `AgentConversation` rows, or just hide them by default behind a "Show old skill agent sessions" toggle? Default: small pill, no toggle.
3. **Deep-link fallback**: if a user hits `/skill-agent?action=create` but the agent picker hasn't loaded yet, do we (a) wait for the picker to resolve `skill_agent` and then auto-pick, or (b) show the bare landing and let them click "Skill Agent" themselves? Default: (a), with a 3-second timeout to (b) to avoid hanging the page.
