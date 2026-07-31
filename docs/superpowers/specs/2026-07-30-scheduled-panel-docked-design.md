# Scheduled Panel — Dock as Right-Side Panel (match ArtifactPanel)

**Date:** 2026-07-30
**Status:** Design (brainstorming complete, ready for plan)
**Scope:** Frontend only. Convert `ScheduledPanel` from a fixed-position overlay into a docked, resizable right-side panel inside the existing `PanelGroup`, matching the `ArtifactPanel` pattern. No backend changes.

## 1. Problem

`ScheduledPanel` (the right-side drawer showing scheduled-task info: schedule, past runs, run checklist, failure actions, deliverables) currently renders as a fixed overlay — `ScheduledPanel.jsx:309–312`:

```jsx
className={`fixed right-0 top-0 z-40 h-screen w-[420px] max-w-[90vw] transform border-l border-border bg-card shadow-2xl transition-transform duration-300 ease-out ${
  open ? 'translate-x-0' : 'translate-x-full'
}`}
```

It floats *on top of* the chat with a slide-in animation and a drop shadow — colloquially, it "pops up".

The reference pattern in the same codebase is `ArtifactPanel`, rendered as a docked, resizable right-side panel inside a `PanelGroup` (`pages/Chat.jsx:2078–2082`):

```jsx
<PanelGroup ...>
  <Panel id="chat" ... />
  <PanelResizeHandle ... />
  <Panel id="artifact-panel" order={2} defaultSize={40} minSize={20} maxSize={75}>
    <ArtifactPanel ... />
  </Panel>
</PanelGroup>
```

Chat and artifact share the layout; no overlay, no z-index, no slide animation. User expectation: the scheduled panel should behave like the artifact panel — docked on the right, resizable, part of the layout — not as a popup overlay.

## 2. Goals / Non-goals

**Goals**
- `ScheduledPanel` becomes a docked right-side panel inside the existing `PanelGroup`, matching `ArtifactPanel`'s mount/resize pattern.
- When the scheduled panel and the artifact panel are both relevant, they render **side-by-side** on the right, both resizable.
- The existing close (X) and toggle (header clock) behaviors are preserved, now operating on a docked panel.
- Internal data fetching, SSE live updates, polling, run history, and run-detail expansion remain unchanged.

**Non-goals (this phase)**
- Auto-opening the scheduled panel when a session is linked to a scheduled task (keep manual toggle via the header clock button).
- Tabs, vertical stacking, or a different coexistence model (decision: side-by-side horizontal).
- Changing `artifact-panel` or `artifact-preview-panel` behavior beyond reassigning their `order` props so the visual layout is correct.
- Persistent panel sizes across reloads (library default reset is acceptable; same as artifact-panel today).

## 3. Decisions (from brainstorming)

| Question | Choice |
|---|---|
| Coexistence with `ArtifactPanel` | **Side-by-side docked panels** — both render as separate docked `<Panel>`s splitting the right side of the layout. |
| Order in `PanelGroup` | **`chat` → `scheduled-panel` → `artifact-panel` → `artifact-preview-panel`** (visual left-to-right). Scheduled sits next to the chat because it's *this chat's* task; artifact is the generic preview pane on the far right. |
| Open/close model | **Conditional mount** — `ScheduledPanel` is rendered into the `PanelGroup` only when `scheduledOpen` is true. Closing (X) sets `scheduledOpen=false` → the Panel unmounts, chat expands. Same pattern the code already uses for `artifact-panel` (`{previewResult && (<>handle + Panel</>)}`). |
| Resize / sizing | **Resizable** via `react-resizable-panels`. Defaults when all panels open: chat=40, scheduled=30, artifact=30. Min sizes: chat=25, scheduled=22, artifact=22. Same `PanelResizeHandle` styling as the existing handle. |

## 4. Existing assets (reused, not replaced)

- `PanelGroup` (`pages/Chat.jsx`) using `react-resizable-panels` (`PanelGroup`, `Panel`, `PanelResizeHandle`) — already wraps the chat panel and conditionally mounts `artifact-panel` (`order={2}`) and `artifact-preview-panel` (`order={4}`). The scheduled panel plugs into the same group.
- `ArtifactPanel` (`components/chat/ArtifactPanel.jsx`) — reference docked-panel pattern: rendered directly inside a `<Panel>` with `h-full w-full` styling, no fixed positioning, no overlay classes. Drives the styling and layout conventions for the scheduled panel.
- `ScheduledPanel` (`components/chat/ScheduledPanel.jsx`) — internal logic (data fetch via `authFetch('/api/automations/by-session/{id}')`, SSE live overlay, 15s polling, run history, `RunChecklist`, `RunFailureActions`, file list, focus-execution deep-link, translations) is preserved verbatim. Only the outer wrapper and props change.
- `scheduledOpen` state, `openRun(executionId)` callback, `focusExecutionId` plumbing in `pages/Chat.jsx` — unchanged. The clock toggle in the chat header and the "Open run" deep-link still work; they just mount/unmount a docked Panel instead of opening an overlay.
- `PanelResizeHandle` styling (the `w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary` classes) — reused for the new handle between the chat and the scheduled panel.

