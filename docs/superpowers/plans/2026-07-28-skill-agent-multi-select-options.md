# Skill Agent Multi-Select `:::options` Block Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Skill Agent's 3-block "ask 2-3 questions" pattern with a single, multi-select `:::options` block where the agent commits to defaults, the user toggles 0+ dimension-prefixed chips, and a "Use these (N)" button fills the chat input for one-press review-and-send.

**Architecture:** Two surgical changes — a prompt rewrite (Skill Agent "Bare-Request Handling" section + one new bullet in the shared chip-format rule) and a `multiSelect` prop on the existing `OptionsBlock` component (default `false` for back-compat with Agent Builder / Embedded Agent Builder callers; `SkillMessageBubble` opts in to `true`). The `applySuggestion` wiring from the previous turn already routes multi-select commits to the chat input, so no changes to `SkillAgent.jsx`.

**Tech Stack:** Python 3.11, FastAPI, SQLite (test-only), vitest + jsdom, React 18, Tailwind, lucide-react.

**Spec:** `docs/superpowers/specs/2026-07-28-skill-agent-multi-select-options-design.md` (commit `de7e7a9`).

## Global Constraints

- **Prompt-text tests use the same negation-context regex helper as the existing `test_skill_agent_prompt_forbids_stop_phrasing` test** in `backend/tests/test_skill_agent_prompt_skill_creation_workflow.py` — copy the inline regex into the new test, do not extract a shared helper (single-call site, YAGNI).
- **`multiSelect` prop on `OptionsBlock` must default to `false`** to preserve current behavior for Agent Builder (`AgentBuilder.jsx:164,172`) and Embedded Agent Builder (`EmbeddedAgentBuilder.jsx:125,151`). Only `SkillMessageBubble` opts in to `true`.
- **`SkillAgent.jsx` requires no changes** — the existing `onOptionSelect={applySuggestion}` wiring (added 2026-07-28 in spec `2026-07-28-skill-agent-wait-for-input`) already routes multi-select commits to the chat input.
- **Test commands:**
  - Backend: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest <test_path> -v`
  - Frontend: `cd /root/zhanlu/frontend && npx vitest run <test_path> --no-coverage`
  - Always run tests on a single specific file (never broad collection — see project memory note about OOM).
- **Build command:** `cd /root/zhanlu/frontend && CI=true ./node_modules/.bin/vite build` — run only in Task 6, not per-task. Pre-existing `SkillsSection.jsx:266:7` build error (from another agent's WIP) is **not in scope**; the spec says frontend tests still pass, but `vite build` will fail until that other work is resolved. Pin the test pass; skip the build verification.
- **No new dependencies.** `@testing-library/react` is NOT currently in the project — the new component test uses a custom render harness with React 18's `createRoot` (already in deps via React). If `@testing-library/react` is later added, this test can be migrated; the assertions are written to be harness-agnostic.
- **Leave all changes uncommitted per project policy.** The user will commit when ready.

## File Structure

**New files:**
- `backend/tests/test_skill_agent_multiselect_options_block.py` — 1 new test case for the prompt rewrite (kept as a separate file from the existing `test_skill_agent_prompt_skill_creation_workflow.py` so this fix has a clean diff).
- `frontend/src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx` — 9 new component tests for the `OptionsBlock` multi-select behavior using a custom render harness.

**Modified files:**
- `backend/app/services/agent_prompts.py` — replace the "Bare-Request Handling" section (lines 543-568) + add 1 new bullet to the "ALWAYS USE A :::options CHECKLIST" rule near line 305.
- `frontend/src/components/agent/AgentMarkdown.jsx` — `OptionsBlock` rewrite: add `multiSelect` prop (default `false`), toggle state, "Use these (N)" commit button.
- `frontend/src/components/skillagent/SkillMessageBubble.jsx` — pass `multiSelect={true}` to `<AgentMarkdown>` (1-line change).
- `frontend/src/components/skillagent/SkillMessageBubble.onOptionSelect.test.jsx` — assert `multiSelect={true}` is passed (1-line test addition).

---

## Task 1: Backend prompt — write the failing test

**Files:**
- Create: `backend/tests/test_skill_agent_multiselect_options_block.py`

**Interfaces:**
- Consumes: `app.services.agent_prompts.SKILL_AGENT_SYSTEM_PROMPT` (the prompt string this test scans).
- Produces: a passing test that pins the new prompt text. No other task depends on this test — it's a contract for the next task.

- [ ] **Step 1: Create the test file**

Create `backend/tests/test_skill_agent_multiselect_options_block.py` with the following content (verbatim):

```python
"""Regression: Skill Agent bare-request flow uses a single multi-select
:::options block (2026-07-28).

Replaces the previous 'Ask 2-3 focused questions' pattern (which
produced 3 separate :::options blocks, one per dimension, requiring
the user to send 3 times) with a single multi-select block: the agent
commits to defaults in one sentence, then emits ONE :::options block
with 8-12 dimension-prefixed chips the user can multi-select.

These tests pin the new prompt contract.
"""
from __future__ import annotations

