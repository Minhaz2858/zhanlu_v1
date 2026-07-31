# Skill Agent Empty State — Chat-Style Landing

**Status:** Approved, **revision 2** (2026-07-28 — "Or edit an existing skill" divider removed)
**Date:** 2026-07-28
**Scope:** `frontend/src/pages/SkillAgent.jsx`, `frontend/src/lib/translations.js`
**Out of scope:** file panel, success state, header, permission mode, Toolkit entry, route paths, backend, Agent Builder, **`ExistingSkillsChips` integration in the empty state** (removed in rev 2 — see §3.4)

---

## 1. Problem

The current empty state of `SkillAgent.jsx` (lines 386-405) is a heavy marketing-style landing:

- 56×56 `Wrench` icon box (`bg-primary/10`)
- `<h1>` "Skill Agent" at `text-3xl`
- ~140-character description paragraph
- "Made by Minha" line
- A 2-column-feeling stack of **four large suggestion cards** (Collect / Create / Learn / Edit), each a full-width row with icon + long sentence

The result is: **a lot to read before a first-time user can type anything**. New users have to scan, decide, click a card. Existing users have to look past the same block every time they open the page to get to the input.

The wait-for-input refactor (spec 2026-07-28-skill-agent-wait-for-input) already moved the four cards from auto-send to `applySuggestion` (prefill + focus). So the cards no longer need to "sell" the action — they just need to be fast shortcuts to a typed prompt.

---

## 2. Goal

Modernize the empty state to a **chat-style landing** (rev 2):

- The **input is the first thing the eye lands on** (just like modern chat UIs: ChatGPT, Claude, Gemini, Linear, Notion AI).
- The four suggestion cards collapse to a row of **4 short chips** (2-3 words each) below the input — the "try one of these" pattern.
- **Rev 2 (2026-07-28):** the originally planned "Or edit an existing skill" divider + `<ExistingSkillsChips>` row was removed per user feedback after the first build (see §3.4). The empty state now ends at the 4 quick-start chips.

