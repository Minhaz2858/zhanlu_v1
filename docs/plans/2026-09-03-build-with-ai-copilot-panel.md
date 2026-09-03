# Build With AI — Copilot Studio Panel Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Turn the workflow canvas "Build with AI" slide-over into a professional AI-builder surface — blueprint empty state with starter prompts, structured streaming output, upgraded composer — without touching backend or stream contracts.

**Architecture:** Single-component frontend redesign of `WorkflowBuilderChat.jsx` + its source-contract test file. All session/stream logic (resume, flow_saved detection, abort) is preserved verbatim; only rendering/UX layers change. Existing tailwind keyframes (`slide-in-right`, `fade-in`, `slide-up`, `pulse`) are reused — NO tailwind.config or index.css change needed.

**Tech Stack:** React 18, Tailwind (shadcn-style tokens), lucide-react, vitest source-contract tests.

**Design doc:** `docs/superpowers/specs/2026-09-03-build-with-ai-copilot-panel-design.md` (commit 2151d29).

---

## Conventions (READ FIRST)

- Working tree is VERY dirty with unrelated uncommitted work — stage ONLY the files you touch; NEVER `git add -A` / `git commit -am`.
- Target files are CLEAN at HEAD: `frontend/src/components/workflow/WorkflowBuilderChat.jsx`, `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx` (verify with `git status --short <file>` before starting).
- Frontend tests: RAM-tight Mac — `pkill -f vitest` BEFORE each vitest run; run ONE test file at a time from `frontend/`:
  `cd /Users/minhazulislam/Desktop/zhanlu_sep_1_n8n/frontend && npx vitest run src/components/workflow/WorkflowBuilderChat.test.jsx`
  ECONNRESET at exit is noise; the pass line is the result. NEVER the full suite.
- Test convention: **source-contract** — the test reads the component SOURCE as text (regex, no React boot). See the existing test file header.
- The existing test file pins MANY strings with exact whitespace. PRESERVE these verbatim (do not reformat the code around them):
  - `createAgentConversation(\n          'workflow_builder',` — whitespace-sensitive (10 spaces)
  - `evt.type === 'flow_saved'` must appear BEFORE `evt.type === 'delta'` in source
  - `existing.length > 0`, `existing[0]`, `conv.messages`, `setResumed(true)`, `limit: 1`
  - `AbortController` / `.abort()` / `signal`, `cancelled = true`, `abortRef.current.abort()`
  - `if (!open) return null` (own line), `lang === 'en'`, `Build with AI`, `AI 构建`, `Escape`, `addEventListener('keydown'`
  - `RotateCcw`, `startNewSession`, `setResumed(false)`, `setMessages([])`, lucide imports of X / Send / RotateCcw
- TWO deliberate test-pin updates (spec-driven): `w-[380px]` → `w-[400px]`, and the `Sparkles` lucide import pin → a `Workflow` lucide import pin. All other pins stay.

---

### Task 1: Empty-state blueprint + starter prompts

**Objective:** When no messages exist, show a designed builder empty state: mini pipeline graphic (CSS chips) + 3 starter-prompt cards (click → send) + microcopy. When history exists, this area is simply absent (existing `messages.length === 0` gate).

**Files:**
- Modify: `frontend/src/components/workflow/WorkflowBuilderChat.jsx`
- Test: `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx`

**Step 1: Extend the source-contract test with new assertions** (append inside the existing `describe` block, before the closing `});`):