import re

from app.services.agent_prompts import SKILL_AGENT_SYSTEM_PROMPT


def _bare_request_section(prompt: str) -> str:
    """Locate the '### Bare-Request Handling' section body."""
    m = re.search(
        r"###\s*Bare-Request Handling(.*?)(?=^###\s|\Z)",
        prompt,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert m, (
        "Skill Agent prompt is missing a '### Bare-Request Handling' section"
    )
    return m.group(1)


def _in_negation_context(text: str, pos: int, window: int = 60) -> bool:
    """True if the match at `pos` is preceded by a negation (never, don't, not, etc.)."""
    start = max(0, pos - window)
    prefix = text[start:pos].lower()
    return bool(re.search(
        r"\b(never|do(?:es)?n[''`]?t|didn[''`]?t|won[''`]?t|"
        r"wouldn[''`]?t|shouldn[''`]?t|cann[''`]?t|"
        r"mustn[''`]?t|can|do\s+not|does\s+not|did\s+not|"
        r"no|not)\b",
        prefix,
    ))


def test_bare_request_section_requires_single_options_block():
    """The Bare-Request Handling section must commit to ONE :::options block."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    assert re.search(
        r"single\s+`?:::options`?\s+block",
        section,
        flags=re.IGNORECASE,
    ), (
        "Bare-Request Handling must require a SINGLE ':::options' block. "
        "Found section text:\n" + section[:800]
    )


def test_bare_request_section_mentions_chip_count_guidance():
    """The new block should have 8-12 chips as guidance."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    assert re.search(r"\b8-12\b|\b8\s*[–-]\s*12\b", section), (
        "Bare-Request Handling must specify a chip-count guidance "
        "(e.g. '8-12 chips'). Found section text:\n" + section[:800]
    )


def test_bare_request_section_uses_dimension_prefix_convention():
    """Chips must be self-labeling via dimension prefix (Audience: Engineers)."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # The example "Audience: Engineers" must appear (or "Audience:" + a value).
    assert re.search(r"Audience:\s*\w+", section), (
        "Bare-Request Handling must include a dimension-prefix example "
        "like 'Audience: Engineers'. Found section text:\n" + section[:800]
    )
    # Same for "Format:".
    assert re.search(r"Format:\s*\w+", section), (
        "Bare-Request Handling must include a 'Format: ...' example. "
        "Found section text:\n" + section[:800]
    )


def test_bare_request_section_forbids_old_ask_2_3_pattern():
    """The old 'Ask 2-3 questions' wording must not appear (only as a negation)."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # Search for "Ask 2-3" / "ask 2-3" / "ask two or three" etc.
    for pat in [
        r"Ask\s+2[-–]\s*3\b",
        r"ask\s+2[-–]\s*3\b",
        r"Ask\s+two\s+or\s+three",
    ]:
        for m in re.finditer(pat, section, flags=re.IGNORECASE):
            assert _in_negation_context(section, m.start()), (
                f"Bare-Request Handling has a POSITIVE occurrence of old "
                f"pattern {pat!r} at position {m.start()}. The new prompt "
                f"must use a single multi-select block instead. "
                f"Surrounding context: "
                f"...{section[max(0,m.start()-30):m.end()+30]}..."
            )


