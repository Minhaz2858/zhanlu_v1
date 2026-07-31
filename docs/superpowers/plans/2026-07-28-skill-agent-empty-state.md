# Skill Agent Empty State — Chat-Style Landing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Modernize the Skill Agent empty state to a chat-style landing: input first, then 4 short quick-start chips, then an "Or edit an existing skill:" divider with the user's existing skills.

**Spec:** `docs/superpowers/specs/2026-07-28-skill-agent-empty-state-design.md`
**Architecture:** Pure-frontend JSX refactor. The `applySuggestion` helper (added in the wait-for-input refactor) is reused as-is — it already prefills + focuses + shows the dismissible pill. The 4 long suggestion cards collapse to 4 short chips. **Revision 2 (2026-07-28, post-build):** the originally planned "Or edit an existing skill" divider using `ExistingSkillsChips` was removed per user feedback after the first build surfaced duplicate-skill rows and visual noise. The empty state now ends at the 4 quick-start chips.

**Tech Stack:** React 18, Vite, Tailwind, lucide-react, vitest + jsdom (source-string scan only — no RTL).

---

## Global Constraints

- **No backend changes.** All edits are under `frontend/src/`.
- **No new test framework.** Use the existing vitest source-string scan pattern (see `src/pages/SkillAgent.applySuggestion.test.jsx` for the precedent). Do not introduce RTL rendering tests for this page — the mocking surface is too large.
- **Test command:** `npx vitest run <path>` (never `npm test` with no args — broad collection risks OOM on the dev box per project memory).
- **Build command:** `cd frontend && ./node_modules/.bin/vite build` (run only in Task 7, not per-task — it takes 30-60s and spikes memory).
- **Translation parity:** Every new key must be added to BOTH the `zh` and `en` blocks in `translations.js`.
- **Branch:** Work is committed to the current branch `feat/auth-hardening` (the user is already working on it; the work is intentionally additive and does not need its own branch).
- **Do not touch** the uncommitted `frontend/src/pages/AutomationTasks.jsx` changes — they are unrelated and belong to a different workstream.

---

## File Structure

**Modified files:**
- `frontend/src/lib/translations.js` — add new i18n keys (zh block: `skillAgent` ~line 442; en block: ~line 1018).
- `frontend/src/pages/SkillAgent.jsx` — rewrite the empty-state JSX block (lines 386-405). **No** `ExistingSkillsChips` import in rev 2 (the divider was removed before the import landed).
- `frontend/src/pages/SkillAgent.applySuggestion.test.jsx` — repoint the suggestion-card test to the new chip-row onClick.

**New files:**
- `frontend/src/pages/SkillAgent.emptyState.test.jsx` — source-scan regression test pinning the new structure (no big icon box, no `text-3xl` h1, no description/madeBy, `renderInputArea(true)` called, 4 chip texts present, **no** `orEdit` / **no** `<ExistingSkillsChips>` — negative assertions pin the rev-2 absence).

---

## Task 1: Add i18n keys to translations.js

**Files:**
- Modify: `frontend/src/lib/translations.js` (zh block: `skillAgent` at line 442; en block: `skillAgent` at line 1018).

**Interfaces:** add 4 new keys to BOTH the `zh` and `en` `skillAgent` blocks:

- `subtitle` — short one-line tagline shown under the title.
- `tryOne` — "Try one of these:" / "试试这些：".
- `chips.weekly` / `chips.pdf` / `chips.code` / `chips.import` — 4 short chip labels.
- `orEdit` — "Or edit an existing skill:" / "或编辑已有技能：".

Suggested values (final copy can be tuned by the user; these match the brainstorm):

```js
// ZH
subtitle: '创建、编辑和发现技能。',
tryOne: '试试这些：',
chips: {
  weekly: '周报生成器',
  pdf: 'PDF 摘要器',
  code: '代码审查助手',
  import: '从 GitHub 导入',
},
orEdit: '或编辑已有技能：',

// EN
subtitle: 'Create, edit, and discover skills.',
tryOne: 'Try one of these:',
chips: {
  weekly: 'Weekly report',
  pdf: 'PDF summarizer',
  code: 'Code reviewer',
  import: 'Import from GitHub',
},
orEdit: 'Or edit an existing skill:',
```