```js
it('ships a blueprint empty state with starter prompts (copilot redesign)', () => {
  expect(SOURCE).toMatch(/import\s*\{[^}]*\bWorkflow\b[^}]*\}\s*from\s*['"`]lucide-react['"`]/);
  // At least one starter card EN title + the send-on-click intent.
  expect(SOURCE).toMatch(/Sales report pipeline/);
  expect(SOURCE).toMatch(/or describe your own workflow below/i);
  // Clicking a card must send the prompt through the existing send path.
  expect(SOURCE).toMatch(/handleSend\s*\(/);
  expect(SOURCE).toMatch(/STARTERS/);
});
```

**Step 2:** Run the ONE test file → expect FAIL (new assertions; also `Workflow` import absent).

**Step 3: Implement the empty state.** Keep the `Sparkles` import REMOVED only in Task 3 (header); in this task import `Workflow` from lucide-react as well (unused import lint will pass only once used — so implement the full empty state now, using `Workflow` in the mark tile or the mini-pipeline).

Module-level constant (top of file, after imports — used by Task 1 and referenced by tests):

```js
const STARTERS = [
  {
    id: 'sales',
    icon: 'BarChart3',
    en: { title: 'Sales report pipeline', prompt: 'A weekly sales report workflow: pull sales from the database, summarize top products and customers, and output a markdown brief.' },
    zh: { title: '销售报告流水线', prompt: '创建一个每周销售报告工作流：从数据库拉取销售数据，汇总重点产品和客户，并输出 Markdown 简报。' },
  },
  {
    id: 'triage',
    icon: 'Inbox',
    en: { title: 'Feedback triage', prompt: 'Classify incoming customer feedback by topic and route urgent complaints to a separate alert branch.' },
    zh: { title: '反馈分流', prompt: '将客户反馈按主题分类，并把紧急投诉路由到单独的告警分支。' },
  },
  {
    id: 'research',
    icon: 'Search',
    en: { title: 'Research assistant', prompt: 'Research a topic with web search, summarize the findings, and save a short brief.' },
    zh: { title: '研究助手', prompt: '用联网搜索研究一个主题，总结发现，并保存一份简短简报。' },
  },
];
```

Empty-state JSX replacing the current one-line `<p>` inside the `messages.length === 0 && (...)` block (styling uses the app's shadcn tokens; icon map: `const starterIcon = { BarChart3, Inbox, Search }` — import those three from lucide-react too):

- A small centered mini-pipeline: three chips (lucide `Zap` → `Bot` → `PanelTop`/`FileOutput` — import what you use) joined by a subtle connector line, the LAST chip wrapping a pulsing dot (`<span className="absolute ... animate-pulse" />`). Chips are muted rounded tiles with hairline borders. Caption under: "Describe it — I'll draft the workflow" (EN) / "描述它，我来起草工作流" (zh).
- Three starter cards: `<button>` rows (left icon in a muted tile, title semibold, one-line muted description), hover ring/translate, `onClick={() => { setInput(prompt); handleSendWith(prompt); }}` — simplest correct approach: call the SAME send path by setting a ref/state. To keep the stream logic untouched, add a tiny wrapper used only by chips:

```js
async function sendPrompt(text) {
  if (!text.trim() || streaming) return;
  setInput(text);           // show what is being sent
  setInputValueForSend(text); // see note
}
```

NOTE — do NOT refactor `handleSend`. Cleanest minimal wiring that reuses the existing handler and its state: chips set `input` then call `handleSend()` AFTER React commits is racy. Instead refactor `handleSend` to take an optional argument: `async function handleSend(prefill) { const text = (prefill ?? input).trim(); ... }` — this is a 1-line change to the existing function; keep the rest identical (the source-contract pin `createAgentConversation(\n          'workflow_builder',` lives in startNewSession, not handleSend — unaffected). Chip onClick: `() => handleSend(prompt)`.
- Microcopy row under the cards: "…or describe your own workflow below." / "…或在下方向我描述你的工作流。" muted xs.
- Keep zh/EN parity for every string (`isEn ? … : …`).

**Step 4:** Run the ONE test file → PASS. Also run `npx eslint src/components/workflow/WorkflowBuilderChat.jsx` (from frontend/) → 0 errors.

**Step 5: Commit:**
```bash
git add frontend/src/components/workflow/WorkflowBuilderChat.jsx frontend/src/components/workflow/WorkflowBuilderChat.test.jsx
git commit -m "feat(workflow): Build with AI blueprint empty state + starter prompts"
```

---

### Task 2: Structured stream renderer (block parser + streaming caret)

**Objective:** Assistant message content renders as light structure — mono panels for fenced/JSON/config fragments, section titles, bullets, inline bold/code — via a safe React block renderer; streaming bubble shows a pulsing caret.

**Files:**
- Modify: `frontend/src/components/workflow/WorkflowBuilderChat.jsx`
- Test: `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx`

**Step 1: Append source-contract assertions:**

```js
it('renders assistant stream content through a safe block renderer (copilot redesign)', () => {
  expect(SOURCE).toMatch(/function\s+renderBlocks|const\s+renderBlocks\s*=/);
  expect(SOURCE).toMatch(/isMonoLine|```|:::decision-summary/);
  // Streaming caret shown while streaming.
  expect(SOURCE).toMatch(/animate-pulse/);
  expect(SOURCE).toMatch(/text-[1em]|caret|▍|▪/);
});
```

**Step 2:** Run the ONE test file → FAIL.

**Step 3: Implement** (module-level pure helpers; React elements only — NO `dangerouslySetInnerHTML`):

```jsx
function isMonoLine(line) {
  const t = line.trim();
  return t.startsWith('```') || t.startsWith('{') || t.startsWith(':::') || t.startsWith('[') && /json|config/i.test(line);
}

function renderInline(text) {
  // **bold** and `code` — split on tokens, keep plain text escaped by React.
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) return <strong key={i}>{p.slice(2, -2)}</strong>;
    if (p.startsWith('`') && p.endsWith('`')) return <code key={i} className="rounded bg-foreground/10 px-1 py-0.5 font-mono text-[0.85em]">{p.slice(1, -1)}</code>;
    return p;
  });
}

function renderBlocks(content) {
  // Line-based parser: fenced/mono runs, #/## headers, -/* bullets, else paragraphs.
  const lines = String(content || '').split('\n');
  const blocks = [];
  let i = 0;
  let mono = null;
  const pushMono = () => { if (mono !== null) { blocks.push(<pre key={blocks.length} className="overflow-x-auto rounded-md bg-foreground/5 p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">{mono.join('\n')}</pre>); mono = null; } };
  for (; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.trim();
    if (mono !== null) { if (line.startsWith('```')) pushMono(); else mono.push(raw); continue; }
    if (line.startsWith('```')) { mono = []; continue; }
    if (isMonoLine(raw)) { blocks.push(<pre key={blocks.length} ...same styling...>{raw}</pre>); continue; }
    if (/^#{1,3}\s/.test(line)) blocks.push(<p key={blocks.length} className="text-xs font-semibold text-foreground">{renderInline(line.replace(/^#{1,3}\s*/, ''))}</p>);
    else if (/^[-*]\s/.test(line)) blocks.push(<p key={blocks.length} className="pl-2 text-[13px] leading-relaxed"><span className="mr-1.5 text-muted-foreground">•</span>{renderInline(line.replace(/^[-*]\s*/, ''))}</p>);
    else if (line === '') blocks.push(<div key={blocks.length} className="h-1.5" />);
    else blocks.push(<p key={blocks.length} className="text-[13px] leading-relaxed">{renderInline(raw)}</p>);
  }
  pushMono();
  return blocks;
}
```

(Use your judgment on exact Tailwind sizes so 13px body fits the 400px panel; keep classes consistent with the rest of the app.)

Wire it in: assistant bubble content becomes `<div className="space-y-1">{renderBlocks(m.content)}</div>`; while `streaming && i === messages.length - 1 && !m.content` show the caret: append `<span className="animate-pulse">▍</span>`. User bubble content stays plain text (no parser). Session-error bubbles keep plain text but gain destructive styling (`border-destructive/30 bg-destructive/5 text-destructive`-ish, keep the `_sessionError` flow identical).

**Step 4:** Run the ONE test file → PASS; eslint → 0 errors.

**Step 5: Commit:**
```bash
git add frontend/src/components/workflow/WorkflowBuilderChat.jsx frontend/src/components/workflow/WorkflowBuilderChat.test.jsx
git commit -m "feat(workflow): structured stream renderer + streaming caret in Build with AI"
```

---

### Task 3: Header identity + shell width (status pill, mark tile)

**Objective:** Panel reads as a professional builder: 400px shell, mark tile (Workflow icon) instead of Sparkles, title + status pill ("Resumed" chip + New-session affordance), hairline zones.

**Files:**
- Modify: `frontend/src/components/workflow/WorkflowBuilderChat.jsx`
- Test: `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx`

**Step 1: Update the two deliberate pins in the test file:**

```js
// in the 'renders a right slide-over panel' test:
expect(SOURCE).toMatch(/w-\[400px\]/);
// replace the Sparkles import pin with the Workflow import pin (already added in Task 1 — drop the duplicate Sparkles assertion):
```
Remove `expect(SOURCE).toMatch(/Sparkles/)`; the Workflow import assertion added in Task 1 now covers the icon pin.

**Step 2:** Run the ONE test file → FAIL (Sparkles removal not yet implemented + width still 380 + old Sparkles import assert removed so the failing bits are: w-[400px] missing).

**Step 3: Implement:**
- Outer shell: `w-[380px]` → `w-[400px]`. Keep `fixed inset-y-0 right-0 z-50 flex flex-col border-l bg-background shadow-xl`.
- Header JSX (keep Escape/close semantics; keep `X`, `RotateCcw` imports and the `resumed && !streaming` New button condition):
  - Mark tile: `<div className="flex h-7 w-7 items-center justify-center rounded-md bg-gradient-to-br from-primary/90 to-primary/60 text-primary-foreground"><Workflow className="h-4 w-4" /></div>` (tune gradient to existing primary; restrained).
  - Title column: h2 title + tiny status line under it when resumed: `{resumed ? (isEn ? 'Resumed session' : '已恢复会话') : (isEn ? 'AI workflow builder' : 'AI 工作流构建')}` muted 10px — gives the "session aware" feel.
  - Right side: existing New (RotateCcw) when resumed, then Close (X). Keep `title="Close (Esc)"` string.
- Remove the now-unused `Sparkles` import from the lucide-react import line (keep X, Send, Loader2, RotateCcw, Workflow, and any icons added in Task 1/2).
- Body/header separators: ensure `border-b` on header, `border-t` on composer (already there); keep spacing `px-4 py-3`.

**Step 4:** Run the ONE test file → PASS (all pins incl. updated width + Workflow import). eslint → 0 errors.

**Step 5: Commit:**
```bash
git add frontend/src/components/workflow/WorkflowBuilderChat.jsx frontend/src/components/workflow/WorkflowBuilderChat.test.jsx
git commit -m "feat(workflow): Build with AI header identity + 400px shell"
```

---

### Task 4: Composer upgrade (autosize textarea, Stop, hint row)

**Objective:** Composer = labelled autosize textarea (Enter send / Shift+Enter newline), Send → Stop while streaming (aborts via existing AbortController), hint row with keyboard hints.

**Files:**
- Modify: `frontend/src/components/workflow/WorkflowBuilderChat.jsx`
- Test: `frontend/src/components/workflow/WorkflowBuilderChat.test.jsx`

**Step 1: Append source-contract assertions:**

```js
it('composer: autosize textarea, Stop affordance and keyboard hints (copilot redesign)', () => {
  expect(SOURCE).toMatch(/<textarea/);
  expect(SOURCE).toMatch(/stop/i);
  expect(SOURCE).toMatch(/Shift\+Enter|Shift.*Enter/);
  expect(SOURCE).toMatch(/Enter/);
  expect(SOURCE).toMatch(/stopStreaming|abortRef\.current\?\.abort/);
});
```

**Step 2:** Run the ONE test file → FAIL.

**Step 3: Implement** (keep `handleSend` behavior; do NOT break `disabled={streaming}` pins — the existing "disables send while streaming" test still checks `streaming` and `disabled` appear, which they will):

- Replace the `<input>` with an autosize textarea:
```jsx
<textarea
  value={input}
  onChange={(e) => { setInput(e.target.value); e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 96) + 'px'; }}
  onKeyDown={(e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  }}
  disabled={streaming}
  rows={1}
  aria-label={isEn ? 'Describe your workflow' : '描述你的工作流'}
  placeholder={isEn ? 'Describe your workflow…' : '描述你的工作流…'}
  className="max-h-24 min-h-9 flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
/>
```
- Button: when `streaming`, render Stop: `<Button variant="outline" size="icon" onClick={() => { abortRef.current?.abort(); }} title={isEn ? 'Stop' : '停止'} aria-label={isEn ? 'Stop' : '停止'}><Square className="h-3.5 w-3.5 fill-current" /></Button>` (import `Square` from lucide-react; keep the existing spinner? — while streaming the button is Stop, so drop Loader2 from the button but keep Loader2 import if still used elsewhere; if unused remove it). When idle: Send icon button as today.
- Hint row under the input row (`border-t-0 pt-1.5`):
```jsx
<div className="flex items-center justify-between px-4 pb-2 text-[10px] text-muted-foreground">
  <span>{isEn ? 'Enter ↵ to send · Shift+Enter for a new line' : 'Enter ↵ 发送 · Shift+Enter 换行'}</span>
  <span>{isEn ? 'Esc closes' : 'Esc 关闭'}</span>
</div>
```
(Composer zone container may need `pb-0`/restructure so hint sits under the input row inside the border-t area — adjust classes, keep zone visual clean.)

**Step 4:** Run the ONE test file → PASS; eslint → 0 errors.

**Step 5: Commit:**
```bash
git add frontend/src/components/workflow/WorkflowBuilderChat.jsx frontend/src/components/workflow/WorkflowBuilderChat.test.jsx
git commit -m "feat(workflow): Build with AI composer — textarea, stop, keyboard hints"
```

---

### Task 5: Motion, a11y polish + full frontend verification

**Objective:** Mount slide/fade on the panel with reduced-motion respect; polish focus/aria; then prove the whole file green + bundle builds with the new literals.

**Files:**
- Modify: `frontend/src/components/workflow/WorkflowBuilderChat.jsx` (wrapper classes only — no logic)
- Test: none new

**Step 1: Implement motion:**
- Outer div: add `animate-slide-in-right` (existing tailwind token; 0.25s ease-out) — BUT respect reduced motion: use a wrapper that applies the class conditionally via a tiny hook:
```js
const [reducedMotion] = useState(() => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
```
`className={... ${reducedMotion ? '' : 'animate-slide-in-right'}}`. (Simplest compliant approach; no global CSS touched.)
- Empty-state chips already animate via pulse on the pipeline's last node; ensure nothing else loops.

**Step 2: a11y pass:**
- Icon-only buttons already carry `title`; add `aria-label` matching title text on Close and New buttons.
- Starter cards are `<button>`s (focusable) — add visible focus ring styling (`focus-visible:ring-2 focus-visible:ring-ring`).
- Ensure the composer textarea `aria-label` from Task 4 remains.

**Step 3: Full verification:**
```bash
pkill -f vitest; sleep 1
cd /Users/minhazulislam/Desktop/zhanlu_sep_1_n8n/frontend && npx vitest run src/components/workflow/WorkflowBuilderChat.test.jsx 2>&1 | tail -4
```
Expect ALL tests pass (existing + new). Then eslint 0 errors. Then:
```bash
npm run build 2>&1 | tail -5
grep -rl "Sales report pipeline" dist/assets/ | head -2
grep -rl "or describe your own workflow" dist/assets/ | head -2
```
Both greps must hit the new main chunk.

**Step 4: Commit** (only if edits surfaced; otherwise no commit):
```bash
git add frontend/src/components/workflow/WorkflowBuilderChat.jsx
git commit -m "feat(workflow): Build with AI mount motion + a11y polish"   # or skip
```

---

### Task 6: Browser verification (destructive-free E2E)

**Objective:** Prove the redesign renders and behaves in the real app; no workflow data is mutated (chat messages DO persist to the builder conversation — acceptable and expected; do NOT alter the canvas graph).

**Files:** none

**Environment:** frontend http://localhost:8088 (nginx serves the new dist after Task 5 build), backend http://localhost:5002 up. Login admin@zhanlu.dev / admin123. Canvas: http://localhost:8088/my-space/workflow/d5288318-bbd1-48d6-9816-e450b90d4707 (Marketing Agent Workflow, 3 nodes).

**Step 1:** Open the canvas. Click **Build with AI** (header button, `MessageSquare` icon). Verify: panel slides in; header shows the mark tile + "Build with AI"; empty state shows the mini-pipeline + 3 starter cards (EN) + microcopy.

**Step 2:** Click the **Sales report pipeline** card → the prompt appears as a user bubble and the assistant streams. While streaming: verify the Stop button is present, structured text rendering appears (if the stream contains headers/bullets/code they render as such — at minimum confirm no raw crash and the caret/pulse is visible on the last bubble). Click **Stop** mid-stream → streaming stops cleanly (button returns to Send; no error bubble).

**Step 3:** Type a prompt manually (e.g. "add an output node to summarize") → Enter sends. **Esc** closes the panel. Reopen → the previous messages are there (resume worked) and the header shows the "Resumed session" status line + New button.

**Step 4:** Console check: `browser_console` → no uncaught errors after each major action. Chunk-hash check: `performance.getEntriesByType('resource')` includes the new main chunk (contains "Sales report pipeline" per Task 5 grep).

**Step 5:** Do NOT save/alter the workflow graph. Report: numbered observations, console errors (expect none), whether resume/status pill behaved, and the exact chunk URL that served the new literals.

---

## Definition of Done

- Panel: 400px, mark-tile header with status line, blueprint empty state + 3 starter cards (EN/ZH), structured stream rendering with caret, composer textarea + Stop + hints, mount motion respecting reduced motion.
- All existing stream/session/abort logic byte-identical in behavior; source-contract test file passes in full (old pins preserved except the 2 deliberate updates).
- `npm run build` green; new literals present in the served chunk.
- Browser E2E: open → starter send → stream → stop → esc → resume all verified; no console errors; canvas graph untouched.
