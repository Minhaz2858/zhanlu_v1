# Skill Agent Bare-Request — Multi-Select `:::options` Block

**Status:** Approved (brainstorming 2026-07-28)
**Date:** 2026-07-28
**Scope:** `backend/app/services/agent_prompts.py`, `frontend/src/components/agent/AgentMarkdown.jsx`, `frontend/src/components/skillagent/SkillMessageBubble.jsx`, plus 1 new test file (frontend) and 1 new test case (backend)
**Out of scope:** Agent Builder, Automation, file panel, header, permission mode, route paths, the `:::decision-summary` block, any change to the `search_skills` budget or the no-stop-phrasing rule (both already fixed 2026-07-28 in the previous turn).

---

## 1. Problem

When a user submits a bare request like "Code reviewer", "Weekly report", or "PDF summarizer" to the Skill Agent, the agent currently:

1. Calls `search_skills` once (fixed in the previous turn).
2. Emits **3 separate `:::options` blocks** (audience, format, focus), each with 3-4 chips. The user has to click one chip per question and send **three** separate messages before the agent starts building.

Observed in the user's screenshot from 2026-07-28: a single "Code reviewer" bare request produced **12 chips across 3 questions** before the agent would commit to anything. The agent is technically asking the right questions, but the multi-round UX is high-friction:

- The user has to read each question in turn and decide.
- Each question requires a send → wait → read → send cycle.
- A wrong pick on Q1 is hard to recover from (the agent commits and moves on).
- Most users would be happy with the agent's first instinct — they just want to override ONE thing, not answer three questions.

The current prompt at `backend/app/services/agent_prompts.py` lines 543-568 says **"Ask 2-3 focused questions"**, which is what produces the 3-block pattern.

---

## 2. Goal

Replace the 3-block "ask 2-3 questions" pattern with a single, more decisive flow:

- The agent **commits to a complete default** in one sentence (audience + format + focus + trigger).
- The agent emits **one** `:::options` block with 8-12 toggleable chips covering 3-4 dimensions.
- Each chip is **self-labeling** via a dimension prefix (`Audience: Engineers`, `Format: Inline comments`).
- The user can click 0+ chips to override; a **"Use these (N)"** button appears when N≥1 and fills the chat input with the selected chips as comma-separated text.
- The user reviews the text, optionally edits, and presses Enter to send — **one** send instead of three.
- If the user sends with no chips selected (or just types a custom reply), the agent uses its pre-stated defaults.

The visual hierarchy becomes: **stated defaults → one multi-select chip block → review-and-send in the input box**.

---

## 3. Design

### 3.1 New bare-request flow (replaces lines 543-568 of `agent_prompts.py`)

For a bare request like "Code reviewer", the agent's reply now looks like this (rendered):

> I'll build a code-reviewer skill with these defaults: **Engineers / Inline comments / Security-focused**. Pick any to change, or press Send to use my picks.
>
> ```
> :::options
> Audience: Individual dev
> Audience: Team lead
> Audience: Open-source maintainer
> Format: Inline comments
> Format: Structured report
> Format: Scoring checklist
> Focus: Security (OWASP, injection, auth)
> Focus: Performance (complexity, DB, caching)
> Focus: Quality & style (lint, naming, DRY)
> :::
> ```
>
> *(The chat UI renders the `:::options` block as 9 toggleable chips. The user clicks 0+ of them. When at least 1 is selected, a "Use these (N)" button appears below the chip group. Clicking it fills the chat input with the comma-separated selected text — e.g. `Audience: Individual dev, Focus: Security (OWASP, injection, auth)`. The user reviews, optionally edits, and presses Enter to send.)*

The agent's next turn (after the user sends the selections or an empty / custom message) is to write the `skill_md` and call `create_skill` — no second clarifying question round.

### 3.2 Prompt changes (exact text)

#### 3.2.1 Replace the "Bare-Request Handling" section in `agent_prompts.py` (lines 543-568)

Current text:

> A bare request is one where the user has named a capability but said nothing about its audience, output, cadence, or trigger conditions. Examples: "Weekly report", "PDF summarizer", "Code reviewer", "Make me a skill that does X". These are common because the Skill Agent quick-start chips ("Try one of these") intentionally produce them as starting labels.
>
> For bare requests, the goal is a **perfect** skill on the first try — not a generic one the user has to revise. Ask 2-3 focused questions using the `:::options` markdown block (chip-style options, max 4 per question, 2-4 options each). Each `:::options` block is rendered as clickable chips in the chat UI — do NOT use an `AskUserQuestion` tool: there is no such tool registered in the Skill Agent's toolset. Pick the dimensions that most affect the `skill_md` you would write:
>
> - Audience ...
> - Output format ...
> - Cadence / trigger ...
> - Scope boundary ...
> - Data source ...
> - Example output ...
>
> Do NOT ask all six. Pick 2-3 that have the most leverage. After the user answers, write the `skill_md` and call `create_skill` — do not ask a second round of clarifying questions unless something in the answers is genuinely contradictory.