## 5. Design

### 5.1 Chat.jsx — add scheduled-panel to the PanelGroup

Inside the existing `PanelGroup` in `pages/Chat.jsx`, insert a new conditional fragment between the chat `</Panel>` and the existing `{previewResult && (...artifact-panel...)}` fragment:

```jsx
{scheduledOpen && (
  <>
    <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-primary/50 data-[resize-handle-state=drag]:bg-primary" />
    <Panel id="scheduled-panel" order={2} defaultSize={30} minSize={22} maxSize={65}>
      <ScheduledPanel
        sessionId={activeId}
        focusExecutionId={focusExecutionId}
        onClose={() => setScheduledOpen(false)}
      />
    </Panel>
  </>
)}
```

Then **remove** the standalone overlay mount currently at `pages/Chat.jsx:2097`:

```jsx
<ScheduledPanel sessionId={activeId} open={scheduledOpen} onOpenChange={setScheduledOpen} focusExecutionId={focusExecutionId} />
```

Key points:
- `order={2}` for the scheduled panel. Because `react-resizable-panels` renders panels in ascending `order` left-to-right, and the chat panel has implicit order 1, this places the scheduled panel **immediately to the right of the chat** in the visual layout.
- To preserve `chat → scheduled → artifact` order, **bump `artifact-panel`'s `order` from `2` to `3`** (and leave `artifact-preview-panel` at `order={4}`). This is a one-line change in Chat.jsx but is load-bearing for the layout.
- `defaultSize={30}`, `minSize={22}`, `maxSize={65}` — scheduled never gets cramped.
- The chat `<Panel>` should be confirmed/updated to have `minSize={25}` so the 25% floor from the decision table is enforced (the library clamps chat when adjacent panels hit their mins; an explicit `minSize` on chat makes the contract explicit and prevents future regressions if a panel's min changes).
- `focusExecutionId` is still passed through so the "Open run" button in a chat message deep-links to a specific run when the panel opens.
- The new `onClose` callback matches the `ArtifactPanel` API (`onClose={...}` → close the docked panel by setting state on the parent).
- No change to `scheduledOpen`/`setScheduledOpen` state ownership; the header clock toggle keeps working.

### 5.2 ScheduledPanel.jsx — strip the overlay wrapper

The component currently renders a fixed overlay `<aside>` with translate/shadow/transition classes. Replace the outer wrapper with a flex container that fills its docked `<Panel>` cell:

```jsx
<aside
  role="complementary"
  aria-label={isEn ? 'Scheduled automation' : '定时任务'}
  data-testid="scheduled-panel"
  className="flex h-full w-full flex-col border-l border-border bg-card"
>
  ...header, schedule config, body (unchanged)...
</aside>
```

Concretely, on the outer `<aside>`:
- **Remove** `fixed`, `right-0`, `top-0`, `z-40`, `h-screen`, `w-[420px]`, `max-w-[90vw]`, `transform`, `shadow-2xl`, `transition-transform`, `duration-300`, `ease-out`, `translate-x-*`, `aria-hidden`, `data-open`.
- **Keep** `border-l border-border bg-card` so the docked panel reads as a separate column.
- **Add** `flex h-full w-full flex-col` so the existing inner `<div className="flex h-full flex-col">` stacks correctly inside the Panel.
- The inner `<div className="flex h-full flex-col">` stays as-is.

### 5.3 ScheduledPanel API — drop overlay-only props

Remove the `open` prop (the panel is now gated by mount, not by an overlay flag). Rename `onOpenChange` → `onClose` for API consistency with `ArtifactPanel`:

```js
export default function ScheduledPanel({ sessionId, focusExecutionId, onClose }) {
  ...
  // Header X button:
  onClick={() => onClose?.()}
}
```

`Chat.jsx` passes `onClose={() => setScheduledOpen(false)}`. The X button now calls `onClose?.()` instead of `onOpenChange?.(false)`.

This is a breaking change to `ScheduledPanel`'s public API, but the component has exactly one call site (`pages/Chat.jsx`) which is updated atomically.

### 5.4 Header / body / interactions — unchanged

- Header (task name + status + close button + "open in automation" link).
- Schedule config block (rrule label, next run, skip confirmations).
- Body (run history list, expandable rows, `RunChecklist`, `RunFailureActions`, deliverables, "Open in chat" deep-link).
- Polling every 15s, SSE overlay for in-progress runs, `focusExecutionId` auto-expand.
- All translations (`isEn` / `lang`) preserved.

## 6. Interaction flow (happy path)

1. User opens a chat session linked to a scheduled task. The scheduled panel is closed (not mounted).
2. User clicks the clock toggle in the chat header → `setScheduledOpen(true)` → the `Panel` + handle mount inside the `PanelGroup` → scheduled panel appears docked on the right at default width 30%.
3. User clicks an "Open run" link on a chat message → `openRun(executionId)` sets `scheduledOpen=true` and `focusExecutionId=<id>` → panel mounts and auto-expands the matching run row.
4. User drags the resize handle between chat and scheduled panel → panel width changes; chat shrinks accordingly. Drag the handle between scheduled and artifact → both right panels resize.
5. User clicks the X in the scheduled panel header → `onClose()` → `setScheduledOpen(false)` → Panel unmounts; chat expands to fill the freed space.
6. Artifact preview opens (e.g. user clicks a docx artifact in chat) → `artifact-panel` mounts to the right of the scheduled panel. Both visible side-by-side.

## 7. Edge cases

- **Session has no linked scheduled task** → `scheduledOpen` stays false → Panel never mounts → no empty-state UI occupies layout space. The "This chat isn't tied to a scheduled task." empty state still renders *inside* the component if a user opens the panel on an unlinked session (current behavior preserved).
- **`scheduledOpen` toggled off mid-run** → Panel unmounts → SSE `EventSource` closes via the component's cleanup effect → reopening re-fetches. Same behavior as the artifact panel closing.
- **Both scheduled and artifact panels open, chat narrow** → each panel respects its `minSize`; the chat is clamped to `minSize={25}` on its right edge. Resize handles remain draggable.
- **`focusExecutionId` becomes stale** → existing `useEffect` resets `expanded`; behavior unchanged.
- **Resize persistence** → `react-resizable-panels` resets sizes when panels mount/unmount (library default). No persistence this phase — same as artifact-panel today.
- **All four panels open** (chat + scheduled + artifact + artifact-preview) → chat becomes very narrow; library clamps via `minSize` props. Pre-existing pattern; not made worse by this change.
- **`order` prop collision** → `react-resizable-panels` renders panels in ascending `order`. To get the desired visual `chat → scheduled → artifact → preview` layout, the order props must be: chat (implicit 1) → scheduled `order={2}` → artifact `order={3}` → preview `order={4}`. The change bumps artifact-panel from `order={2}` to `order={3}`. This is a critical detail called out in the plan.

## 8. Testing

- **Frontend (vitest):**
  - `ScheduledPanel` renders without overlay classes when used standalone (no `fixed`, no `z-40`, no `shadow-2xl`, no `translate-x-*`).
  - `ScheduledPanel` does not require an `open` prop.
  - The X button calls `onClose`.
  - Existing `RunChecklist.test.jsx` and `RunFailureActions.test.jsx` continue to pass — internal components untouched.
- **Manual / E2E smoke (live, vs Postgres):**
  - Open chat → click clock toggle → scheduled panel docks on the right (no overlay, no shadow).
  - Trigger "Open run" from a chat message → panel opens, focuses the run row, SSE stream starts if running.
  - Drag resize handle → chat and panel resize smoothly.
  - Open an artifact preview → scheduled and artifact panels sit side-by-side, both resizable.
  - Click X → scheduled panel closes, chat expands; SSE closes.
  - Reuse the `admin@zhanlu.dev` / `admin123` credentials convention.

## 9. Risks

- **`react-resizable-panels` conditional panels** — the library is known to be finicky when panels mount/unmount. The codebase already conditionally mounts `artifact-panel` and `artifact-preview-panel` with `<>handle + Panel</>` fragments, so the pattern is established and working. Mirroring it for `scheduled-panel` keeps behavior consistent.
- **Layout jank on toggle** — opening/closing a docked Panel may cause a brief resize of the chat. Acceptable; same as the existing artifact-panel toggle.
- **Order prop collision** — covered in §7 edge cases; the fix is to bump `artifact-panel`'s `order` from `2` to `3` and assign `order={2}` to the scheduled panel.
- **Removing the `open` prop** is a breaking change to the component's public API, but `ScheduledPanel` has exactly one call site (`pages/Chat.jsx`). Updated atomically in the same change.

## 10. Phasing (for the implementation plan)

- **Phase 1 — ScheduledPanel component refactor:** strip the overlay wrapper (`ScheduledPanel.jsx`), drop `open` prop, rename `onOpenChange` → `onClose`, change outer `<aside>` to a flex container filling its parent.
- **Phase 2 — Chat.jsx integration:** add the conditional `<Panel>` + `<PanelResizeHandle>` inside the `PanelGroup` with `order={2}`; remove the standalone overlay mount; pass `onClose={() => setScheduledOpen(false)}`; bump `artifact-panel`'s `order` from `2` to `3`.
- **Phase 3 — Verification:** manual smoke + frontend tests + visual confirmation that the layout matches `ArtifactPanel` and both panels coexist side-by-side.

Phases 1 and 2 should land in the same commit (single logical change). No backend changes.