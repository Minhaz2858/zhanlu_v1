# Build With AI — Copilot Studio Panel Redesign (Design Spec)

> Date: 2026-09-03
> Status: Approved (brainstorming) — pending spec review
> Feature: Visual/UX upgrade of the workflow canvas "Build with AI" slide-over
> Component: `frontend/src/components/workflow/WorkflowBuilderChat.jsx` + its source-contract test
> No backend changes. No stream-contract changes.

## Problem

The Build With AI panel (Workflow Builder Chat slide-over) reads as a generic demo
chat, not a professional AI builder:

1. **No identity** — flat 380px slide-over; `Sparkles` icon + plain title; the empty
   state is a single gray sentence ("Describe the workflow you want and the AI will
   draft it on this canvas.").
2. **Raw LLM text dumps** — assistant bubbles are plain `whitespace-pre-wrap` text.
   The `workflow_builder` agent streams prose plus config/JSON-ish fragments and
   repair-loop copy with zero structure.
3. **No guidance** — no example prompts, no capability hints; a non-technical user
   stares at an empty input.
4. **No sense of progress** — "Building…" is a tiny spinner line; nothing signals
   designing → validating → saving; on success the panel silently closes.
5. **Weak composer** — single-line input; Send disabled while streaming (no Stop);
   no keyboard hints.

## Goals

- Make the panel look like a professional AI **builder** surface inside a canvas tool.
- Make it feel "smart": guided starters, structured streaming output, clear states.
- Keep every existing contract intact: `workflow_builder` agent binding,
  `metadata.workflow_id`, `streamAgentResponse`, `createAgentConversation`,
  `listAgentConversations` resume, `flow_saved` detection (typed event / status /
  conversation metadata flag), Escape-close, abort-on-close/unmount, session error
  bubble, parent-driven `onFlowSaved()` (canvas reload), `open`-gated rendering.

## Design

### Identity & signature element

The panel is a workflow **builder**, not a chat. Signature: the **blueprint empty
state** — a mini-pipeline graphic drawn with CSS/SVG (three chips: trigger → agent →
output, connected by a faint line; the last node pulses gently = "waiting for your
idea") above three **starter-prompt cards**:

1. **Sales report pipeline** — "A weekly sales report workflow: pull sales from the
   database, summarize top products and customers, output a markdown brief."
2. **Feedback triage** — "Classify incoming customer feedback by topic and route
   urgent complaints to a separate alert branch."
3. **Research assistant** — "Research a topic with web search, summarize the
   findings, and save a short brief."

EN + ZH (zh equivalents required). Each card = lucide icon + one-line title +
one-sentence what-it-builds description. Clicking a card sends that prompt as the
user message. Under the cards: muted microcopy "…or describe your own workflow below."

### Shell

- Width 400px (was 380), full-height right slide-over, canvas context preserved.
- Header (~48px): rounded tile **mark** (lucide `Workflow` glyph in a small
  gradient tile — restrained; do NOT use Sparkles/emoji), title **Build with AI**
  / **AI 构建** (strings kept for the source-contract tests), status pill on the
  right ("Resumed · New" affordance only when a session was resumed — existing
  `resumed` state drives it; RotateCcw icon + label), Close (`X`, title
  "Close (Esc)"/"关闭 (Esc)" — string kept).
- Zones separated by hairlines; spacing follows the app's 4/8 rhythm.

### Message rendering (block renderer)

A tiny **safe** renderer builds React elements from the assistant text (no
`dangerouslySetInnerHTML`; all text is escaped by React by default). Line-based
block parser:

- Lines starting with ` ``` ` switch a mono fenced block (rendered as a rounded
  mono panel, `bg-foreground/5`, `font-mono`, small).
- Lines that start a JSON/config fragment (first non-space char `{`, or contain
  `:::decision-summary`) render as a mono panel block (builder config sketches).
- `#` / `##` / `###` prefixes → section title weight.
- `- ` / `* ` prefixes → list rows (bulleted).
- `**bold**` and `` `code` `` inline tokens in plain paragraphs.
- Everything else = normal paragraph.

Assistant rows: tiny mark avatar + card (`bg-muted`, hairline border, rounded).
User rows: right-aligned primary bubble (existing color semantics), refined radius.
While streaming, the last assistant bubble shows an animated caret.
Session-start failure bubble: destructive tone (restyled, same inline behavior).

### Composer

- Autosize `<textarea>` (1–4 rows); Enter sends, Shift+Enter newline.
- Send button (lucide `Send`); while streaming it becomes **Stop** (square,
  outline/destructive styling) that aborts the in-flight `AbortController`
  (reuse `abortRef`).
- Hint row under composer: "Enter ↵ send · Esc close" (zh equivalent), muted,
  small. Right side may host the "New session" chip if header space is tight —
  default is header.
- Disabled states: reduced opacity + `disabled` semantics.

### Motion & accessibility

- Mount slide+fade (~200ms ease-out) only when the panel opens (component returns
  null when closed; exit animation is out of scope — the parent unmounts it).
- Built-in pulses/blinks (`animate-pulse` for the empty-state last node, caret
  blink via `animate-pulse` on the caret span).
- `@media (prefers-reduced-motion: reduce)` disables the panel animation.
- Focus rings on all interactive elements; icon-only buttons carry `aria-label` /
  `title`; textarea is labelled ("Describe your workflow" / zh); status is never
  color-only (pills include text).
- Keyboard paths preserved: Escape closes, Enter sends.

### Keyframes plumbing

If the project has no keyframe infra for a slide/fade, add two small keyframes to
`frontend/src/index.css` (`panel-in`, `caret-blink`) in the existing CSS-in-Tailwind
pattern. Keep additive and tiny; no global visual changes.

## Testing

- Extend `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx`
  (source-contract convention — regex over SOURCE, no React boot). Keep all existing
  assertions green (workflow_builder, workflow_id, streamAgentResponse,
  onFlowSaved, createAgentConversation, open-gate, close-on-Escape string).
  New assertions: starter prompt strings (EN title of at least one card), the
  Stop affordance string, block-renderer helper name(s) referenced in source,
  "or describe your own" microcopy.
- Existing behavior tests must not regress: `WorkflowBuilderChat.test.jsx` full
  pass, `npm run build` success, and the WorkflowCanvas source-contract tests
  (button string untouched).

## Browser verification (after build)

Open `/my-space/workflow/<3-node flow>` → click **Build with AI** → panel slides in;
empty state shows the blueprint + 3 starter cards; click a card → prompt appears as
a user bubble, assistant streams with structured blocks + caret; Stop halts the
stream; Esc closes. Verify with the canvas-verification conventions
(chunk-hash check via `performance.getEntriesByType('resource')`, string-literal
grep of the built chunk — do NOT grep comments or function names).

## Files

- `frontend/src/components/workflow/WorkflowBuilderChat.jsx` — render/UX rewrite;
  state machine, session logic, stream loop unchanged.
- `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx` — extended
  source-contract tests.
- `frontend/src/index.css` — only if keyframes are missing (2 tiny keyframes).

## Out of scope (future phase candidates)

- Center-stage wizard with live graph-preview pane (needs backend graph events).
- Backend typed "phase" events (designing/validating/saving) — inferred only from
  existing events in this phase.
- Canvas-side success toast (currently the canvas reload IS the feedback).