- [ ] **Step 1: Open the file and locate both `skillAgent` blocks** (zh at line 442, en at line 1018). Verify they are in fact separate objects.
- [ ] **Step 2: Add the 4 new keys to the ZH block**, immediately after the `title:` line, so they read as a group: `subtitle`, `tryOne`, `chips: {...}`, `orEdit`. Keep the existing `description`, `madeBy`, `placeholder`, `prefilledHint`, `or`, `emptyUploadCta`, `emptyUploadHint`, `existingSkills`, `backToToolkit` untouched.
- [ ] **Step 3: Mirror the same 4 keys in the EN block.**
- [ ] **Step 4: Verify parity** by running `grep -n "chips:" frontend/src/lib/translations.js` — should match exactly 2 lines (zh + en).

---

## Task 2: Rewrite the empty state in SkillAgent.jsx

**Files:**
- Modify: `frontend/src/pages/SkillAgent.jsx` (the JSX block currently at lines 386-405; plus the import list at lines 1-11).

  - [ ] **Step 1: Replace lines 386-405** (the entire `showEmpty ? (...) : (...)` truthy branch) with the new chat-style layout. New JSX (drop-in replacement, copy exactly):

  ```jsx
  {showEmpty ? (
    <div className="flex flex-1 flex-col items-center justify-center px-6 pb-12">
      <div className="mb-6 flex w-full max-w-2xl items-center gap-2">
        <Wrench className="h-5 w-5 text-primary" />
        <h1 className="font-display text-2xl tracking-tight text-foreground">{t.skillAgent.title}</h1>
        <span className="ml-1 text-sm text-muted-foreground">{t.skillAgent.subtitle}</span>
      </div>
      <div className="w-full max-w-2xl">
        {renderInputArea(true)}
      </div>
      <div className="mt-6 w-full max-w-2xl">
        <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {t.skillAgent.tryOne}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {[
            { key: 'weekly', text: t.skillAgent.chips.weekly },
            { key: 'pdf', text: t.skillAgent.chips.pdf },
            { key: 'code', text: t.skillAgent.chips.code },
            { key: 'import', text: t.skillAgent.chips.import },
          ].map((chip) => (
            <button
              key={chip.key}
              type="button"
              onClick={() => applySuggestion(chip.text)}
              className="inline-flex items-center rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground transition hover:border-primary/40 hover:bg-secondary/60"
            >
              {chip.text}
            </button>
          ))}
        </div>
      </div>
    </div>
  ) : (
    /* unchanged active-state JSX */
    <>
  ```

  Important: keep the closing `</>` and the rest of the active-state branch **byte-identical** to the current code. Only the inside of the `showEmpty` branch changes. **Rev 2 note:** the original plan included an "Or edit an existing skill" divider with `<ExistingSkillsChips>` between the chip row and the closing `</div>`. That block is intentionally omitted here per the user's post-build feedback (see spec §3.4).