New text:

> A bare request is one where the user has named a capability but said nothing about its audience, output, cadence, or trigger conditions. Examples: "Weekly report", "PDF summarizer", "Code reviewer", "Make me a skill that does X". These are common because the Skill Agent quick-start chips ("Try one of these") intentionally produce them as starting labels.
>
> For bare requests, the goal is a **perfect** skill on the first try — not a generic one the user has to revise. The agent MUST commit to a complete default in one sentence (audience + format + focus + trigger, with the most likely values inline), then emit **a single `:::options` block** with 8-12 toggleable chips covering 3-4 of the dimensions below. Each chip is prefixed with its dimension name (`Audience: Engineers`, `Format: Inline comments`, `Focus: Security`) so the chip is self-labeling — no separate legend is needed. The user can click 0+ chips to override; the agent uses its pre-stated defaults for any dimension the user did NOT override. The `:::options` block is rendered as multi-select chips with a "Use these (N)" commit button in the chat UI — do NOT use an `AskUserQuestion` tool: there is no such tool registered in the Skill Agent's toolset. Do NOT ask a second round of clarifying questions after the user sends — the multi-select block replaces the previous 2-3 question pattern.
>
> Pick 3-4 dimensions from the list below that have the most leverage for the requested skill:
>
> - **Audience** — who runs the skill? (e.g. engineering team, individual contributor, executive)
> - **Output format** — what does the skill produce? (HTML, DOCX, PDF, PPTX, Markdown, plain text, Slack message, code)
> - **Cadence / trigger** — when does the skill run? (on-demand, weekly, per-PR, on-data-change). This drives the `trigger` field.
> - **Focus** — what is the skill's primary lens? (security, performance, quality, breadth, audience-specific)
> - **Scope boundary** — what does the skill explicitly NOT do?
> - **Data source** — where does the skill get its input? (uploaded files, a specific data source, user-pasted text, an API)

The change is: replace "Ask 2-3 focused questions" (which produced 3 separate blocks) with "commit to defaults + emit a single multi-select block" (which produces 1 block the user can multi-select from). The list of dimensions stays the same — they are now the *dimensions from which the chips are drawn*, not separate question topics.

#### 3.2.2 Augment the chip-format rule (Agent Builder's `ALWAYS USE A :::options CHECKLIST` near line 305)

The current rule says "Options must be concrete and short (≤ 8 words each)." Add a sibling bullet documenting the dimension-prefix convention (so the convention is shared across agents — the prompt convention should be consistent even if the multi-select UI is Skill Agent only):

> - When the question covers multiple dimensions in one block, prefix each option with its dimension (`Audience: Engineers`, `Format: Inline comments`). Self-labeling chips are required for multi-dimension blocks because there is no separate legend.

### 3.3 Frontend changes

#### 3.3.1 `OptionsBlock` becomes a multi-select component (with a `multiSelect` prop for back-compat)

`AgentMarkdown.jsx` lines 102-117. Current behavior: each option is a single button that fires `onClick={() => onSelect?.(opt)}` immediately on click — single-select semantics.

New behavior:

- New optional prop `multiSelect: boolean` (default `false` for back-compat with Agent Builder and Embedded Agent Builder).
- When `multiSelect === false`: current behavior unchanged. Single click on a chip immediately calls `onOptionSelect(opt)`. No commit button. (Protects Agent Builder + Embedded Agent Builder callers.)
- When `multiSelect === true`:
  - Track `selected: Set<string>` in component state.
  - Render each chip as a **toggle button**:
    - **Unselected**: outline style — `border border-primary/30 bg-primary/5 text-primary` (current default)
    - **Selected**: filled style — `bg-primary text-primary-foreground border-primary`; `aria-pressed="true"`
  - A `selected.size >= 1` condition renders a "**Use these (N)**" button below the chip group (`disabled` when 0 selected).
  - The commit button calls `onOptionSelect(Array.from(selected).join(', '))`.
  - After commit, `selected` resets to empty (so a fresh `:::options` block starts clean).

The existing `onOptionSelect` prop API on `AgentMarkdown` (line 141) is preserved.

#### 3.3.2 `SkillMessageBubble.jsx` — pass `multiSelect={true}` to `AgentMarkdown`

The single wiring point that flips Skill Agent to multi-select:

```jsx
<AgentMarkdown onOptionSelect={onOptionSelect} multiSelect={true}>{message.content}</AgentMarkdown>
```