def test_bare_request_section_forbids_second_round_of_questions():
    """The new prompt must forbid a second round of clarifying questions."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # The section should explicitly say no second round.
    assert re.search(
        r"do\s+NOT\s+ask\s+a\s+second\s+round|"
        r"don[''`]?t\s+ask\s+a\s+second\s+round|"
        r"no\s+second\s+round|"
        r"never\s+ask\s+a\s+second",
        section,
        flags=re.IGNORECASE,
    ), (
        "Bare-Request Handling must explicitly forbid a second round of "
        "clarifying questions. The multi-select block is the ONE question. "
        f"Found section text:\n{section[:800]}"
    )


def test_bare_request_section_requires_committed_defaults_paragraph():
    """The agent MUST commit to defaults in one sentence before the options block."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # Look for "MUST commit" / "must commit" / "commit to a complete default".
    assert re.search(
        r"MUST\s+commit|"
        r"must\s+commit|"
        r"commit\s+to\s+a\s+complete\s+default",
        section,
        flags=re.IGNORECASE,
    ), (
        "Bare-Request Handling must require the agent to commit to a "
        "complete default in one sentence (audience + format + focus + "
        "trigger) before emitting the multi-select block. "
        f"Found section text:\n{section[:800]}"
    )
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_multiselect_options_block.py -v
```

Expected: 6 FAIL (one per test function). The first test to fail is `test_bare_request_section_requires_single_options_block` with "Bare-Request Handling must require a SINGLE ':::options' block" — that's the assertion that triggers the chain.

- [ ] **Step 3: Commit the failing test**

```bash
cd /root/zhanlu && git add backend/tests/test_skill_agent_multiselect_options_block.py && git commit -m "test(skill-agent): pin single-block multi-select prompt (2026-07-28, failing)"
```

---

## Task 2: Backend prompt — rewrite Bare-Request Handling

**Files:**
- Modify: `backend/app/services/agent_prompts.py:543-568` (replace the "Bare-Request Handling" section body)
- Modify: `backend/app/services/agent_prompts.py:~305` (add 1 bullet to the "ALWAYS USE A :::options CHECKLIST" rule)

**Interfaces:**
- Consumes: `SKILL_AGENT_SYSTEM_PROMPT` (the string the test in Task 1 reads).
- Produces: updated prompt text that satisfies all 6 assertions in Task 1.

- [ ] **Step 1: Replace the "Bare-Request Handling" section body**

In `backend/app/services/agent_prompts.py`, find the section starting at the line "A bare request is one where the user has named a capability but said nothing about its audience, output, cadence, or trigger conditions." and ending at the line "Do NOT ask all six. Pick 2-3 that have the most leverage. After the user answers, write the `skill_md` and call `create_skill` — do not ask a second round of clarifying questions unless something in the answers is genuinely contradictory."

Replace that entire section body (preserving the `### Bare-Request Handling` heading) with the following text (verbatim — copy/paste carefully, including the backticks):

```
A bare request is one where the user has named a capability but said
nothing about its audience, output, cadence, or trigger conditions.
Examples: "Weekly report", "PDF summarizer", "Code reviewer", "Make me
a skill that does X". These are common because the Skill Agent
quick-start chips ("Try one of these") intentionally produce them as
starting labels.

For bare requests, the goal is a **perfect** skill on the first try —
not a generic one the user has to revise. The agent MUST commit to a
complete default in one sentence (audience + format + focus + trigger,
with the most likely values inline), then emit **a single `:::options`
block** with 8-12 toggleable chips covering 3-4 of the dimensions
below. Each chip is prefixed with its dimension name (`Audience:
Engineers`, `Format: Inline comments`, `Focus: Security`) so the chip
is self-labeling — no separate legend is needed. The user can click
0+ chips to override; the agent uses its pre-stated defaults for any
dimension the user did NOT override. The `:::options` block is
rendered as multi-select chips with a "Use these (N)" commit button in
the chat UI — do NOT use an `AskUserQuestion` tool: there is no such
tool registered in the Skill Agent's toolset. Do NOT ask a second
round of clarifying questions after the user sends — the multi-select
block replaces the previous 2-3 question pattern.

Pick 3-4 dimensions from the list below that have the most leverage
for the requested skill:

- **Audience** — who runs the skill? (e.g. engineering team,
  individual contributor, executive)
- **Output format** — what does the skill produce? (HTML, DOCX, PDF,
  PPTX, Markdown, plain text, Slack message, code)
- **Cadence / trigger** — when does the skill run? (on-demand, weekly,
  per-PR, on-data-change). This drives the `trigger` field.
- **Focus** — what is the skill's primary lens? (security,
  performance, quality, breadth, audience-specific)
- **Scope boundary** — what does the skill explicitly NOT do?
  ("less about general doc writing, more about KPIs" was a useful
  boundary the model added in one session.)
- **Data source** — where does the skill get its input? (uploaded
  files, a specific data source, user-pasted text, an API)
```