- [ ] **Step 2: Leave the `SUGGESTIONS` array (lines 14-19) in place.** It is still referenced by the test (which we'll update in Task 3). Removing it is a follow-up.
- [ ] **Step 3: Visually confirm** with a search that no other code path in `SkillAgent.jsx` uses `t.skillAgent.description` or `t.skillAgent.madeBy` (we are not removing the keys from translations, just stopping the empty state from rendering them): `grep -n "t.skillAgent.description\|t.skillAgent.madeBy" frontend/src/pages/SkillAgent.jsx` — should return nothing in the empty-state area, and any remaining references elsewhere in the file are out of scope.

---

## Task 3: Update / add tests

**Files:**
- Modify: `frontend/src/pages/SkillAgent.applySuggestion.test.jsx` — repoint the suggestion-card test.
- Create: `frontend/src/pages/SkillAgent.emptyState.test.jsx` — new source-scan regression test.

### 3a. Update `SkillAgent.applySuggestion.test.jsx`

- [ ] **Step 1: Replace the `it('suggestion card onClick calls applySuggestion, NOT startNewChat', …)` test body** (currently looks for the 4-card onClick) with a test that pins the new chip-row onClick. The new test should assert that the JSX contains four chip buttons wired to `applySuggestion`. A robust check is to scan for the literal `chip.text` mapping (proves the array is rendered) and the four `t.skillAgent.chips.*` references (proves the right keys are used):

  ```js
  it('quick-start chip onClick calls applySuggestion (pre-fill + focus, no auto-send)', () => {
    // The empty state maps 4 chips and wires each onClick to applySuggestion.
    expect(SOURCE).toMatch(/applySuggestion\s*\(\s*chip\.text\s*\)/);
    // The 4 chip keys from translations are referenced.
    expect(SOURCE).toMatch(/t\.skillAgent\.chips\.weekly/);
    expect(SOURCE).toMatch(/t\.skillAgent\.chips\.pdf/);
    expect(SOURCE).toMatch(/t\.skillAgent\.chips\.code/);
    expect(SOURCE).toMatch(/t\.skillAgent\.chips\.import/);
    // The old 4-card array literal is gone (Collect / Create / Learn / Edit cards removed from the empty state).
    expect(SOURCE).not.toMatch(/SUGGESTIONS\.map\(\(s\)\s*=>/);
    // The deferSend escape hatch is gone (no suggestion auto-sends any more).
    expect(SOURCE).not.toMatch(/deferSend:/);
  });
  ```

  Leave the other 6 tests in the file untouched (they pin `applySuggestion` itself, the `?action=create` branch, the intentional auto-send paths, the `PrefilledHintPill`, and the `prefilledHint` clearing on input change — all of which are preserved).

### 3b. Create `SkillAgent.emptyState.test.jsx`

- [ ] **Step 2: Create the new test file** at `frontend/src/pages/SkillAgent.emptyState.test.jsx` with the following content:

  ```jsx
  /**
   * Regression: SkillAgent.jsx empty state is chat-style (input first,
   * quick-start chips, "or edit an existing skill" divider using
   * ExistingSkillsChips). See spec
   * 2026-07-28-skill-agent-empty-state-design.md §3.1.
   */

  import { describe, it, expect } from 'vitest';
  import { readFileSync } from 'fs';
  import { fileURLToPath } from 'url';
  import { dirname, resolve } from 'path';

  const __filename = fileURLToPath(import.meta.url);
  const __dirname = dirname(__filename);
  const SOURCE = readFileSync(resolve(__dirname, './SkillAgent.jsx'), 'utf8');

  describe('SkillAgent.jsx — chat-style empty state (rev 2)', () => {
    it('renders the input area first in the empty state', () => {
      // renderInputArea(true) is the compact input used in the empty state.
      expect(SOURCE).toMatch(/renderInputArea\(true\)/);
    });

    it('no large icon box in the empty state', () => {
      // The old 56×56 Wrench icon box used h-14 w-14 rounded-2xl bg-primary/10.
      expect(SOURCE).not.toMatch(/h-14\s+w-14\s+rounded-2xl\s+bg-primary\/10/);
    });

    it('no text-3xl h1 (title shrunk to 2xl)', () => {
      expect(SOURCE).not.toMatch(/text-3xl/);
    });

    it('does not render description or madeBy in the empty state', () => {
      // We don't reference these keys from JSX any more.
      // (Keys are still in translations.js — the spec leaves them for safety.)
      expect(SOURCE).not.toMatch(/t\.skillAgent\.description/);
      expect(SOURCE).not.toMatch(/t\.skillAgent\.madeBy/);
    });

    it('shows the "Try one of these:" label and 4 quick-start chips', () => {
      expect(SOURCE).toMatch(/t\.skillAgent\.tryOne/);
      for (const k of ['weekly', 'pdf', 'code', 'import']) {
        expect(SOURCE).toMatch(new RegExp(`t\\.skillAgent\\.chips\\.${k}`));
      }
    });

    it('does NOT show the "Or edit an existing skill" divider (rev 2 removal)', () => {
      // The divider was removed per user feedback after the first build. Pin the
      // absence so any future re-add is intentional and visible in code review.
      expect(SOURCE).not.toMatch(/t\.skillAgent\.orEdit/);
      expect(SOURCE).not.toMatch(/<ExistingSkillsChips\b/);
    });

    it('shows the new subtitle next to the title', () => {
      expect(SOURCE).toMatch(/t\.skillAgent\.subtitle/);
    });
  });
  ```

---

## Task 4: Run vitest on the affected test files

- [ ] **Step 1: Run the two updated/new test files:**

  ```bash
  cd /root/zhanlu/frontend && npx vitest run \
    src/pages/SkillAgent.applySuggestion.test.jsx \
    src/pages/SkillAgent.emptyState.test.jsx
  ```

  All tests must pass. If any test fails, **stop and fix the source/test until it passes** — do not paper over with `it.skip`.

- [ ] **Step 2: Also run the existing `ExistingSkillsChips.test.jsx`** to make sure re-importing the component from the page doesn't regress its contract:

  ```bash
  npx vitest run src/components/skillagent/ExistingSkillsChips.test.jsx
  ```

---

## Task 5: Run the full frontend lint

- [ ] **Step 1: Run eslint over the changed files:**

  ```bash
  cd /root/zhanlu/frontend && npx eslint src/pages/SkillAgent.jsx src/lib/translations.js
  ```

  No errors. Warnings are fine; fix any error that points at the new code.

---

## Task 6: Run vite build (only if everything above is green)

- [ ] **Step 1: Build the frontend to confirm the JSX/imports compile cleanly:**

  ```bash
  cd /root/zhanlu/frontend && ./node_modules/.bin/vite build
  ```

  This takes 30-60s and spikes memory. If it OOMs, run again with `NODE_OPTIONS=--max-old-space-size=4096` and stop. If it still OOMs, stop and report to the user — the dev box may need a restart.

- [ ] **Step 2: Confirm the build artifact landed in `frontend/dist/`.**

---

## Task 7: Visual smoke test (optional, recommended)

- [ ] **Step 1: Start the dev server:** `cd /root/zhanlu/frontend && ./node_modules/.bin/vite --host 0.0.0.0 --port 5173` (in a backgrounded shell or `nohup`).
- [ ] **Step 2: Open the page** at the route `/skill-agent` and confirm:
  - The Wrench icon is small (20px), inline with the "Skill Agent" title.
  - The subtitle reads "Create, edit, and discover skills.".
  - The textarea is the first interactive element.
  - Four chips render below the input: "Weekly report", "PDF summarizer", "Code reviewer", "Import from GitHub".
  - Clicking a chip prefills the input and shows the suggestion-applied pill.
  - If the user has skills, the "Or edit an existing skill:" divider shows below the chips with up to 8 skill chips.
- [ ] **Step 3: Stop the dev server** (`kill <pid>`).

---

## Task 8: Commit

- [ ] **Step 1: Stage only the files this plan touched** (do not commit the unrelated `AutomationTasks.jsx` changes):

  ```bash
  git add docs/superpowers/specs/2026-07-28-skill-agent-empty-state-design.md \
          docs/superpowers/plans/2026-07-28-skill-agent-empty-state.md \
          frontend/src/lib/translations.js \
          frontend/src/pages/SkillAgent.jsx \
          frontend/src/pages/SkillAgent.applySuggestion.test.jsx \
          frontend/src/pages/SkillAgent.emptyState.test.jsx
  ```

- [ ] **Step 2: Commit with a descriptive message:**

  ```bash
  git commit -m "feat(skill-agent): chat-style empty state (input first, quick-start chips, edit-existing divider)"
  ```

---

## Self-Review Checklist (run before finishing)

- [ ] All 4 new i18n keys present in BOTH `zh` and `en` blocks.
- [ ] `SkillAgent.jsx` empty state matches the rev-2 layout in §3.1 of the spec (input first, chips — **no** divider, **no** `ExistingSkillsChips`).
- [ ] `applySuggestion` is reused (not duplicated / not replaced).
- [ ] Intentional auto-send paths preserved: `?skill=`, `?files=`, Enter-on-empty.
- [ ] `SUGGESTIONS` array kept (don't break the test that pins the old card-onClick test until the test is replaced — which it is in Task 3).
- [ ] No `text-3xl`, no `t.skillAgent.description`, no `t.skillAgent.madeBy` references in `SkillAgent.jsx`.
- [ ] `npx vitest run` on the 3 affected test files: all green.
- [ ] `npx eslint` on the changed files: no errors.
- [ ] `vite build` succeeds.
- [ ] No changes to `AutomationTasks.jsx` or other unrelated files.
