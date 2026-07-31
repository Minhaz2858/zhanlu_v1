# Skill Agent / Agent Builder: Wait-for-input guided flow

**Status**: Approved (defaults chosen for §9 open questions)
**Date**: 2026-07-28
**Scope**: `frontend/src/pages/SkillAgent.jsx`, `frontend/src/pages/AgentBuilder.jsx`, `frontend/src/hooks/useAgentBuilder.js`, `frontend/src/lib/translations.js`
**Bug report**: "It's automatic building skills but it needs at first user input, then based on the user input the Skill Agent needs to create the skills for the user."

---

## 1. Root cause

There are **four auto-fire call sites** where generic suggestion text triggers `startNewChat(...)`, which inside `setTimeout(() => handleSend(prefillText, conv), 100)` immediately sends the prefilled text to the backend **without waiting for the user to edit, confirm, or even read it**.

| # | File | Line | Trigger | Current call |
|---|------|------|---------|--------------|
| 1 | `SkillAgent.jsx` | 52-53 | URL `?action=create` | `startNewChat(t.skillAgent.suggestions.create)` |
| 2 | `SkillAgent.jsx` | 358 | Suggestion card `onClick` (4 cards) | `startNewChat(t.skillAgent.suggestions[s.key], { deferSend: s.key === 'learn' })` |
| 3 | `AgentBuilder.jsx` | 63 | URL `?prefill=...` | `startNewChat(prefill)` |
| 4 | `AgentBuilder.jsx` | 112 | Suggestion card `onClick` (3 cards) | `startNewChat(t.agentBuilder.suggestions[s.key])` |

`startNewChat` implementations:
- **`SkillAgent.jsx:123`** — `if (prefillText) { setInput(prefillText); if (!opts.deferSend) setTimeout(() => handleSend(prefillText, conv), 100); }`. Only the `learn` suggestion passes `deferSend: true`, so the other 3 suggestions and `?action=create` auto-send.
- **`useAgentBuilder.js:416`** — `setTimeout(() => handleSend(promptText, conv), 100)` unconditionally. No `deferSend` option exists.

**Intentional auto-send paths (must be preserved):**
- `SkillAgent.jsx:50` `?skill={id}` → `startWithSkill(skillId)` — user clicked a specific skill to edit.
- `SkillAgent.jsx:47` `?files=urls` → `startWithFiles(urls)` — user uploaded/dropped files.
- `AgentBuilder.jsx:64` `?edit={id}` → `startWithEdit(editId)` — user clicked a specific agent to edit.

These are user-initiated, object-specific actions where auto-send is the expected UX.

---

## 2. Approach

Replace the four auto-fire call sites with a new `applySuggestion(text)` helper that **only** prefills the input and focuses it — never creates a conversation, never calls the backend. The user then edits/confirms and presses Enter (which goes through the existing intentional `startNewChat(input)` path on the empty state, or `handleSend(input)` once a conversation exists).

```javascript
const applySuggestion = (text) => {
  setInput(text);
  setPrefilledHint(true);
  inputRef.current?.focus();
};
```

`prefilledHint` is a new boolean state that renders a small dismissible pill above the input explaining what just happened ("Suggestion applied — edit and press Enter to send"). It clears on first input mutation, send, or dismiss.

The empty state is also redesigned: suggestion cards become clearly "pre-fill only", a divider + upload CTA is added, and a chip row of existing skills/agents is shown so the user can pick up where they left off.

`startNewChat` itself is **not** removed — it remains the intentional path for Enter-on-empty-state and for the preserved auto-send URLs (`?skill`, `?files`, `?edit`).

---

## 3. Component changes

### 3.1 `frontend/src/pages/SkillAgent.jsx`

**Add state (near other `useState` calls, ~line 33):**
```javascript
const [prefilledHint, setPrefilledHint] = useState(false);
```

**Add helper (near `startNewChat`, ~line 140):**
```javascript
function applySuggestion(text) {
  setInput(text);
  setPrefilledHint(true);
  inputRef.current?.focus();
}
```

**Replace line 52-53** (`?action=create` branch):
```javascript
} else if (action === 'create') {
  applySuggestion(t.skillAgent.suggestions.create);
}
```

**Replace line 358** (suggestion card `onClick`):
```jsx
<button
  key={s.key}
  onClick={() => applySuggestion(t.skillAgent.suggestions[s.key])}
  className="...existing classes..."
>
```
(Remove the `{ deferSend: s.key === 'learn' }` argument — all four cards now use `applySuggestion` uniformly.)

