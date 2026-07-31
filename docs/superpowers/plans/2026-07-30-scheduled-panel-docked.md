# Scheduled Panel Docking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `ScheduledPanel` from a fixed overlay into a docked, resizable right-side panel inside the existing `PanelGroup`, matching `ArtifactPanel`'s pattern. Side-by-side coexistence with `ArtifactPanel`.

**Architecture:** Strip the overlay wrapper from `ScheduledPanel.jsx` (drop `open` prop, rename `onOpenChange`→`onClose`, replace the `<aside>` classes with a flex container that fills its parent). In `Chat.jsx`, mount `ScheduledPanel` conditionally inside the existing `PanelGroup` between the chat panel and `artifact-panel`, and bump `artifact-panel`'s `order` from `2` to `3` so the visual layout becomes `chat → scheduled → artifact → preview`. Single frontend commit.

**Tech Stack:** React, `react-resizable-panels` (`PanelGroup`/`Panel`/`PanelResizeHandle`), `@testing-library/react`, vitest, TailwindCSS, vite.

## Global Constraints

- **Frontend only.** No backend changes.
- **Coexistence**: side-by-side docked panels (`chat → scheduled-panel → artifact-panel → artifact-preview-panel` visual order).
- **Open/close**: `ScheduledPanel` is conditionally mounted inside the `PanelGroup` (same pattern as `artifact-panel`'s `{previewResult && (<>handle + Panel</>)}`).
- **API**: `ScheduledPanel` props become `{ sessionId, focusExecutionId, onClose }` — drop `open`, rename `onOpenChange`→`onClose` (matches `ArtifactPanel`).
- **Layout**: `scheduled-panel` gets `order={2}`, `defaultSize={30}`, `minSize={22}`, `maxSize={65}`. `artifact-panel` `order` bumps `2→3`. Chat panel (`order={1}`, `minSize={30}`) and `artifact-panel` (`minSize={20}`) stay unchanged — these already enforce a usable floor.
- **Commits require explicit user approval** per project convention ("Commit only when user asks"). Pause for user confirmation before each commit step. Do not auto-commit code.
- **Single frontend commit** for the whole change (`ScheduledPanel.jsx` + `ScheduledPanel.test.jsx` + `Chat.jsx`).
- **Frontend deploy**: rebuild dist with `/opt/rebuild-zhanlu.sh --force` after commit (dist can silently go stale vs worktree).
- **Test command**: `cd /root/zhanlu/frontend && npm run test` (vitest run). **Build command**: `cd /root/zhanlu/frontend && npm run build`. **Lint command**: `cd /root/zhanlu/frontend && npm run lint`.

---

## File map

| File | Change |
|---|---|
| `frontend/src/components/chat/ScheduledPanel.jsx` | Modify: drop overlay wrapper, change signature, update X-button handler. |
| `frontend/src/components/chat/ScheduledPanel.test.jsx` | Create: new co-located test file (matches `RunChecklist.test.jsx` / `RunFailureActions.test.jsx` convention). |
| `frontend/src/pages/Chat.jsx` | Modify: add conditional `<Panel>` inside `PanelGroup`, bump `artifact-panel` order `2→3`, remove standalone overlay mount. |

---

### Task 1: Write failing tests for `ScheduledPanel` docked contract

**Files:**
- Create: `frontend/src/components/chat/ScheduledPanel.test.jsx`

**Interfaces:**
- Consumes: existing `ScheduledPanel` (current overlay contract).
- Produces: tests that gate the new docked contract (no overlay classes; X button calls `onClose`).

- [ ] **Step 1: Create the test file**

Create `frontend/src/components/chat/ScheduledPanel.test.jsx` with the following content (matches the conventions in `frontend/src/components/automation/RunChecklist.test.jsx` — co-located, vitest + `@testing-library/react`, `LanguageProvider` wrapper, `afterEach(cleanup)`):

```jsx
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { LanguageProvider } from '@/lib/LanguageProvider';

// ScheduledPanel calls authFetch('/api/automations/by-session/{id}') on mount
// and uses EventSource for live SSE. Mock both so the test focuses on the
// docked-contract DOM assertions without network or real streams.
vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn().mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ task: null, executions: [] }),
  }),
}));

global.EventSource = vi.fn(() => ({
  close: vi.fn(),
  onmessage: null,
  onerror: null,
}));

import ScheduledPanel from './ScheduledPanel';

function renderWithProvider(ui) {
  return render(<LanguageProvider>{ui}</LanguageProvider>);
}

describe('ScheduledPanel (docked contract)', () => {
  it('outer aside uses flex layout and has no overlay positioning classes', () => {
    renderWithProvider(<ScheduledPanel sessionId="s1" onClose={() => {}} />);
    const aside = screen.getByRole('complementary');
    const cls = aside.className;
    expect(cls).toMatch(/flex/);
    expect(cls).toMatch(/h-full/);
    expect(cls).toMatch(/w-full/);
    expect(cls).not.toMatch(/\bfixed\b/);
    expect(cls).not.toMatch(/z-40/);
    expect(cls).not.toMatch(/shadow-2xl/);
    expect(cls).not.toMatch(/translate-x/);
    expect(cls).not.toMatch(/h-screen/);
    expect(cls).not.toMatch(/w-\[420px\]/);
  });

  it('header X button calls onClose', () => {
    const onClose = vi.fn();
    renderWithProvider(<ScheduledPanel sessionId="s1" onClose={onClose} />);
    const btn = screen.getByRole('button', { name: /close scheduled panel/i });
    fireEvent.click(btn);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/zhanlu/frontend && npm run test -- src/components/chat/ScheduledPanel.test.jsx`

Expected: FAIL. Two failure modes (TDD red):
- First test: `expect(cls).not.toMatch(/\bfixed\b/)` etc. fail because the current `<aside>` carries `fixed right-0 top-0 z-40 h-screen w-[420px] ... shadow-2xl ... translate-x-full`.
- Second test: `expect(onClose).toHaveBeenCalledTimes(1)` fails because the current X button calls `onOpenChange?.(false)`, not `onClose`.

If the file fails to import (e.g. `authFetch` mock missing — different error), fix the mock and re-run. Do not proceed until BOTH tests fail with the expected contract mismatch.

- [ ] **Step 3: Commit the failing test**

This is a test-only commit. Per project convention, pause for user approval before committing. Suggested message:

```
test(chat): add failing tests for ScheduledPanel docked contract

TDD red. Gates the new contract (no overlay classes; X button calls
onClose). Will pass once ScheduledPanel is refactored to the docked
wrapper in the next task.
```

Do NOT continue to Task 2 until the user approves this commit.

---

### Task 2: Refactor `ScheduledPanel` to the docked contract

**Files:**
- Modify: `frontend/src/components/chat/ScheduledPanel.jsx`

**Interfaces (target signature):**
- `ScheduledPanel({ sessionId, focusExecutionId, onClose })` — drop `open` and `onOpenChange`.

- [ ] **Step 1: Update the component signature**

In `frontend/src/components/chat/ScheduledPanel.jsx`, change the default export signature from:

```jsx
export default function ScheduledPanel({ sessionId, open, onOpenChange, focusExecutionId }) {
```

to:

```jsx
export default function ScheduledPanel({ sessionId, focusExecutionId, onClose }) {
```

- [ ] **Step 2: Strip the overlay wrapper from the outer `<aside>`**

Replace the entire `<aside>` opening tag in the component's return (currently around the `className={...fixed right-0 top-0 z-40...}` template literal and its `aria-hidden={!open}` / `data-open` attributes) with:

```jsx
    <aside
      role="complementary"
      aria-label={isEn ? 'Scheduled automation' : '定时任务'}
      data-testid="scheduled-panel"
      className="flex h-full w-full flex-col border-l border-border bg-card"
    >
```

Keep the inner `<div className="flex h-full flex-col">` exactly as it is. Do not change any of the header / schedule config / body JSX.

- [ ] **Step 3: Update the header X button handler**

Find the X button in the header (the `MoreVertical` icon's neighbor) and change its `onClick` from:

```jsx
              onClick={() => onOpenChange?.(false)}
```

to:

```jsx
              onClick={() => onClose?.()}
```

Also update its `aria-label` from `Close scheduled panel` (already correct) — no change needed; verify it stays `Close scheduled panel`.

- [ ] **Step 4: Confirm no other references to `open` or `onOpenChange` remain**

Grep the file: `grep -n "onOpenChange\|\bopen\b" frontend/src/components/chat/ScheduledPanel.jsx`. Only matches should be unrelated (`open` as a substring in other words like `opened`, or in node_modules paths). No remaining usages of `onOpenChange` or the `open` prop. If any remain (e.g. a leftover `aria-hidden={!open}`), delete them.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd /root/zhanlu/frontend && npm run test -- src/components/chat/ScheduledPanel.test.jsx`

Expected: PASS (both tests green). Run the full frontend test suite to confirm no regressions in unrelated components:

Run: `cd /root/zhanlu/frontend && npm run test`

Expected: all tests pass.

- [ ] **Step 6: Lint**

Run: `cd /root/zhanlu/frontend && npm run lint`

Expected: no errors. Fix any lint warnings introduced by the signature change (e.g. unused-var if the linter flags the dropped `open`).

- [ ] **Step 7: Commit**

This is a code commit. Per project convention, pause for user approval. Suggested message:

```
refactor(chat): strip ScheduledPanel overlay wrapper (docked contract)

- Drop 'open' prop; rename 'onOpenChange' -> 'onClose' (matches ArtifactPanel).
- Replace fixed-overlay <aside> classes with 'flex h-full w-full flex-col
  border-l border-border bg-card' so the panel fills its <Panel> cell.
- X button now calls onClose?.().
- No behavioral change at the API surface used by the parent yet;
  Chat.jsx will be updated in the next commit.
```

Do NOT continue to Task 3 until the user approves this commit.

---

### Task 3: Integrate `scheduled-panel` into `Chat.jsx` `PanelGroup`

**Files:**
- Modify: `frontend/src/pages/Chat.jsx`

**Interfaces (target wiring):**
- New conditional PanelGroup child: `{scheduledOpen && (<>handle + <Panel id="scheduled-panel" order={2} defaultSize={30} minSize={22} maxSize={65}><ScheduledPanel sessionId={activeId} focusExecutionId={focusExecutionId} onClose={() => setScheduledOpen(false)} /></Panel></>)}`
- `<Panel id="artifact-panel" ...>`: `order` bumped from `2` to `3`.
- Standalone overlay mount at the bottom of `Chat()`: removed.

- [ ] **Step 1: Bump `artifact-panel` `order` from `2` to `3` AND drop `defaultSize` from `40` to `30`**

In `frontend/src/pages/Chat.jsx`, find this line (currently line 2079):

```jsx
            <Panel id="artifact-panel" order={2} defaultSize={40} minSize={20} maxSize={75}>
```

Change `order={2}` to `order={3}` AND `defaultSize={40}` to `defaultSize={30}`. `minSize={20}` and `maxSize={75}` are unchanged. The `defaultSize` drop (user tweak, 2026-07-30) makes the three-panel split match the spec defaults (chat=40 / scheduled=30 / artifact=30); the trade-off is that artifact-panel gets a smaller default share when opened without scheduled (chat=70 / artifact=30 instead of 60/40).

- [ ] **Step 2: Insert the conditional `scheduled-panel` fragment**

Find the chat panel's closing tag (currently line 2075, `</Panel>` directly after the chat column's inner `</div>`). Immediately after that closing `</Panel>` and BEFORE the existing `{previewResult && (...)` block (currently line 2076), insert:

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

Use the SAME indentation as the surrounding `{previewResult && (...)}` block so the JSX stays aligned.

- [ ] **Step 3: Remove the standalone overlay mount**

Find and delete this single line (currently line 2097, just before the final `</div>` of `Chat()`):

```jsx
        <ScheduledPanel sessionId={activeId} open={scheduledOpen} onOpenChange={setScheduledOpen} focusExecutionId={focusExecutionId} />
```

Verify no remaining references to the old `open` / `onOpenChange` props on `ScheduledPanel` anywhere in `Chat.jsx`: `grep -n "ScheduledPanel" frontend/src/pages/Chat.jsx`. Only the new conditional mount (from Step 2) should match.

- [ ] **Step 4: Build to catch syntax / import errors**

Run: `cd /root/zhanlu/frontend && npm run build`

Expected: build succeeds with no errors. Warnings about chunk size are normal and acceptable.

- [ ] **Step 5: Lint**

Run: `cd /root/zhanlu/frontend && npm run lint`

Expected: no errors.

- [ ] **Step 6: Run the full frontend test suite**

Run: `cd /root/zhanlu/frontend && npm run test`

Expected: all tests pass, including `ScheduledPanel.test.jsx` from Task 1 and the pre-existing `RunChecklist.test.jsx` / `RunFailureActions.test.jsx`.

- [ ] **Step 7: Commit**

This is a code commit. Per project convention, pause for user approval. Suggested message:

```
feat(chat): dock ScheduledPanel as right-side panel (match ArtifactPanel)

- Mount ScheduledPanel conditionally inside the existing PanelGroup,
  between the chat panel and artifact-panel (visual: chat | scheduled
  | artifact | preview).
- Bump artifact-panel 'order' 2 -> 3 so the new scheduled-panel
  (order={2}) sits next to the chat; artifact-preview-panel stays
  order={4}.
- Remove the standalone fixed-overlay <ScheduledPanel> mount.
- Pass onClose={() => setScheduledOpen(false)} so the header X button
  closes the docked panel by setting parent state (same pattern as
  ArtifactPanel's onClose).

Combined with the prior component refactor, ScheduledPanel now behaves
like ArtifactPanel: docked, resizable, part of the layout — no more
popup overlay.
```

Do NOT continue to Task 4 until the user approves this commit.

---

### Task 4: Manual smoke verification (live, vs running backend)

**Files:** none (verification only).

- [ ] **Step 1: Rebuild frontend dist and restart backend if needed**

Run: `/opt/rebuild-zhanlu.sh --force`

Expected: vite build succeeds; nginx serves the new bundle. (Backend does not need a restart — no backend changes.)

- [ ] **Step 2: Verify docked behavior in the browser**

Open the chat UI at `http://zhanlu.ai:8000` and log in as `admin@zhanlu.dev` / `admin123` (per project E2E convention; the `E2E_ADMIN_PASS=Test1234!` default in some test files is WRONG).

1. Open a chat session that is linked to a scheduled automation task.
2. Click the clock toggle in the chat header. **Expected**: the Scheduled panel appears docked on the right side of the layout. **No** floating overlay, **no** drop shadow, **no** slide-in animation.
3. Click the X button in the Scheduled panel header. **Expected**: the panel disappears and the chat expands to fill the freed space.
4. Reopen the Scheduled panel. Trigger an "Open run" link from a chat message (if present). **Expected**: the panel opens AND the matching run row is auto-expanded (existing `focusExecutionId` behavior preserved).
5. Drag the resize handle between the chat and the Scheduled panel. **Expected**: both columns resize smoothly.
6. Open an artifact preview (e.g. click a `.docx` artifact in a chat message). **Expected**: the `artifact-panel` mounts to the RIGHT of the Scheduled panel — both visible side-by-side, both resizable.

If any step fails, do NOT commit further fixes silently — capture a screenshot, check the browser console for errors, and revisit Task 3.

- [ ] **Step 3: Confirm no code changes were needed**

This task is verification only. If the smoke passes, the implementation is complete. If a smoke step reveals a real defect, add a follow-up task (do not silently patch in this task) and loop back through Tasks 2–3 as needed.

---

## Self-Review (against the spec)

**1. Spec coverage:**
- §1 Problem (overlay wrapper) → Task 2 strips it. ✅
- §2 Goals: docked panel → Tasks 2 + 3. ✅ Side-by-side coexistence → Task 3. ✅ X/toggle preserved → Task 3. ✅ Internal logic untouched → Task 2 only changes wrapper/signature. ✅
- §3 Decisions: side-by-side, order chat→scheduled→artifact→preview, conditional mount, defaults/min sizes → Task 3 (with `minSize={22}` for scheduled; chat `minSize={30}` and artifact `minSize={20}` kept as-is to minimize unrelated changes — see Global Constraints note). ✅
- §5.1 Chat.jsx integration → Task 3 Steps 1–3. ✅
- §5.2 ScheduledPanel.jsx strip overlay → Task 2 Steps 2–4. ✅
- §5.3 API change (`open` drop, `onOpenChange`→`onClose`) → Task 2 Step 1 + Step 3. ✅
- §5.4 unchanged header/body/interactions → Task 2 Step 4 verification (grep). ✅
- §8 Testing: frontend vitest for the contract → Task 1 + Task 2 Step 5. ✅ Manual smoke → Task 4. ✅

**2. Placeholder scan:** No "TBD"/"TODO"/"similar to"/"add validation". Every step contains the actual content (exact file paths, exact code, exact commands, exact expected results). The single qualitative instruction is Step 4 of Task 3 ("Verify no remaining references") and Step 1 of Task 4 ("rebuild frontend dist") — both reference concrete commands. ✅

**3. Type / name consistency:**
- `ScheduledPanel` signature `{ sessionId, focusExecutionId, onClose }` defined in Task 2 Step 1 and used in Task 3 Step 2. ✅
- `scheduled-panel` `id` consistent across Task 3 Steps 2 and Step 3 grep. ✅
- `setScheduledOpen` referenced in Task 3 Step 2 (closure) and matches existing state in Chat.jsx. ✅
- `onClose` prop used identically in Task 1 test, Task 2 Step 3, and Task 3 Step 2. ✅
- `artifact-panel` `order={2}→{3}` AND `defaultSize={40→30}` (user tweak 2026-07-30) consistent in Task 3 Step 1 and the rationale note. ✅

**4. Scope:** Single logical change, frontend-only, one commit boundary (Tasks 2 + 3 ship together; Task 1 is a test-only commit that may or may not be wanted — see per-task pause notes). Appropriately scoped for a single plan. ✅