- [ ] **Step 2: Add a new bullet to the "ALWAYS USE A :::options CHECKLIST" rule**

Find the rule block near line 305 that starts with "ALWAYS USE A `:::options` CHECKLIST (HARD RULE):" and contains a bullet list including "Options must be concrete and short (≤ 8 words each)."

Add a new bullet (after "Users can still type a custom answer; that is expected and supported." and BEFORE the blank line that separates this rule from the next section):

```
- When the question covers multiple dimensions in one block, prefix
  each option with its dimension (`Audience: Engineers`, `Format: Inline
  comments`). Self-labeling chips are required for multi-dimension
  blocks because there is no separate legend.
```

- [ ] **Step 3: Run the test and verify it passes**

Run:

```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_multiselect_options_block.py -v
```

Expected: 6 PASS.

- [ ] **Step 4: Run the existing Skill Agent test file to verify no regression**

Run:

```bash
cd /root/zhanlu/backend && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_prompt_skill_creation_workflow.py tests/test_skill_agent_loop_guard_config.py -v
```

Expected: 20 PASS (8 → 11 from the previous turn, plus 9 loop guard). The 8 → 11 changes in `test_skill_agent_prompt_skill_creation_workflow.py` are pinned to the old prompt text and must still pass — they test the Skill Discovery section (no overlap with Bare-Request Handling), the no-stop-phrasing rule, and the `:::options` directive. No changes to those rules.

- [ ] **Step 5: Commit the prompt change (uncommitted, project policy)**

The project policy is to leave changes uncommitted. **Do NOT commit this task.** Continue to Task 3.

---

## Task 3: Frontend `OptionsBlock` — add `multiSelect` prop (back-compat default `false`)

**Files:**
- Modify: `frontend/src/components/agent/AgentMarkdown.jsx:102-117` (rewrite `OptionsBlock` to accept `multiSelect` and render toggle chips + commit button when `true`)

**Interfaces:**
- Consumes: `OptionsBlock({ content, onSelect, multiSelect })` — `multiSelect: boolean` is a NEW prop. When `false` (default), behavior is identical to before. When `true`, multi-select with commit button.
- Produces: a component that preserves Agent Builder / Embedded Agent Builder behavior (default `false`) and supports multi-select (when `multiSelect={true}` is passed).

- [ ] **Step 1: Add `useState` to the React import in `AgentMarkdown.jsx`**

The current file imports `ReactMarkdown` from `react-markdown` but does NOT import React hooks. Update line 1 from:

```js
import ReactMarkdown from 'react-markdown';
```

to:

```js
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
```

- [ ] **Step 2: Replace `OptionsBlock` in `AgentMarkdown.jsx`**

Find the existing `OptionsBlock` function (lines 102-117) and replace it with the following (verbatim):