**`startNewChat` (line 123-139)**: unchanged. Still used by:
- `?skill=` (via `startWithSkill`, line 141)
- `?files=` (via `startWithFiles`, line 160)
- Enter-on-empty-state (line 327): `showEmpty ? startNewChat(input) : handleSend(input)`
- Send button on empty state (line 335): same

**`renderInputArea` (line 311-340)**: add the prefilled hint pill above the textarea, and a `onChange`/`onKeyDown` clear hook:
```jsx
{prefilledHint && (
  <div className="flex items-center gap-1.5 px-3 pt-2">
    <span className="inline-flex items-center gap-1 rounded-md bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
      {t.skillAgent.prefilledHint}
      <button onClick={() => setPrefilledHint(false)} className="text-primary/60 hover:text-primary">
        <X className="h-3 w-3" />
      </button>
    </span>
  </div>
)}
<textarea
  ...
  onChange={(e) => { setInput(e.target.value); if (prefilledHint) setPrefilledHint(false); }}
  ...
/>
```

**Empty-state section (line 347-366)**: redesign to include upload CTA + existing-skills chip row:
```jsx
{showEmpty ? (
  <div className="flex flex-1 flex-col items-center justify-center px-6 pb-12">
    <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10">
      <Wrench className="h-7 w-7 text-primary" />
    </div>
    <h1 className="font-display text-3xl tracking-tight text-foreground">{t.skillAgent.title}</h1>
    <p className="mt-2 max-w-lg text-center text-sm text-muted-foreground">{t.skillAgent.description}</p>
    <p className="mt-1 text-xs text-muted-foreground">{t.skillAgent.madeBy}</p>
    <div className="mt-8 w-full max-w-2xl">
      {/* Suggestion cards — pre-fill only */}
      <div className="mb-4 flex flex-col gap-2">
        {SUGGESTIONS.map((s) => (
          <button key={s.key} onClick={() => applySuggestion(t.skillAgent.suggestions[s.key])} className="...">
            <s.icon className="h-4 w-4 shrink-0 text-primary" />
            <span>{t.skillAgent.suggestions[s.key]}</span>
          </button>
        ))}
      </div>

      {/* Divider + upload CTA (reuses SkillUploadButton, larger variant) */}
      <div className="mb-4 flex items-center gap-3">
        <div className="h-px flex-1 bg-border" />
        <span className="text-[11px] text-muted-foreground">{t.skillAgent.or}</span>
        <div className="h-px flex-1 bg-border" />
      </div>
      <div className="mb-4">
        <SkillUploadButton onUploaded={handleFilesUploaded} variant="cta" ctaLabel={t.skillAgent.emptyUploadCta} ctaHint={t.skillAgent.emptyUploadHint} />
      </div>

      {/* Existing skills chip row */}
      <ExistingSkillsChips onError={() => {}} />

      {renderInputArea(true)}
    </div>
  </div>
) : ...}
```

**Existing-skills chip row**: small inline component (or inline JSX) that calls `base44.entities.Tool.list` filtered by current owner (anonymous id fallback, mirroring `Toolkit.jsx:56`) and renders the top 8 as clickable chips. Clicking a chip navigates to `?skill={id}` (preserved auto-send path). Errors silently no-op (already-loaded chips remain).

### 3.2 `frontend/src/hooks/useAgentBuilder.js`

`startNewChat` (line 416-428) is **unchanged**. It is still called by the intentional paths.

Add a new `applySuggestion` callback that mirrors the SkillAgent helper, and expose it in the returned object so `AgentBuilder.jsx` can consume it:

```javascript
const applySuggestion = useCallback((text) => {
  setInput(text);
  setPrefilledHint(true);
  inputRef.current?.focus();
}, []);
```

Add new state:
```javascript
const [prefilledHint, setPrefilledHint] = useState(false);
```

Return both from the hook:
```javascript
return {
  ...
  prefilledHint,
  setPrefilledHint,
  applySuggestion,
  ...
};
```

### 3.3 `frontend/src/pages/AgentBuilder.jsx`

**Destructure new fields from `builder`** (line 27-57):
```javascript
const {
  ...,
  prefilledHint,
  setPrefilledHint,
  applySuggestion,
  ...
} = builder;
```

**Replace line 63** (`?prefill=...` branch):
```javascript
if (prefill) applySuggestion(prefill);
```

**Replace line 112** (suggestion card `onClick`):
```jsx
<button
  key={s.key}
  onClick={() => applySuggestion(t.agentBuilder.suggestions[s.key])}
  className="..."
>
```

**`renderInputArea` (line 70-89)**: add the prefilled hint pill (same pattern as SkillAgent).