The visual hierarchy becomes: **input → quick-start chips** (and that's it).

---

## 3. Design

### 3.1 New empty-state layout (replaces lines 386-405)

```text
┌──────────────────────────────────────────────────────────┐
│  🔧  Skill Agent                                          │
│      Create, edit, and discover skills.                  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  (input area — same renderInputArea(true) as today) │  │
│  │  [textarea + SkillUploadButton + Send button]      │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  Try one of these:                                       │
│  [ Weekly report ] [ PDF summarizer ]                    │
│  [ Code reviewer  ] [ Import from GitHub ]               │
└──────────────────────────────────────────────────────────┘
```

- **Top row**: small `Wrench` icon (20px, `text-primary`, no box) + title `<h1>` at `text-2xl` + one-line subtitle (`text-sm text-muted-foreground`). No `madeBy` line, no `description` paragraph.
- **Input**: moved to render **first** under the title row, using the existing `renderInputArea(true)` (compact mode, same as today).
- **Quick chips** row: `text-[11px] uppercase tracking-wide text-muted-foreground` label + 2×2 grid of pill-shaped buttons (rounded-md, border, hover:bg-secondary/60). Clicking calls `applySuggestion(chipText)` — same helper as the existing cards.

### 3.2 Quick-start chip content (4 chips)

| # | English | 中文 |
|---|---------|------|
| 1 | Weekly report | 周报生成器 |
| 2 | PDF summarizer | PDF 摘要器 |
| 3 | Code reviewer | 代码审查助手 |
| 4 | Import from GitHub | 从 GitHub 导入 |

Each chip is a **placeholder prompt that gets pre-filled into the input**, e.g. clicking "Weekly report" puts the literal text `"Weekly report"` in the textarea. The user can then type more (e.g. "Weekly report for the engineering team, last 7 days, with a risks section") and press Enter.

`Import from GitHub` keeps the existing GitHub-collection flow alive — the user can follow up with a URL.

### 3.3 `applySuggestion` behavior (unchanged)

The `applySuggestion(text)` helper added in the wait-for-input refactor is reused as-is. It:

1. `setInput(text)`
2. `setPrefilledHint(true)` — shows the dismissible `<PrefilledHintPill>`
3. `inputRef.current?.focus()`

No auto-send, no conversation creation. User must press Enter (or the send button) to actually start the chat.

### 3.4 Revision 2 (2026-07-28): "Or edit an existing skill" divider — **not added**

The original revision 1 of this spec included a second row, separated by a `border-t` divider, that surfaced the user's existing skills via the `<ExistingSkillsChips>` component (re-imported from the wait-for-input refactor). After implementing rev 1 and viewing the result, the user asked to **remove the whole divider**:

> "@image don't need to show this"

The reasons (as observed in the screenshot):

- The user's `Tool` list contained duplicate rows (the same skill name appeared multiple times — e.g. `synexia-report-generator` x2, `ppt-maker` x2, `data-analysis` x2, `report-generator` x2). The component currently does not dedupe.
- Even without the duplicates, a row of existing-skill chips on the empty state added visual noise to what was meant to be a minimal "input first" landing.

**What this revision removes (vs. the original plan):**

- The `<div className="mt-6 w-full max-w-2xl border-t border-border pt-4">` block from `SkillAgent.jsx`.
- The `import ExistingSkillsChips from '@/components/skillagent/ExistingSkillsChips';` line.
- The `ExistingSkillsChips` import + its `<ExistingSkillsChips onError={...} />` JSX usage in the empty state.

**What this revision does NOT change:**

- The `<ExistingSkillsChips>` component itself — it stays in `frontend/src/components/skillagent/ExistingSkillsChips.jsx` and its test file stays. It can still be used elsewhere later.
- The `t.skillAgent.existingSkills` and `t.skillAgent.orEdit` keys — they stay in `translations.js` for i18n parity / future use, same reasoning as `description` / `madeBy`.
- Any other part of the empty state (title row, input, quick chips).

**Test strategy revision:**

- The original `SkillAgent.emptyState.test.jsx` was going to assert that `<ExistingSkillsChips>` is imported and that the divider is present. In rev 2, those assertions are dropped and replaced with a **negative-assertion test** that pins the **absence** (`expect(SOURCE).not.toMatch(/t\.skillAgent\.orEdit/);` and `expect(SOURCE).not.toMatch(/<ExistingSkillsChips\b/);`). This makes any future re-add intentional and visible in code review.

---

## 4. Removed from the empty state

- The 56×56 `Wrench` icon **box** (`bg-primary/10` rounded-2xl). The icon remains but inline with the title at 20px.
- The 3xl `<h1>`. Replaced with a 2xl inline title.
- The ~140-char `t.skillAgent.description` paragraph.
- The "Made by Minha" `t.skillAgent.madeBy` line.
- The 4 stacked suggestion cards (Collect / Create / Learn / Edit). The `SUGGESTIONS` array stays in source for now (still used by the test that pins `applySuggestion` on suggestion-card onClick) — we'll either remove the array or repoint the test to the new chip row.

**Translation keys to keep but stop using in the empty state**: `description`, `madeBy`. Leave them in `translations.js` (other code paths / future use may still reference them) — the cost of leaving them is zero, and removing them risks breaking the i18n contract for any cached user locale.

---

## 5. Behavior matrix

| Action | Before | After (rev 2) |
|---|---|---|
| Page load, no params | Shows landing with 4 suggestion cards | Shows landing with input first, then 4 quick-start chips. **No "or edit" divider.** |
| Click a quick-start chip (e.g. "Weekly report") | n/a (chips didn't exist) | `applySuggestion(chipText)` → input prefilled, pill shown, input focused |
| Click existing-skill chip | n/a (divider not in empty state in rev 2; the `ExistingSkillsChips` component itself is unused) | n/a — to open an existing skill, use the sidebar's session list (AgentConversation rows from before the wait-for-input refactor are still merged) or the Toolkit page |
| URL `?action=create` | `applySuggestion(t.skillAgent.suggestions.create)` | **No change** (still uses the long `create` suggestion text) |
| URL `?skill=X` | `startWithSkill(skillId)` (auto-send) | **No change** |
| URL `?files=…` | `startWithFiles(urls)` (auto-send) | **No change** |
| Press Enter on empty-state input | `startNewChat(input)` (auto-send) | **No change** |
| Press Enter on active-state input | `handleSend(input)` | **No change** |

The `?action=create` deep link (Toolkit's "Build with Agent" button) still uses the long `t.skillAgent.suggestions.create` text, so the user lands with the full "Create a new skill, e.g. a report generation tool" prefill. **If we want `?action=create` to also feel "new", we can switch it to one of the 4 chip texts ("Weekly report")** — recommended, see §7 follow-up.

---

## 6. Files changed (rev 2)

- `frontend/src/pages/SkillAgent.jsx` — replace the empty-state JSX block (lines 386-405). **No** `ExistingSkillsChips` import in rev 2 (the divider was removed before the import landed). The `SUGGESTIONS` array stays for test pinning.
- `frontend/src/lib/translations.js` — add new keys (zh + en):
  - `skillAgent.subtitle` — short tagline
  - `skillAgent.tryOne` — "Try one of these:" / "试试这些："
  - `skillAgent.chips.weekly` / `.pdf` / `.code` / `.import` — chip labels
  - `skillAgent.orEdit` — kept for i18n parity even though it's unused in rev 2; safe to remove in a follow-up.

- `frontend/src/pages/SkillAgent.emptyState.test.jsx` (NEW) — source-scan regression test pinning:
  - No `bg-primary/10` rounded-2xl icon box
  - No `text-3xl` `<h1>`
  - No `t.skillAgent.description` or `t.skillAgent.madeBy` reference in the empty state
  - `renderInputArea(true)` is called
  - The 4 chip texts are present in JSX (e.g. `t.skillAgent.chips.weekly`)
  - **No** `t.skillAgent.orEdit` reference and **no** `<ExistingSkillsChips>` JSX (negative assertions pin the rev-2 absence)

- `frontend/src/pages/SkillAgent.applySuggestion.test.jsx` — **update** the suggestion-card test (it pins the old 4-card click handler). Repoint the test to verify the chip-row click handler calls `applySuggestion`.

---

## 7. Follow-ups (out of scope for this iteration)

- Switch `?action=create` to use one of the 4 chip texts (`t.skillAgent.chips.weekly`) so the Toolkit's "Build with Agent" button lands with a tighter prefill.
- Consider whether `t.skillAgent.description` and `t.skillAgent.madeBy` should finally be removed from `translations.js` (we left them here to avoid surprise breakage; revisit next iteration).
- A11y: the new chip row should have `role="group"` and an `aria-label` (covered by the visible "Try one of these:" label, but a screen reader benefit from `aria-labelledby` would be a 1-line addition).
- Animate the input area in on first mount (subtle fade-in) — defer.