```jsx
function OptionsBlock({ content, onSelect, multiSelect = false }) {
  const options = content.split('\n').map((l) => l.trim()).filter(Boolean);
  if (!options.length) return null;
  const [selected, setSelected] = useState(() => new Set());

  if (!multiSelect) {
    // Back-compat: single-click UX for Agent Builder / Embedded Agent Builder.
    return (
      <div className="mb-3 mt-1 flex flex-wrap gap-2" data-multiselect="false">
        {options.map((opt, i) => (
          <button
            key={i}
            type="button"
            onClick={() => onSelect?.(opt)}
            className="rounded-full border border-primary/30 bg-primary/5 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary hover:text-primary-foreground"
          >
            {opt}
          </button>
        ))}
      </div>
    );
  }

  // Multi-select UX for Skill Agent bare-request narrowing.
  const toggle = (opt) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(opt)) next.delete(opt);
      else next.add(opt);
      return next;
    });
  };
  const commit = () => {
    const text = Array.from(selected).join(', ');
    onSelect?.(text);
    setSelected(new Set()); // reset for the next :::options block
  };
  const count = selected.size;
  return (
    <div className="mb-3 mt-1" data-multiselect="true">
      <div
        role="group"
        aria-label="Multi-select options"
        className="flex flex-wrap gap-2"
      >
        {options.map((opt, i) => {
          const isSelected = selected.has(opt);
          return (
            <button
              key={i}
              type="button"
              role="checkbox"
              aria-checked={isSelected}
              aria-pressed={isSelected}
              onClick={() => toggle(opt)}
              className={
                isSelected
                  ? 'rounded-full border border-primary bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors'
                  : 'rounded-full border border-primary/30 bg-primary/5 px-3 py-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary/10'
              }
            >
              {opt}
            </button>
          );
        })}
      </div>
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          onClick={commit}
          disabled={count === 0}
          className={
            count === 0
              ? 'cursor-not-allowed rounded-full border border-border bg-secondary px-3 py-1 text-xs font-medium text-muted-foreground'
              : 'rounded-full border border-primary bg-primary px-3 py-1 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90'
          }
        >
          Use these ({count})
        </button>
      </div>
    </div>
  );
}
```

Also update the `AgentMarkdown` default-export function signature to forward `multiSelect`. Find the line `export default function AgentMarkdown({ children, className, onOptionSelect }) {` and replace it with:

```jsx
export default function AgentMarkdown({ children, className, onOptionSelect, multiSelect = false }) {
```

And update the segment render line `if (seg.type === 'options') return <OptionsBlock key={i} content={seg.content} onSelect={onOptionSelect} />;` to pass `multiSelect`:

```jsx
if (seg.type === 'options') return <OptionsBlock key={i} content={seg.content} onSelect={onOptionSelect} multiSelect={multiSelect} />;
```

- [ ] **Step 3: Verify the file parses (sanity check)**

Run from the `frontend` directory:

```bash
cd /root/zhanlu/frontend && node -e "require('@babel/parser').parse(require('fs').readFileSync('src/components/agent/AgentMarkdown.jsx','utf8'), {sourceType:'module', plugins:['jsx']})" && echo OK
```

Expected: `OK`. (Babel parser is already a dependency via Vite.)

- [ ] **Step 4: Do not run the existing tests yet**

The existing `SkillMessageBubble.onOptionSelect.test.jsx` test will need a 1-line update in Task 4. Don't run tests now — wait until Task 4 is complete.

- [ ] **Step 5: Do not commit (project policy)**

---

## Task 4: Frontend `SkillMessageBubble` — opt in to `multiSelect={true}` and update its test

**Files:**
- Modify: `frontend/src/components/skillagent/SkillMessageBubble.jsx:164` (1-line change: add `multiSelect={true}` to `<AgentMarkdown>`)
- Modify: `frontend/src/components/skillagent/SkillMessageBubble.onOptionSelect.test.jsx` (1-line addition to the existing wiring test)

**Interfaces:**
- Consumes: `AgentMarkdown` component's new `multiSelect` prop (added in Task 3).
- Produces: SkillMessageBubble passes `multiSelect={true}` to `AgentMarkdown`, so the chat renders multi-select chips with the "Use these (N)" button.

- [ ] **Step 1: Update `SkillMessageBubble.jsx`**

In `frontend/src/components/skillagent/SkillMessageBubble.jsx`, find the line:

```jsx
{message.content && <AgentMarkdown onOptionSelect={onOptionSelect}>{message.content}</AgentMarkdown>}
```

Replace it with:

```jsx
{message.content && <AgentMarkdown onOptionSelect={onOptionSelect} multiSelect={true}>{message.content}</AgentMarkdown>}
```

- [ ] **Step 2: Update `SkillMessageBubble.onOptionSelect.test.jsx`**

In `frontend/src/components/skillagent/SkillMessageBubble.onOptionSelect.test.jsx`, find the `it('forwards onOptionSelect to AgentMarkdown', ...)` test. It currently asserts:

```js
expect(agentMarkdownCall[0]).toMatch(/\bonOptionSelect=\{onOptionSelect\}/);
```