**Empty-state section (line 101-120)**: add divider + existing-agents chip row (mirrors SkillAgent pattern, using `base44.entities.AgentApp.list`).

### 3.4 `frontend/src/lib/translations.js`

Add the following keys to **both** the `zh` block (around line 442 for `skillAgent`, line 460 for `agentBuilder`) and the `en` block (mirrored structure):

**`skillAgent` (both languages):**
```javascript
prefilledHint: '已填入建议——可编辑后按回车发送',  // en: 'Suggestion applied — edit and press Enter to send'
or: '或',                                                  // en: 'or'
emptyUploadCta: '上传 .zip / .skill / .md 技能文件',       // en: 'Upload .zip / .skill / .md skill file'
emptyUploadHint: '导入已有技能，由 Skill Agent 接管分析与改进', // en: 'Import an existing skill for Skill Agent to analyze and improve'
existingSkills: '你的技能',                                // en: 'Your skills'
backToToolkit: '返回工具包',                               // en: 'Back to toolkit'
```

**`agentBuilder` (both languages):**
```javascript
prefilledHint: '已填入建议——可编辑后按回车发送',  // en: 'Suggestion applied — edit and press Enter to send'
or: '或',                                                  // en: 'or'
existingAgents: '你的智能体',                              // en: 'Your agents'
backToAgents: '返回智能体列表',                            // en: 'Back to agents'
```

### 3.5 `SkillUploadButton` — `variant="cta"` prop