`AgentBuilder.jsx` and `EmbeddedAgentBuilder.jsx` keep `<AgentMarkdown onOptionSelect={handleSelectOption}>` — no `multiSelect` prop, default `false`, unchanged behavior.

#### 3.3.3 Why no change to `SkillAgent.jsx`

The previous turn established the pattern:
- `SkillAgent.jsx:457` passes `onOptionSelect={applySuggestion}` to `<SkillMessageBubble>`.
- `applySuggestion` (in `SkillAgent.jsx:181-185`) does `setInput(text); setPrefilledHint(true); inputRef.current?.focus()`.

The new `OptionsBlock` multi-select commit calls the same `onOptionSelect` with a comma-separated string. `applySuggestion` drops that string into the input box. The user sees a populated textarea, can edit, and presses Enter to send. **One send instead of three.** This is exactly the "Use these (N) → input box round-trip" UX picked in brainstorming.

### 3.4 Visual sketch

```
┌──────────────────────────────────────────────────────────┐
│  Skill Agent                                             │
│  Create, edit, and discover skills.                      │
│                                                          │
│  ┌─ chat messages ────────────────────────────────────┐  │
│  │ [user]   Code reviewer                              │  │
│  │ [agent]  search_skills ✓ (1 call)                   │  │
│  │ [agent]  I'll build a code-reviewer skill with      │  │
│  │          these defaults: Engineers / Inline         │  │
│  │          comments / Security-focused. Pick any      │  │
│  │          to change, or press Send to use my picks.  │  │
│  │          ┌──────────────────────────────────┐       │  │
│  │          │ Audience: Individual dev        │       │  │
│  │          │ Audience: Team lead             │       │  │
│  │          │ Audience: Open-source maintainer│       │  │
│  │          │ Format: Inline comments         │       │  │
│  │          │ Format: Structured report       │       │  │
│  │          │ Format: Scoring checklist       │       │  │
│  │          │ Focus: Security (OWASP, ...)    │       │  │
│  │          │ Focus: Performance (...)        │       │  │
│  │          │ Focus: Quality & style (...)    │       │  │
│  │          └──────────────────────────────────┘       │  │
│  │          [ Use these (0) ]   (disabled until N≥1)   │  │
│  └─────────────────────────────────────────────────────┘  │
│  ┌─ input area (existing renderInputArea) ─────────────┐ │
│  │ (textarea + Send button)                             │  │
│  └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

After the user clicks 2 chips ("Audience: Individual dev", "Focus: Security (OWASP, injection, auth)"), the button becomes `[ Use these (2) ]` (active, primary color). Clicking it fills the input with `Audience: Individual dev, Focus: Security (OWASP, injection, auth)`. The user presses Enter to send.

---

## 4. Error handling

| Failure mode | What happens |
|---|---|
| Agent emits **0** `:::options` blocks (omits the block) | Falls back to current behavior — user has to type everything. The prompt encourages the block but does not make it load-bearing. (Soft: a test asserts the prompt mentions the block, see §5.1.) |
| Agent emits **2+** `:::options` blocks (regresses to the old pattern) | All blocks render; the user can pick from any of them. The prompt forbids it but the UI does not block it. |
| Agent emits a `:::options` block with **0 options** | `OptionsBlock` returns `null` (current behavior, line 104). No chips render, no commit button. |
| Agent emits a `:::options` block with **> 12 options** | All render. No upper bound enforced in the UI. The prompt says 8-12 as a guideline; the upper bound is a soft target, not a hard rule. |
| User clicks the commit button while a send is already in flight | `applySuggestion` just sets input state. The existing in-flight guard in `handleSend` (lines 235-237) prevents double-send. No regression. |
| User's selected chips contain commas / special characters | `Array.from(selected).join(', ')` produces valid input. No escaping needed because the text is just chat input, not JSON. |
| `multiSelect` prop is omitted on a Skill Agent message | `OptionsBlock` defaults to `multiSelect={false}` → single-click behavior. (The user explicitly passes `multiSelect={true}` in the Skill Agent wiring. See §3.3.2.) |

---

## 5. Testing

### 5.1 Backend prompt test (extend `test_skill_agent_prompt_skill_creation_workflow.py`)

Add **1 new test** to the existing file:

`test_skill_agent_prompt_uses_single_multiselect_options_block`:
- Asserts the Bare-Request Handling section contains the phrase `"single ` :::options` block"`.
- Asserts it mentions `"8-12"` (chip count guidance).
- Asserts it forbids the old `"ask 2-3 questions"` wording — uses the same negation-context regex helper from the existing `test_skill_agent_prompt_forbids_stop_phrasing` test.
- Asserts it contains the dimension-prefix example (`"Audience:"` and `"Format:"`).
- Asserts the section forbids `"second round"` of clarifying questions.

### 5.2 Frontend component test (new file)

`frontend/src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx`.

Uses `@testing-library/react` (verify availability in the project's vitest stack before writing; if not available, fall back to React Test Renderer or a vitest component-import smoke test).

Tests:

1. **Renders chips**: given a `:::options` block content, renders one button per line.
2. **Toggle selected on click**: clicking chip A marks it selected (filled style + `aria-pressed="true"`); clicking again deselects (outline style + `aria-pressed="false"`).
3. **Multi-select state**: clicking A then B then C produces `selected = {A, B, C}`. Clicking B again removes it.
4. **Commit button disabled when 0 selected**: button is rendered but `disabled` and visually muted.
5. **Commit button enabled when N≥1**: button is rendered with active styling, label shows `(N)`.
6. **Commit calls `onOptionSelect`**: clicking the commit button calls `onOptionSelect('A, B, C')` (comma-separated, single space).
7. **Reset after commit**: after commit, `selected` resets to empty.
8. **No regression on single-click UX (`multiSelect={false}` default)**: clicking a chip immediately calls `onOptionSelect` with that single chip's text. No commit button rendered. (Protects Agent Builder + Embedded Agent Builder callers.)
9. **`multiSelect={false}` is the default**: omitting the prop gives the same behavior as `multiSelect={false}`.

### 5.3 E2E smoke (no new tests, just re-run)

Existing Skill Agent test files — they should all still pass after the changes:

- `frontend/src/lib/translations.test.js` (4 tests)
- `frontend/src/lib/translations.skillAgentKeys.test.jsx` (3 tests)
- `frontend/src/pages/SkillAgent.emptyState.test.jsx`
- `frontend/src/pages/SkillAgent.applySuggestion.test.jsx`
- `frontend/src/pages/SkillAgent.currentSkillSync.test.jsx`
- `frontend/src/components/skillagent/SkillMessageBubble.onOptionSelect.test.jsx` (4 tests — this one needs a 1-line update to assert `multiSelect={true}` is passed; see §6.1 step 5)
- `backend/tests/test_skill_agent_prompt_skill_creation_workflow.py` (12 tests after the new one is added)
- `backend/tests/test_skill_agent_loop_guard_config.py` (9 tests — unchanged)

---

## 6. Rollout

### 6.1 Rollout sequence

1. Backend prompt change (`agent_prompts.py`) — 1 file, 1 section rewrite + 1 new bullet near line 305.
2. Backend test added to `test_skill_agent_prompt_skill_creation_workflow.py`.
3. Frontend `OptionsBlock` change (`AgentMarkdown.jsx`) — 1 component rewrite (~50 lines, new `multiSelect` prop).
4. Frontend `SkillMessageBubble.jsx` 1-line change: pass `multiSelect={true}` to `AgentMarkdown`.
5. Frontend `SkillMessageBubble.onOptionSelect.test.jsx` 1-line update: assert `multiSelect={true}` is present in the JSX call.
6. New frontend test file `AgentMarkdown.optionsBlock.multiselect.test.jsx`.
7. Run all Skill Agent tests (frontend + backend). Free RAM 7.5 GiB before run.
8. Manual smoke: open `localhost:3000/skill-agent`, send "Code reviewer", confirm: 1 search call, 1 :::options block, 9 chips, "Use these (0)" disabled, click 2 chips → "Use these (2)" enabled, click it → input populated, press Enter → agent builds the skill.
9. Leave uncommitted per project policy. The user hard-refreshes to pick up the new JS bundle.

### 6.2 Risk

- **Risk: regression of `OptionsBlock` for Agent Builder and Embedded Agent Builder.** Both currently rely on the single-click behavior. The new `OptionsBlock` defaults to `multiSelect={false}` so they are unaffected. Test 5.2.8 pins the no-regression invariant.
- **Risk: prompt drift.** The agent might not always emit the "stated defaults" paragraph. The prompt makes it a `MUST`, but the model may still occasionally skip it. Acceptable degradation — the user just doesn't see the lead-in, but the chips still work. The test in §5.1 only asserts the prompt mentions the pattern; it does not assert runtime adherence (which would need an LLM eval, out of scope for this fix).
- **Risk: long chip text overflows the chat bubble.** Some focus areas have long parenthetical text (`Focus: Security (OWASP, injection, auth)`). The chip is already styled `rounded-full ... px-3 py-1.5 text-xs`, so it wraps as a pill. If the user reports overflow, fall back to abbreviating parenthetical text in the chip itself (`Focus: Security` with a tooltip showing the detail). Not in scope for this turn.