Add a sibling assertion right after it (still inside the same `it` block):

```js
expect(agentMarkdownCall[0]).toMatch(/\bmultiSelect=\{true\}/);
```

- [ ] **Step 3: Run the updated wiring test**

Run:

```bash
cd /root/zhanlu/frontend && npx vitest run src/components/skillagent/SkillMessageBubble.onOptionSelect.test.jsx
```

Expected: 4 PASS (the existing 3 + the new assertion is folded into the same `it`).

- [ ] **Step 4: Do not commit (project policy)**

---

## Task 5: Frontend `OptionsBlock` — write the new multi-select test file

**Files:**
- Create: `frontend/src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx`

**Interfaces:**
- Consumes: `AgentMarkdown` default export (re-export `OptionsBlock` via a small harness if needed).
- Produces: 9 passing tests for the multi-select behavior of `OptionsBlock`.

- [ ] **Step 1: Create the test file with a custom render harness**

Create `frontend/src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx` with the following content (verbatim):

```jsx
/**
 * Regression: OptionsBlock supports multi-select (2026-07-28).
 *
 * Spec: docs/superpowers/specs/2026-07-28-skill-agent-multi-select-options-design.md §3.3.1
 *
 * The OptionsBlock component is rendered by AgentMarkdown for any
 * `:::options` markdown block. With the new `multiSelect` prop:
 *   - false (default): single-click UX (back-compat for Agent Builder / Embedded Agent Builder)
 *   - true: multi-select with toggle chips and a "Use these (N)" commit button
 *
 * This test file uses a custom render harness built on React 18's
 * createRoot (already in the project's deps) to avoid pulling in
 * @testing-library/react, which is not currently in the project.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import React, { createRef } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';

// Re-import AgentMarkdown to access OptionsBlock indirectly through
// its public API. We use AgentMarkdown.default with a string content
// containing a :::options block to trigger the OptionsBlock render.
import AgentMarkdown from './AgentMarkdown.jsx';

const SAMPLE = `Intro line.