The existing `SkillUploadButton` component currently renders as a compact inline button. Add an optional `variant` prop (`'compact'` default, `'cta'` for the larger empty-state version). When `variant === 'cta'`, render with larger padding, primary-coloured drop zone, and accept the `ctaLabel`/`ctaHint` props for the visible text. This avoids creating a new component for the CTA (satisfies §9 question 2's default choice).

---

## 4. Behavior matrix

| Entry point | Before | After |
|-------------|--------|-------|
| `?action=create` URL | Auto-creates conv + sends `t.skillAgent.suggestions.create` to backend | Pre-fills input, focuses, shows hint pill. User edits + Enter → `startNewChat(input)` (intentional). |
| SkillAgent suggestion card click (collect/create/learn/edit) | Auto-creates conv + sends suggestion text (except `learn` which pre-filled only) | All four pre-fill input + focus + hint pill. User edits + Enter. |
| `?skill={id}` URL | Auto-creates conv + sends edit prompt | **Unchanged** — auto-send preserved (user clicked specific skill). |
| `?files=urls` URL | Auto-creates conv + sends process-files prompt | **Unchanged** — auto-send preserved (user dropped files). |
| Enter on empty state with input | `startNewChat(input)` → creates conv + sends | **Unchanged** — this is the intentional send path. |
| Send button on empty state with input | Same as above | **Unchanged**. |
| `?prefill=...` URL (AgentBuilder) | Auto-creates conv + sends prefill text | Pre-fills input, focuses, hint pill. User edits + Enter. |
| `?edit={id}` URL (AgentBuilder) | Auto-creates conv + sends edit prompt | **Unchanged** — auto-send preserved (user clicked specific agent). |
| AgentBuilder suggestion card click | Auto-creates conv + sends suggestion text | Pre-fills input + focus + hint pill. User edits + Enter. |
| Main `/agent` chat page | N/A | **Out of scope** (§7). Follow-up spec if reported. |

---

## 5. Edge cases

1. **User clicks suggestion then immediately clicks another** — second `applySuggestion` overwrites input, hint pill stays visible (already shown). No backend call ever fired. Safe.
2. **User clicks suggestion, edits, then reloads page** — input is local React state, lost on reload. Acceptable (matches current `?action=create` reload behavior).
3. **User clicks suggestion on a non-empty state (active conversation)** — suggestion cards are only rendered in the empty state (`showEmpty === true`), so this can't happen.
4. **`?action=create` arrives while another tab has an active conversation** — `applySuggestion` only sets input + focus; doesn't touch `activeId`/`messages`. Safe.
5. **Existing-skills chip fetch fails (network/permissions)** — chip row renders empty (or hides itself). Silent no-op, no error toast. Matches `Toolkit.jsx:56` pattern.
6. **Anonymous user (no auth)** — chip fetch uses `getAnonymousId()` fallback filter, same as `Toolkit.jsx:56`. If anonymous id is null/empty, chip row hides.
7. **`prefilledHint` pill dismissed** — `setPrefilledHint(false)` only hides the pill; input text remains. User can still press Enter.
8. **`prefilledHint` not cleared after send** — `handleSend`/`startNewChat` already call `setInput('')`; add `setPrefilledHint(false)` to both for cleanliness.
9. **User uploads a file via the new CTA on empty state** — `handleFilesUploaded` already appends to `pendingFiles`; the existing `pendingFiles` chip row in `renderInputArea` shows them. No change needed.

---

## 6. Testing strategy

This is a frontend-only behavioural change. No new backend tests required. Verification:

1. **Sucrase parse check**: after editing, run `npx sucrase ./frontend/src/pages/SkillAgent.jsx -t jsx` (and AgentBuilder.jsx) to confirm the JSX parses.
2. **Vite build**: `cd frontend && ./node_modules/.bin/vite build` — must succeed. Confirm new bundle hash in `dist/index.html`.
3. **String-presence check** in built bundle:
   ```bash
   grep -c 'prefilledHint\|Suggestion applied\|已填入建议' dist/assets/*.js
   ```
   Must be > 0.
4. **Manual smoke test** (post-build, hard refresh):
   - Navigate to `/skill-agent?action=create` → input should be pre-filled, no conversation created, no assistant message.
   - Click each of the 4 suggestion cards → input pre-filled, no chat started.
   - Edit input + press Enter → conversation created, message sent.
   - Navigate to `/skill-agent?skill={id}` → conversation auto-created with edit prompt (preserved behaviour).
   - Same matrix for `/agent-builder` with `?prefill=...` and `?edit={id}`.
5. **Regression**: existing intentional auto-send paths (`?skill=`, `?files=`, `?edit=`) still work.
6. **No new backend tests** — this change touches only React state and the `Tool.list`/`AgentApp.list` read paths that already have coverage.

---

## 7. Out of scope

- The main `/agent` chat page (`Chat.jsx`) has a similar URL-param-driven auto-send pattern. If users report it there, a follow-up spec will address it. Not touched in this spec to keep blast radius small.
- The `learn` suggestion's previous `deferSend: true` special case is removed (now all 4 suggestions use the same `applySuggestion` path). The previous special-casing was an inconsistent half-fix; this spec makes it uniform.
- No changes to the backend `skill_agent` or `agent_builder` agent runtimes, prompts, or tool whitelists.
- No changes to the `SkillFilePanel`, `BuilderMessageBubble`, `ChatThinkingIndicator`, or any other consumer of the conversation state.
- No A/B testing or feature flag — direct replacement.

---

## 8. Rollout plan

1. Implement on `feat/auth-hardening` branch (current branch; user chose to commit existing changes first then continue).
2. Single PR (or direct push to `feat/auth-hardening` since this is a dev branch) containing:
   - `SkillAgent.jsx` changes
   - `AgentBuilder.jsx` changes
   - `useAgentBuilder.js` changes
   - `translations.js` new keys (en + zh)
   - `SkillUploadButton.jsx` `variant="cta"` prop
   - Optional: extract `ExistingSkillsChips` / `ExistingAgentsChips` into small components under `frontend/src/components/skillagent/` and `frontend/src/components/agentbuilder/` respectively.
3. Run `vite build` and verify (per §6).
4. Ask user to hard-refresh and confirm the four entry points behave per the §4 matrix.
5. Memory note in `/root/.codebuddy/memory/2026-07-28.md`.

---

## 9. Open questions (resolved with defaults)

The user chose "Save spec + write plan first" without separately resolving these. Defaults below will be used; they can be revisited during code review.

1. **Wording for `prefilledHint` pill and chip-row labels.**
   - **Default**: "已填入建议——可编辑后按回车发送" / "Suggestion applied — edit and press Enter to send".
   - Chip-row labels: "你的技能" / "Your skills" (skillAgent); "你的智能体" / "Your agents" (agentBuilder).
   - These are short, actionable, and match the existing tone in `translations.js`.

2. **Upload CTA: new card component vs. larger `SkillUploadButton`.**
   - **Default**: Reuse `SkillUploadButton` with a new `variant="cta"` prop. Avoids component sprawl, keeps a single source of truth for upload logic.
   - If the prop API gets awkward during implementation, fall back to a thin `<EmptyStateUploadCta>` wrapper that composes `SkillUploadButton`.

3. **Whether to also fix the main `/agent` chat page in this spec.**
   - **Default**: No — out of scope (§7). `Chat.jsx` has its own URL-param handling and a different conversation lifecycle. A follow-up spec will be opened if users report the same auto-send surprise there.

These defaults are explicitly noted so reviewers can push back during plan review if any choice is wrong.
