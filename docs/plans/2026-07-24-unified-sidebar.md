# Unified Sidebar Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Merge the main nav `Sidebar` and the chat `SessionList` into a single context-aware unified sidebar, reclaiming ~280px of horizontal space on small windows.

**Architecture:**
- Lift session state (sessions, activeId, pendingProject, pendingProjectId + handlers) from `Chat.jsx` into a new `ChatSessionContext` provider mounted in `AppLayout`.
- Transform `Sidebar.jsx` into a route-aware `UnifiedSidebar` that:
  - Always renders brand + nav links + UserMenu
  - On `/` (Chat route), additionally renders the chat-specific content (New Task button, project dropdown, sessions list)
- `SessionList` becomes a presentational component that consumes the context.
- `Chat.jsx` no longer renders `SessionList` directly; it reads from the context.

**Tech Stack:** React 18, react-router-dom v6, Tailwind CSS, lucide-react icons, @base44/sdk, vitest + @testing-library/react.

---

## Task 1: Create `ChatSessionContext`

**Files:**
- Create: `frontend/src/lib/ChatSessionContext.jsx`

Move the session state and handlers out of `Chat.jsx` into a context provider.

- State: `sessions`, `activeId`, `pendingProject`, `pendingProjectId`
- Actions: `setActiveId`, `setPendingProject(name, id)`, `selectSession(id)`, `newChat(projectName)`, `deleteSession(s)`, `starSession(s)`, `renameSession(id, title)`, `refreshSessions()`
- On mount, fetches `ChatSession.list` and exposes `loading`
- When `activeId` changes, internally manages no side effect on messages (that stays in Chat.jsx because messages are per-page concern)

**Step 1-2: Write the context file with the lifted state and handlers.**

## Task 2: Refactor `SessionList` to consume context

**Files:**
- Modify: `frontend/src/components/chat/SessionList.jsx`

- Remove `sessions`, `activeId`, `pendingProject`, `pendingProjectId`, `onProjectChange`, `onSelect`, `onNew`, `onDelete`, `onStar`, `onRename` from props.
- Read all state and handlers from `useChatSession()` hook.
- Keep `panelCollapsed` as local state.
- Keep all visual design intact.

## Task 3: Transform `Sidebar.jsx` into `UnifiedSidebar.jsx`

**Files:**
- Modify: `frontend/src/components/Sidebar.jsx` (keep filename to minimize import changes) OR rename to `UnifiedSidebar.jsx` and update imports.

**Decision: Keep filename as `Sidebar.jsx`** — the file still exports a `Sidebar` default export; we just enrich it.

- Add `useLocation` from `react-router-dom`.
- When on `/`, render the chat-specific section between nav and UserMenu:
  - `<NewTaskButton />` (small extracted component or inline)
  - `<ProjectDropdown />` (small extracted component or inline)
  - `<SessionList />` (which now reads from context)
- When NOT on `/`, render just the nav (current behavior).
- Keep collapse/expand state as before.
- Width stays at `w-64` (256px) for chat route (to match old SessionList), `w-60` (240px) for other routes. Or keep both at `w-64`. **Decision: use `w-64` always** for visual consistency.

## Task 4: Wire up `AppLayout` with the provider

**Files:**
- Modify: `frontend/src/components/AppLayout.jsx`

- Wrap `<Outlet />` (and `<Sidebar />`) in `<ChatSessionProvider>`.

## Task 5: Update `Chat.jsx` to use the context

**Files:**
- Modify: `frontend/src/pages/Chat.jsx`

- Remove local `sessions`, `activeId`, `pendingProject`, `pendingProjectId` state.
- Read them from `useChatSession()`.
- Remove the `<SessionList ... />` rendering from JSX.
- Remove the `handleDelete`, `handleStar`, `handleRename` functions (now in context).
- Keep `handleNew` behavior (call `newChat(projectName)` from context).
- Keep the URL param parsing for `?project=&projectName=&agent=` but call `setPendingProject(name, id)` from context.

## Task 6: Update existing `Chat.test.jsx`

**Files:**
- Modify: `frontend/src/pages/Chat.test.jsx`

- Remove the `vi.mock('@/components/chat/SessionList', ...)` since Chat no longer renders it directly.
- Wrap `renderChat` in `<ChatSessionProvider>` with a mock provider for test isolation.
- Or, since the test doesn't care about session UI, just remove the SessionList mock and let it render with the default context value.

## Task 7: Add tests for `UnifiedSidebar` / `Sidebar`

**Files:**
- Create: `frontend/src/components/Sidebar.test.jsx`

- Test: renders brand + nav on non-chat route
- Test: renders New Task + project dropdown on `/` route
- Test: collapse/expand button toggles width
- Test: UserMenu is always shown

## Task 8: Run full test suite

- Run `cd frontend && npx vitest run`
- Fix any failures

## Task 9: Visual verification

- Start dev server
- Use playwright/agent-browser to verify the unified sidebar looks right on chat and other routes
- Test small window behavior

---

## Architecture Decisions

1. **Context over prop drilling**: Chat and Sidebar both need session state. Lifting to a context is the cleanest way.

2. **Single sidebar, conditional content**: Keep the existing Sidebar component as the unified sidebar. Just make its content route-aware. This minimizes the refactor surface.

3. **Width: 256px (`w-64`) always**: Slightly wider than the old 240px main sidebar but matches the old SessionList width. The extra 16px is worth the consistency.

4. **Test isolation**: The new context provider will be used in tests via a small wrapper or the test will create a `ChatSessionProvider` mock.

5. **Backward compatibility for SessionList**: We change SessionList's prop signature. If any other place uses it (besides Chat.jsx and the new Sidebar), it'll break. The codebase search showed only Chat.jsx uses it.