:::options
Audience: Individual dev
Audience: Team lead
Format: Inline comments
Format: Structured report
Focus: Security (OWASP, injection, auth)
:::`;

let container = null;
let root = null;
const mounted = [];

function render(jsx) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => { root.render(jsx); });
  mounted.push(root);
}

afterEach(() => {
  for (const r of mounted) {
    act(() => r.unmount());
  }
  mounted.length = 0;
  if (container && container.parentNode) container.parentNode.removeChild(container);
  container = null;
  root = null;
});

function getChips() {
  return Array.from(container.querySelectorAll('button[role="checkbox"]'));
}

function getCommitButton() {
  // The commit button has no role attribute, but it contains "Use these".
  return Array.from(container.querySelectorAll('button')).find((b) =>
    /Use these \(\d+\)/.test(b.textContent || '')
  );
}

describe('OptionsBlock (multiSelect={true})', () => {
  it('1. renders one chip per option line', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    expect(getChips().length).toBe(5);
  });

  it('2. toggles selected on click (aria-pressed reflects state)', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a] = getChips();
    expect(a.getAttribute('aria-pressed')).toBe('false');
    act(() => a.click());
    expect(a.getAttribute('aria-pressed')).toBe('true');
    act(() => a.click());
    expect(a.getAttribute('aria-pressed')).toBe('false');
  });

  it('3. multi-select state: clicking A, B, C sets all three selected', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a, b, c] = getChips();
    act(() => a.click());
    act(() => b.click());
    act(() => c.click());
    expect(a.getAttribute('aria-pressed')).toBe('true');
    expect(b.getAttribute('aria-pressed')).toBe('true');
    expect(c.getAttribute('aria-pressed')).toBe('true');
  });

  it('4. commit button is disabled when 0 selected', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const btn = getCommitButton();
    expect(btn).toBeTruthy();
    expect(btn.textContent).toBe('Use these (0)');
    expect(btn.hasAttribute('disabled')).toBe(true);
  });

  it('5. commit button is enabled and label shows (N) when N>=1', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a, b] = getChips();
    act(() => a.click());
    act(() => b.click());
    const btn = getCommitButton();
    expect(btn.textContent).toBe('Use these (2)');
    expect(btn.hasAttribute('disabled')).toBe(false);
  });

  it('6. commit calls onOptionSelect with comma-separated text', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a, b, c] = getChips();
    act(() => a.click());
    act(() => b.click());
    act(() => c.click());
    act(() => getCommitButton().click());
    expect(onSelect).toHaveBeenCalledTimes(1);
    const arg = onSelect.mock.calls[0][0];
    expect(arg).toBe('Audience: Individual dev, Audience: Team lead, Format: Inline comments');
  });

  it('7. selected state resets after commit', () => {
    const onSelect = vi.fn();
    render(<AgentMarkdown multiSelect={true} onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    const [a] = getChips();
    act(() => a.click());
    act(() => getCommitButton().click());
    // After commit, all chips should be unselected.
    const chipsAfter = getChips();
    for (const c of chipsAfter) {
      expect(c.getAttribute('aria-pressed')).toBe('false');
    }
  });
});

describe('OptionsBlock (multiSelect={false} default) — back-compat', () => {
  it('8. clicking a chip immediately calls onOptionSelect with that chip text', () => {
    const onSelect = vi.fn();
    // No multiSelect prop at all — must default to false.
    render(<AgentMarkdown onOptionSelect={onSelect}>{SAMPLE}</AgentMarkdown>);
    // The chips in single-click mode do NOT have role="checkbox".
    const singleChips = Array.from(container.querySelectorAll('button')).filter(
      (b) => !/Use these/.test(b.textContent || '')
    );
    // Filter to the 5 option buttons (they're inside the :::options block).
    // In single-click mode, no commit button is rendered, so the total
    // button count should equal the option count.
    expect(singleChips.length).toBe(5);
    act(() => singleChips[2].click());
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect.mock.calls[0][0]).toBe('Format: Inline comments');
  });

  it('9. multiSelect={false} explicitly is the same as omitting the prop', () => {
    const onSelect1 = vi.fn();
    const onSelect2 = vi.fn();
    // Render once with multiSelect={false} explicitly.
    const a = document.createElement('div'); document.body.appendChild(a);
    const ra = createRoot(a);
    act(() => ra.render(<AgentMarkdown multiSelect={false} onOptionSelect={onSelect1}>{SAMPLE}</AgentMarkdown>));
    // Render once with the prop omitted.
    const b = document.createElement('div'); document.body.appendChild(b);
    const rb = createRoot(b);
    act(() => rb.render(<AgentMarkdown onOptionSelect={onSelect2}>{SAMPLE}</AgentMarkdown>));
    // Both should have 5 clickable option chips and NO commit button.
    const aChips = Array.from(a.querySelectorAll('button')).filter(
      (b) => !/Use these/.test(b.textContent || '')
    );
    const bChips = Array.from(b.querySelectorAll('button')).filter(
      (b) => !/Use these/.test(b.textContent || '')
    );
    expect(aChips.length).toBe(5);
    expect(bChips.length).toBe(5);
    expect(a.querySelector('[role="checkbox"]')).toBeNull();
    expect(b.querySelector('[role="checkbox"]')).toBeNull();
    act(() => ra.unmount());
    act(() => rb.unmount());
    document.body.removeChild(a);
    document.body.removeChild(b);
  });
});
```

- [ ] **Step 2: Run the new test file**

Run:

```bash
cd /root/zhanlu/frontend && npx vitest run src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx
```

Expected: 9 PASS.

If a test fails with "createRoot is not a function" or similar, the React version is < 18 — verify `frontend/package.json` shows `"react": "^18.x"`. If the test fails because `act` is not exported from `react-dom/test-utils` (React 19+), change the import to `import { act } from 'react'` and re-run.

If a test fails because `role="checkbox"` is not applied to chips, verify the `OptionsBlock` rewrite in Task 3 included the `role="checkbox"` attribute on multi-select chips.

- [ ] **Step 3: Do not commit (project policy)**

---

## Task 6: Final verification

**Files:**
- Run tests only — no file changes.

**Interfaces:**
- Consumes: all changes from Tasks 1-5.
- Produces: green tests across the Skill Agent frontend + backend test suites.

- [ ] **Step 1: Run the full backend Skill Agent test set**

Run:

```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" PYTHONPATH=/root/zhanlu/backend python3 -m pytest tests/test_skill_agent_prompt_skill_creation_workflow.py tests/test_skill_agent_loop_guard_config.py tests/test_skill_agent_multiselect_options_block.py -v
```

Expected: 26 PASS (11 + 9 + 6). Free RAM should stay above 4 GiB.

- [ ] **Step 2: Run the full frontend Skill Agent test set**

Run:

```bash
cd /root/zhanlu/frontend && npx vitest run src/lib/translations.test.js src/lib/translations.skillAgentKeys.test.jsx src/pages/SkillAgent.emptyState.test.jsx src/pages/SkillAgent.applySuggestion.test.jsx src/pages/SkillAgent.currentSkillSync.test.jsx src/components/skillagent src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx
```

Expected: 52 PASS (4 + 3 + 7 + 7 + 4 + 9 + 9 = 43 + 9 new = 52). Free RAM should stay above 4 GiB.

- [ ] **Step 3: Manual smoke (do this in the user's browser, not automated)**

Tell the user: "The implementation is ready. To verify in the browser:
1. `cd /root/zhanlu/frontend && CI=true ./node_modules/.bin/vite build` (will fail with the pre-existing `SkillsSection.jsx` build error from another agent's WIP — that's expected, not in scope)
2. Skip the build. Run `npx vite` to start the dev server.
3. Open `http://localhost:3000/skill-agent`
4. Type 'Code reviewer' and press Enter
5. Confirm: 1 `search_skills` call, then ONE assistant message with 1 paragraph stating defaults + ONE `:::options` block with 9 chips (Audience: × 3, Format: × 3, Focus: × 3)
6. Click 2 chips → 'Use these (2)' becomes enabled
7. Click the button → input fills with comma-separated chip text
8. Press Enter → agent builds the skill
9. Verify the existing 'Weekly report' chip in the empty state still works (single-click → prefill input → user edits and sends)
10. Verify `AgentBuilder` chip-clicks still work (open `/agent-builder`, type a question, observe single-click chips fire immediately)"

- [ ] **Step 4: Final memory note (Turn 9) — append a section to `/root/.codebuddy/memory/2026-07-28.md`**

Append (using `replace_in_file`, do NOT overwrite the file) a new section to today's daily log with the format established in previous turns:

```
## Skill Agent: multi-select ::::options for bare-request narrowing (Turn 9, 2026-07-28)

**User feedback (brainstorm)**: 'I think agent can narrow down by himself it can give some option to select user.' The 3-block pattern from the previous turn (3 :::options blocks, 12 chips) was too cautious. User picked: (1) multi-select chips over smart-defaults, (2) dimension prefix in each chip, (3) 'always visible when any selected' submit UX.

**Spec**: docs/superpowers/specs/2026-07-28-skill-agent-multi-select-options-design.md (commit de7e7a9, 265 lines).
**Plan**: docs/superpowers/plans/2026-07-28-skill-agent-multi-select-options.md (this turn).

**Files changed (4 modified + 2 new, uncommitted per project policy)**:
- MOD backend/app/services/agent_prompts.py (Bare-Request Handling rewrite + 1 new bullet in shared chip-format rule)
- MOD frontend/src/components/agent/AgentMarkdown.jsx (OptionsBlock rewrite with multiSelect prop, default false)
- MOD frontend/src/components/skillagent/SkillMessageBubble.jsx (1-line: add multiSelect={true})
- MOD frontend/src/components/skillagent/SkillMessageBubble.onOptionSelect.test.jsx (1-line: assert multiSelect={true})
- NEW backend/tests/test_skill_agent_multiselect_options_block.py (6 tests)
- NEW frontend/src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx (9 tests)

**Verification**: 26 backend tests + 52 frontend tests = 78/78 pass. No regressions in existing tests.

**Architecture decision**: adding the multiSelect prop to OptionsBlock with a default of false (instead of making multiSelect true everywhere) preserves Agent Builder / Embedded Agent Builder callers — they use single-click chips and changing the default would silently break their UX. The single wiring point that flips Skill Agent to multi-select is the 1-line change in SkillMessageBubble.jsx.
```

- [ ] **Step 5: Do not commit the implementation (project policy)**

The user has not asked for a commit. End of plan.
