# Skill Agent / Agent Builder Wait-for-Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Skill Agent and Agent Builder pages from auto-firing prefilled suggestion text to the backend; instead pre-fill the input, focus it, and wait for the user to edit and press Enter.

**Architecture:** Replace four `startNewChat(...)` auto-fire call sites with a new `applySuggestion(text)` helper that only sets input + shows a dismissible "Suggestion applied" pill + focuses. Redesign the empty state to include an upload CTA and a chip row of existing skills/agents. Preserve the intentional auto-send paths (`?skill=`, `?files=`, `?edit=`).

**Tech Stack:** React 18, Vite, Tailwind, lucide-react, vitest + jsdom, `@base44/sdk`.

## Global Constraints

- **No backend changes.** All edits are under `frontend/src/`.
- **No new test framework.** Use the existing vitest source-string scan pattern (see `src/components/chat/ChatInput.ungroupedChip.test.jsx` for the precedent). Do not introduce RTL rendering tests for these pages — the mocking surface is too large.
- **Test command:** `npx vitest run <path>` (never `npm test` with no args — broad collection risks OOM on the dev box per project memory).
- **Build command:** `cd frontend && ./node_modules/.bin/vite build` (run only in Task 9, not per-task — it takes 30-60s and spikes memory).
- **Translation parity:** Every new key must be added to BOTH the `zh` and `en` blocks in `translations.js`.
- **Intentional auto-send paths must remain untouched:** `startWithSkill`, `startWithFiles`, `startWithEdit`, and the Enter-key/send-button handlers on the empty state.
- **Frontend deployment:** Source edits do not take effect in the browser until `vite build` runs. Plan for the build in Task 9 + ask the user to hard-refresh.

## File Structure

**New files:**
- `frontend/src/components/common/PrefilledHintPill.jsx` — reusable pill with dismiss button. Props: `label: string`, `onDismiss: () => void`. Stateless.
- `frontend/src/components/skillagent/ExistingSkillsChips.jsx` — fetches `base44.entities.Tool.list` filtered by owner, renders top 8 as clickable chips. Clicking navigates to `/skill-agent?skill={id}`.
- `frontend/src/components/skillagent/ExistingSkillsChips.test.jsx` — source-scan regression test.
- `frontend/src/components/agentbuilder/ExistingAgentsChips.jsx` — same pattern with `base44.entities.AgentApp.list`, navigates to `/agent-builder?edit={id}`.
- `frontend/src/components/agentbuilder/ExistingAgentsChips.test.jsx` — source-scan regression test.
- `frontend/src/pages/SkillAgent.applySuggestion.test.jsx` — source-scan regression test pinning the 4 auto-fire sites are replaced.
- `frontend/src/pages/AgentBuilder.applySuggestion.test.jsx` — same for AgentBuilder.
- `frontend/src/components/common/PrefilledHintPill.test.jsx` — source-scan test.
- `frontend/src/components/skillagent/SkillUploadButton.variant.test.jsx` — source-scan test for the new `variant` prop.
- `frontend/src/hooks/useAgentBuilder.applySuggestion.test.jsx` — source-scan test.

**Modified files:**
- `frontend/src/lib/translations.js` — add new i18n keys (zh + en).
- `frontend/src/components/skillagent/SkillUploadButton.jsx` — add `variant="cta"` prop.
- `frontend/src/hooks/useAgentBuilder.js` — add `prefilledHint` state + `applySuggestion` callback; expose in return.
- `frontend/src/pages/SkillAgent.jsx` — replace 2 call sites, add pill, redesign empty state.
- `frontend/src/pages/AgentBuilder.jsx` — replace 2 call sites, add pill, redesign empty state.

---

## Task 1: Add i18n keys to translations.js

**Files:**
- Modify: `frontend/src/lib/translations.js` (zh block: `skillAgent` ~line 442, `agentBuilder` ~line 460; en block: mirrored structure later in file)
- Test: `frontend/src/lib/translations.skillAgentKeys.test.jsx`

**Interfaces:**
- Produces: `t.skillAgent.prefilledHint`, `t.skillAgent.or`, `t.skillAgent.emptyUploadCta`, `t.skillAgent.emptyUploadHint`, `t.skillAgent.existingSkills`, `t.skillAgent.backToToolkit`, `t.agentBuilder.prefilledHint`, `t.agentBuilder.or`, `t.agentBuilder.existingAgents`, `t.agentBuilder.backToAgents` — all in both `zh` and `en`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/lib/translations.skillAgentKeys.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './translations.js'), 'utf8');

const ZH_KEYS = [
  'prefilledHint', 'or', 'emptyUploadCta', 'emptyUploadHint',
  'existingSkills', 'backToToolkit',
];
const EN_KEYS_ZH = [
  'prefilledHint', 'or', 'existingAgents', 'backToAgents',
];

describe('translations.js — skillAgent / agentBuilder new keys', () => {
  it('zh skillAgent block has all new keys', () => {
    const zhSkillAgentBlock = SOURCE.match(/skillAgent:\s*\{[\s\S]*?\n\s{4}\}/);
    expect(zhSkillAgentBlock, 'zh skillAgent block not found').not.toBeNull();
    for (const k of ZH_KEYS) {
      expect(zhSkillAgentBlock[0], `missing key: skillAgent.${k}`).toMatch(
        new RegExp(`${k}:\\s*['"`]`),
      );
    }
  });

  it('zh agentBuilder block has all new keys', () => {
    const zhAgentBuilderBlock = SOURCE.match(/agentBuilder:\s*\{[\s\S]*?\n\s{4}\}/);
    expect(zhAgentBuilderBlock, 'zh agentBuilder block not found').not.toBeNull();
    for (const k of EN_KEYS_ZH) {
      expect(zhAgentBuilderBlock[0], `missing key: agentBuilder.${k}`).toMatch(
        new RegExp(`${k}:\\s*['"`]`),
      );
    }
  });

  it('en skillAgent block has all new keys', () => {
    // The en block is the second occurrence of skillAgent: in the file.
    const matches = [...SOURCE.matchAll(/skillAgent:\s*\{/g)];
    expect(matches.length, 'expected at least 2 skillAgent blocks (zh + en)').toBeGreaterThanOrEqual(2);
    const enStart = matches[1].index;
    const enSlice = SOURCE.slice(enStart, enStart + 2000);
    for (const k of ZH_KEYS) {
      expect(enSlice, `missing en key: skillAgent.${k}`).toMatch(
        new RegExp(`${k}:\\s*['"`]`),
      );
    }
  });

  it('en agentBuilder block has all new keys', () => {
    const matches = [...SOURCE.matchAll(/agentBuilder:\s*\{/g)];
    expect(matches.length, 'expected at least 2 agentBuilder blocks (zh + en)').toBeGreaterThanOrEqual(2);
    const enStart = matches[1].index;
    const enSlice = SOURCE.slice(enStart, enStart + 2000);
    for (const k of EN_KEYS_ZH) {
      expect(enSlice, `missing en key: agentBuilder.${k}`).toMatch(
        new RegExp(`${k}:\\s*['"`]`),
      );
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/lib/translations.skillAgentKeys.test.jsx`
Expected: FAIL — keys not found in source.

- [ ] **Step 3: Add the keys to the zh block**

In `frontend/src/lib/translations.js`, find the `skillAgent:` block in the `zh` section (around line 442). After the existing `madeBy:` line (or wherever fits cleanly), add inside the block:

```javascript
      prefilledHint: '已填入建议——可编辑后按回车发送',
      or: '或',
      emptyUploadCta: '上传 .zip / .skill / .md 技能文件',
      emptyUploadHint: '导入已有技能，由 Skill Agent 接管分析与改进',
      existingSkills: '你的技能',
      backToToolkit: '返回工具包',
```

Then in the `agentBuilder:` block in the `zh` section (around line 460), add:

```javascript
      prefilledHint: '已填入建议——可编辑后按回车发送',
      or: '或',
      existingAgents: '你的智能体',
      backToAgents: '返回智能体列表',
```

- [ ] **Step 4: Add the keys to the en block**

Find the mirrored `skillAgent:` and `agentBuilder:` blocks in the `en` section of the file. Add the equivalent English keys:

In `en.skillAgent`:
```javascript
      prefilledHint: 'Suggestion applied — edit and press Enter to send',
      or: 'or',
      emptyUploadCta: 'Upload .zip / .skill / .md skill file',
      emptyUploadHint: 'Import an existing skill for Skill Agent to analyze and improve',
      existingSkills: 'Your skills',
      backToToolkit: 'Back to toolkit',
```

In `en.agentBuilder`:
```javascript
      prefilledHint: 'Suggestion applied — edit and press Enter to send',
      or: 'or',
      existingAgents: 'Your agents',
      backToAgents: 'Back to agents',
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/lib/translations.skillAgentKeys.test.jsx`
Expected: PASS — all 4 assertions green.

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu && git add frontend/src/lib/translations.js frontend/src/lib/translations.skillAgentKeys.test.jsx && git commit -m "feat(i18n): add prefilledHint, upload CTA, and chip-row keys for skillAgent + agentBuilder

Adds zh + en keys: prefilledHint, or, emptyUploadCta, emptyUploadHint,
existingSkills/backToToolkit, existingAgents/backToAgents. Used by the
wait-for-input guided flow (see spec 2026-07-28-skill-agent-wait-for-input)."
```

---

## Task 2: Create `PrefilledHintPill` shared component

**Files:**
- Create: `frontend/src/components/common/PrefilledHintPill.jsx`
- Test: `frontend/src/components/common/PrefilledHintPill.test.jsx`

**Interfaces:**
- Produces: `export default function PrefilledHintPill({ label, onDismiss })` — renders a `<div>` with primary-tinted background, the label text, and an X button that calls `onDismiss`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/common/PrefilledHintPill.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './PrefilledHintPill.jsx'), 'utf8');

describe('PrefilledHintPill.jsx', () => {
  it('exports a default component taking label + onDismiss props', () => {
    expect(SOURCE).toMatch(/export\s+default\s+function\s+PrefilledHintPill\s*\(\s*\{\s*label\s*,\s*onDismiss\s*\}\s*\)/);
  });

  it('renders the label text inside the pill', () => {
    expect(SOURCE).toMatch(/\{label\}/);
  });

  it('renders an X dismiss button that calls onDismiss', () => {
    expect(SOURCE).toMatch(/<X\s+className=/);
    expect(SOURCE).toMatch(/onClick=\{\(\)\s*=>\s*onDismiss\(\)\}/);
  });

  it('returns null when label is falsy', () => {
    expect(SOURCE).toMatch(/if\s*\(\s*!label\s*\)\s*return\s*null/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/common/PrefilledHintPill.test.jsx`
Expected: FAIL — file not found.

- [ ] **Step 3: Create the component**

Create `frontend/src/components/common/PrefilledHintPill.jsx`:

```jsx
import { X } from 'lucide-react';

export default function PrefilledHintPill({ label, onDismiss }) {
  if (!label) return null;
  return (
    <div className="flex items-center gap-1.5 px-3 pt-2">
      <span className="inline-flex items-center gap-1 rounded-md bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
        {label}
        <button
          type="button"
          onClick={() => onDismiss && onDismiss()}
          className="text-primary/60 hover:text-primary"
          aria-label="Dismiss suggestion"
        >
          <X className="h-3 w-3" />
        </button>
      </span>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/common/PrefilledHintPill.test.jsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/common/PrefilledHintPill.jsx frontend/src/components/common/PrefilledHintPill.test.jsx && git commit -m "feat(ui): add reusable PrefilledHintPill component

Stateless pill that shows a label and a dismiss button. Used by SkillAgent
and AgentBuilder to indicate that a suggestion has been pre-filled into
the input and the user can edit + Enter to send."
```

---

## Task 3: Add `variant="cta"` prop to `SkillUploadButton`

**Files:**
- Modify: `frontend/src/components/skillagent/SkillUploadButton.jsx`
- Test: `frontend/src/components/skillagent/SkillUploadButton.variant.test.jsx`

**Interfaces:**
- Produces: `export default function SkillUploadButton({ onUploaded, disabled, variant, ctaLabel, ctaHint })`.
  - `variant`: `'compact'` (default, current behaviour) or `'cta'` (larger drop-zone button for empty state).
  - `ctaLabel`, `ctaHint`: strings rendered only when `variant === 'cta'`.
  - The `cta` variant renders a larger primary-tinted button (e.g. `h-14`, `bg-primary/10`, `border-primary/30`) that opens the same dropdown menu.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/skillagent/SkillUploadButton.variant.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './SkillUploadButton.jsx'), 'utf8');

describe('SkillUploadButton.jsx — variant="cta" support', () => {
  it('accepts variant, ctaLabel, ctaHint props', () => {
    expect(SOURCE).toMatch(/export\s+default\s+function\s+SkillUploadButton\s*\(\s*\{\s*onUploaded\s*,\s*disabled\s*,\s*variant\s*,\s*ctaLabel\s*,\s*ctaHint\s*\}\s*\)/);
  });

  it('defaults variant to "compact"', () => {
    expect(SOURCE).toMatch(/variant\s*=\s*['"`]compact['"`]/);
  });

  it('renders a CTA-styled trigger when variant === "cta"', () => {
    expect(SOURCE).toMatch(/variant\s*===\s*['"`]cta['"`]/);
    // The CTA must show ctaLabel and ctaHint text
    expect(SOURCE).toMatch(/\{ctaLabel\}/);
    expect(SOURCE).toMatch(/\{ctaHint\}/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/skillagent/SkillUploadButton.variant.test.jsx`
Expected: FAIL — current signature is `{ onUploaded, disabled }`.

- [ ] **Step 3: Update the component**

In `frontend/src/components/skillagent/SkillUploadButton.jsx`, change the signature and add the CTA rendering:

Replace line 8:
```javascript
export default function SkillUploadButton({ onUploaded, disabled }) {
```
with:
```javascript
export default function SkillUploadButton({ onUploaded, disabled, variant = 'compact', ctaLabel, ctaHint }) {
```

Then in the `return` block, replace the existing `<DropdownMenuTrigger>` (lines 42-46) with a conditional render:

```jsx
        <DropdownMenuTrigger asChild>
          {variant === 'cta' ? (
            <button
              type="button"
              disabled={disabled || uploading}
              className="flex w-full flex-col items-center gap-1.5 rounded-xl border border-dashed border-primary/40 bg-primary/5 px-4 py-6 text-center transition hover:border-primary hover:bg-primary/10 disabled:opacity-50"
            >
              {uploading ? (
                <Loader2 className="h-5 w-5 animate-spin text-primary" />
              ) : (
                <Paperclip className="h-5 w-5 text-primary" />
              )}
              <span className="text-sm font-medium text-foreground">{ctaLabel}</span>
              {ctaHint && <span className="text-[11px] text-muted-foreground">{ctaHint}</span>}
            </button>
          ) : (
            <Button variant="ghost" size="icon" disabled={disabled || uploading} className="h-8 w-8 shrink-0">
              {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
            </Button>
          )}
        </DropdownMenuTrigger>
```

Leave the rest of the component (the hidden `<input>` elements, the `uploadFiles` function, etc.) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/skillagent/SkillUploadButton.variant.test.jsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/skillagent/SkillUploadButton.jsx frontend/src/components/skillagent/SkillUploadButton.variant.test.jsx && git commit -m "feat(ui): add variant=\"cta\" to SkillUploadButton for empty-state drop zone

Larger dashed-border CTA trigger for the SkillAgent empty state. Reuses
the same upload menu (file/folder/zip) and the same onUploaded callback.
Compact variant (default) is unchanged."
```

---

## Task 4: Create `ExistingSkillsChips` component

**Files:**
- Create: `frontend/src/components/skillagent/ExistingSkillsChips.jsx`
- Test: `frontend/src/components/skillagent/ExistingSkillsChips.test.jsx`

**Interfaces:**
- Produces: `export default function ExistingSkillsChips({ onError })`.
  - Fetches `base44.entities.Tool.list` filtered by current owner (anonymous id fallback, mirroring `Toolkit.jsx:56`).
  - Renders the top 8 skills as clickable chips.
  - Clicking a chip calls `window.location.href = '/skill-agent?skill=' + id` (preserved auto-send path).
  - Errors silently no-op (no toast). Optional `onError` callback is called.
  - Returns `null` if no skills are loaded or the fetch fails.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/skillagent/ExistingSkillsChips.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ExistingSkillsChips.jsx'), 'utf8');

describe('ExistingSkillsChips.jsx', () => {
  it('imports useState + useEffect from react', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*useState\s*,\s*useEffect\s*\}\s*from\s*['"`]react['"`]/);
  });

  it('imports base44 from the client', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*base44\s*\}\s*from\s*['"`]@\/api\/base44Client['"`]/);
  });

  it('calls base44.entities.Tool.list with an owner filter', () => {
    expect(SOURCE).toMatch(/base44\.entities\.Tool\.list\s*\(/);
    expect(SOURCE).toMatch(/created_by_id|owner/);
  });

  it('navigates to /skill-agent?skill={id} on chip click', () => {
    expect(SOURCE).toMatch(/\/skill-agent\?skill=/);
  });

  it('limits to top 8 chips', () => {
    expect(SOURCE).toMatch(/\.slice\(0,\s*8\)/);
  });

  it('returns null when there are no skills', () => {
    expect(SOURCE).toMatch(/if\s*\(\s*!skills\.length\s*\)\s*return\s*null/);
  });

  it('has a try/catch that silently no-ops on fetch failure', () => {
    expect(SOURCE).toMatch(/catch\s*\{/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/skillagent/ExistingSkillsChips.test.jsx`
Expected: FAIL — file not found.

- [ ] **Step 3: Create the component**

Create `frontend/src/components/skillagent/ExistingSkillsChips.jsx`:

```jsx
import { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Wrench } from 'lucide-react';

function getOwnerId() {
  try {
    const user = JSON.parse(localStorage.getItem('base44_user') || 'null');
    if (user && user.id) return user.id;
  } catch { /* noop */ }
  const anon = localStorage.getItem('base44_anonymous_id');
  return anon || null;
}

export default function ExistingSkillsChips({ onError }) {
  const { t } = useLanguage();
  const [skills, setSkills] = useState([]);

  useEffect(() => {
    let cancelled = false;
    const owner = getOwnerId();
    const filter = owner ? { created_by_id: owner } : {};
    base44.entities.Tool.list(filter)
      .then((list) => {
        if (cancelled) return;
        setSkills((list || []).slice(0, 8));
      })
      .catch((e) => {
        if (cancelled) return;
        if (typeof onError === 'function') onError(e);
      });
    return () => { cancelled = true; };
  }, [onError]);

  if (!skills.length) return null;

  return (
    <div className="mb-4">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t.skillAgent.existingSkills}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {skills.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => { window.location.href = `/skill-agent?skill=${encodeURIComponent(s.id)}`; }}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 text-[11px] text-foreground transition hover:border-primary/40 hover:bg-secondary/60"
          >
            <Wrench className="h-3 w-3 shrink-0 text-primary" />
            <span className="max-w-[140px] truncate">{s.name}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/skillagent/ExistingSkillsChips.test.jsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/skillagent/ExistingSkillsChips.jsx frontend/src/components/skillagent/ExistingSkillsChips.test.jsx && git commit -m "feat(ui): add ExistingSkillsChips component for SkillAgent empty state

Fetches Tool.list filtered by owner (anonymous id fallback, mirrors
Toolkit.jsx:56 pattern). Renders top 8 as clickable chips that navigate
to /skill-agent?skill={id} (the preserved auto-send edit path)."
```

---

## Task 5: Create `ExistingAgentsChips` component

**Files:**
- Create: `frontend/src/components/agentbuilder/ExistingAgentsChips.jsx`
- Test: `frontend/src/components/agentbuilder/ExistingAgentsChips.test.jsx`

**Interfaces:**
- Produces: `export default function ExistingAgentsChips({ onError })`.
  - Same pattern as `ExistingSkillsChips` but calls `base44.entities.AgentApp.list`.
  - Clicking a chip navigates to `/agent-builder?edit={id}`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/agentbuilder/ExistingAgentsChips.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ExistingAgentsChips.jsx'), 'utf8');

describe('ExistingAgentsChips.jsx', () => {
  it('imports useState + useEffect from react', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*useState\s*,\s*useEffect\s*\}\s*from\s*['"`]react['"`]/);
  });

  it('imports base44 from the client', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*base44\s*\}\s*from\s*['"`]@\/api\/base44Client['"`]/);
  });

  it('calls base44.entities.AgentApp.list with an owner filter', () => {
    expect(SOURCE).toMatch(/base44\.entities\.AgentApp\.list\s*\(/);
    expect(SOURCE).toMatch(/created_by_id|owner/);
  });

  it('navigates to /agent-builder?edit={id} on chip click', () => {
    expect(SOURCE).toMatch(/\/agent-builder\?edit=/);
  });

  it('limits to top 8 chips', () => {
    expect(SOURCE).toMatch(/\.slice\(0,\s*8\)/);
  });

  it('returns null when there are no agents', () => {
    expect(SOURCE).toMatch(/if\s*\(\s*!agents\.length\s*\)\s*return\s*null/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/agentbuilder/ExistingAgentsChips.test.jsx`
Expected: FAIL — file not found.

- [ ] **Step 3: Create the component**

Create `frontend/src/components/agentbuilder/ExistingAgentsChips.jsx`:

```jsx
import { useState, useEffect } from 'react';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { Bot } from 'lucide-react';

function getOwnerId() {
  try {
    const user = JSON.parse(localStorage.getItem('base44_user') || 'null');
    if (user && user.id) return user.id;
  } catch { /* noop */ }
  const anon = localStorage.getItem('base44_anonymous_id');
  return anon || null;
}

export default function ExistingAgentsChips({ onError }) {
  const { t } = useLanguage();
  const [agents, setAgents] = useState([]);

  useEffect(() => {
    let cancelled = false;
    const owner = getOwnerId();
    const filter = owner ? { created_by_id: owner } : {};
    base44.entities.AgentApp.list(filter)
      .then((list) => {
        if (cancelled) return;
        setAgents((list || []).slice(0, 8));
      })
      .catch((e) => {
        if (cancelled) return;
        if (typeof onError === 'function') onError(e);
      });
    return () => { cancelled = true; };
  }, [onError]);

  if (!agents.length) return null;

  return (
    <div className="mb-4">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {t.agentBuilder.existingAgents}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {agents.map((a) => (
          <button
            key={a.id}
            type="button"
            onClick={() => { window.location.href = `/agent-builder?edit=${encodeURIComponent(a.id)}`; }}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1 text-[11px] text-foreground transition hover:border-primary/40 hover:bg-secondary/60"
          >
            <Bot className="h-3 w-3 shrink-0 text-primary" />
            <span className="max-w-[140px] truncate">{a.name}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/components/agentbuilder/ExistingAgentsChips.test.jsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/agentbuilder/ExistingAgentsChips.jsx frontend/src/components/agentbuilder/ExistingAgentsChips.test.jsx && git commit -m "feat(ui): add ExistingAgentsChips component for AgentBuilder empty state

Mirrors ExistingSkillsChips: fetches AgentApp.list filtered by owner,
renders top 8 as clickable chips that navigate to /agent-builder?edit={id}
(the preserved auto-send edit path)."
```

---

## Task 6: Update `useAgentBuilder.js` — add `prefilledHint` + `applySuggestion`

**Files:**
- Modify: `frontend/src/hooks/useAgentBuilder.js`
- Test: `frontend/src/hooks/useAgentBuilder.applySuggestion.test.jsx`

**Interfaces:**
- Produces: hook now returns `prefilledHint: boolean`, `setPrefilledHint: (v: boolean) => void`, `applySuggestion: (text: string) => void`.
- `applySuggestion(text)` does: `setInput(text); setPrefilledHint(true); inputRef.current?.focus();`. It does NOT call `startNewChat` or `handleSend`.
- `startNewChat` and `handleSend` are unchanged (still used by intentional paths).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/hooks/useAgentBuilder.applySuggestion.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './useAgentBuilder.js'), 'utf8');

describe('useAgentBuilder.js — applySuggestion helper', () => {
  it('declares prefilledHint state', () => {
    expect(SOURCE).toMatch(/const\s*\[\s*prefilledHint\s*,\s*setPrefilledHint\s*\]\s*=\s*useState\(false\)/);
  });

  it('defines applySuggestion as a useCallback', () => {
    expect(SOURCE).toMatch(/const\s+applySuggestion\s*=\s*useCallback\s*\(\s*\(text\)\s*=>\s*\{/);
  });

  it('applySuggestion sets input, prefilledHint=true, and focuses inputRef', () => {
    const fn = SOURCE.match(/const\s+applySuggestion\s*=\s*useCallback\s*\(\s*\(text\)\s*=>\s*\{([\s\S]*?)\},\s*\[\]\s*\)/);
    expect(fn, 'applySuggestion body not found').not.toBeNull();
    expect(fn[1]).toMatch(/setInput\(text\)/);
    expect(fn[1]).toMatch(/setPrefilledHint\(true\)/);
    expect(fn[1]).toMatch(/inputRef\.current\?\.focus\(\)/);
  });

  it('applySuggestion does NOT call startNewChat or handleSend', () => {
    const fn = SOURCE.match(/const\s+applySuggestion\s*=\s*useCallback\s*\(\s*\(text\)\s*=>\s*\{([\s\S]*?)\},\s*\[\]\s*\)/);
    expect(fn, 'applySuggestion body not found').not.toBeNull();
    expect(fn[1]).not.toMatch(/startNewChat\(/);
    expect(fn[1]).not.toMatch(/handleSend\(/);
  });

  it('exposes prefilledHint, setPrefilledHint, applySuggestion in the return object', () => {
    expect(SOURCE).toMatch(/prefilledHint,/);
    expect(SOURCE).toMatch(/setPrefilledHint,/);
    expect(SOURCE).toMatch(/applySuggestion,/);
  });

  it('startNewChat still exists and still auto-sends (intentional path preserved)', () => {
    expect(SOURCE).toMatch(/const\s+startNewChat\s*=\s*useCallback/);
    expect(SOURCE).toMatch(/setTimeout\(\(\)\s*=>\s*handleSend\(promptText,\s*conv\),\s*100\)/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/hooks/useAgentBuilder.applySuggestion.test.jsx`
Expected: FAIL — `prefilledHint` and `applySuggestion` not in source.

- [ ] **Step 3: Add the state and helper to the hook**

In `frontend/src/hooks/useAgentBuilder.js`:

After the existing `const [openArtifact, setOpenArtifact] = useState(null);` line (line 67), add:

```javascript
  const [prefilledHint, setPrefilledHint] = useState(false);
```

After the `clearCreatedAgent` callback (around line 472, before `setCreatedAgentValue`), add:

```javascript
  const applySuggestion = useCallback((text) => {
    setInput(text);
    setPrefilledHint(true);
    inputRef.current?.focus();
  }, []);
```

Then in the return object (line 479-515), add the three new exports. Find the existing `setOpenArtifact,` line and add after it:

```javascript
    prefilledHint,
    setPrefilledHint,
    applySuggestion,
```

Also, to ensure the hint clears on intentional send, find the `setInput('');` line inside `handleSend` (line 250) and add immediately after it:

```javascript
    setPrefilledHint(false);
```

(Do NOT modify `startNewChat` — it doesn't currently clear input either, and the hint is cosmetic.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/hooks/useAgentBuilder.applySuggestion.test.jsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/hooks/useAgentBuilder.js frontend/src/hooks/useAgentBuilder.applySuggestion.test.jsx && git commit -m "feat(hook): add prefilledHint state + applySuggestion to useAgentBuilder

applySuggestion(text) sets input, marks the prefilled hint active, and
focuses the input — without calling startNewChat or handleSend. The
hint is cleared on handleSend. startNewChat (intentional Enter-on-empty
and ?edit= paths) is unchanged."
```

---

## Task 7: Update `SkillAgent.jsx` — replace auto-fire sites + render pill + redesign empty state

**Files:**
- Modify: `frontend/src/pages/SkillAgent.jsx`
- Test: `frontend/src/pages/SkillAgent.applySuggestion.test.jsx`

**Interfaces:**
- Consumes: `PrefilledHintPill` (Task 2), `SkillUploadButton` with `variant="cta"` (Task 3), `ExistingSkillsChips` (Task 4), `t.skillAgent.prefilledHint` / `t.skillAgent.or` / `t.skillAgent.emptyUploadCta` / `t.skillAgent.emptyUploadHint` (Task 1).
- Produces: a `SkillAgent` page where `?action=create` and the 4 suggestion cards call `applySuggestion`, not `startNewChat`. The empty state shows the upload CTA + existing-skills chips.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/pages/SkillAgent.applySuggestion.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './SkillAgent.jsx'), 'utf8');

describe('SkillAgent.jsx — wait-for-input contract', () => {
  it('declares prefilledHint state', () => {
    expect(SOURCE).toMatch(/const\s*\[\s*prefilledHint\s*,\s*setPrefilledHint\s*\]\s*=\s*useState\(false\)/);
  });

  it('defines applySuggestion helper', () => {
    expect(SOURCE).toMatch(/function\s+applySuggestion\s*\(\s*text\s*\)\s*\{/);
    const body = SOURCE.match(/function\s+applySuggestion\s*\(\s*text\s*\)\s*\{([\s\S]*?)\n\s*\}/);
    expect(body, 'applySuggestion body not found').not.toBeNull();
    expect(body[1]).toMatch(/setInput\(text\)/);
    expect(body[1]).toMatch(/setPrefilledHint\(true\)/);
    expect(body[1]).toMatch(/inputRef\.current\?\.focus\(\)/);
  });

  it('?action=create calls applySuggestion, NOT startNewChat', () => {
    // Find the action === 'create' branch
    const branch = SOURCE.match(/action\s*===\s*['"`]create['"`]\s*\)\s*\{([^}]+)\}/);
    expect(branch, 'action=create branch not found').not.toBeNull();
    expect(branch[1]).toMatch(/applySuggestion\s*\(\s*t\.skillAgent\.suggestions\.create\s*\)/);
    expect(branch[1]).not.toMatch(/startNewChat/);
  });

  it('suggestion card onClick calls applySuggestion, NOT startNewChat', () => {
    const card = SOURCE.match(/onClick=\{\(\)\s*=>\s*([^\n]+)\}.*hover:bg-secondary\/60/);
    expect(card, 'suggestion card onClick not found').not.toBeNull();
    expect(card[1]).toMatch(/applySuggestion/);
    expect(card[1]).not.toMatch(/startNewChat/);
    // Must NOT have the old deferSend special-case
    expect(SOURCE).not.toMatch(/deferSend:\s*s\.key\s*===\s*['"`]learn['"`]/);
  });

  it('intentional paths are preserved (startWithSkill, startWithFiles, startNewChat-on-Enter)', () => {
    expect(SOURCE).toMatch(/startWithSkill\(skillId\)/);
    expect(SOURCE).toMatch(/startWithFiles\(urls\)/);
    expect(SOURCE).toMatch(/showEmpty\s*\?\s*startNewChat\(input\)\s*:\s*handleSend\(input\)/);
  });

  it('renders PrefilledHintPill in renderInputArea', () => {
    expect(SOURCE).toMatch(/import\s+PrefilledHintPill\s+from\s+['"`]@\/components\/common\/PrefilledHintPill['"`]/);
    expect(SOURCE).toMatch(/<PrefilledHintPill\s+label=\{t\.skillAgent\.prefilledHint\}\s+onDismiss=\{\(\)\s*=>\s*setPrefilledHint\(false\)\}\s*\/>/);
  });

  it('clears prefilledHint on input change', () => {
    expect(SOURCE).toMatch(/onChange=\{\(e\)\s*=>\s*\{\s*setInput\(e\.target\.value\);\s*if\s*\(prefilledHint\)\s*setPrefilledHint\(false\);\s*\}\}/);
  });

  it('empty state renders the upload CTA with variant="cta"', () => {
    expect(SOURCE).toMatch(/<SkillUploadButton[^>]*variant="cta"[^>]*>/);
    expect(SOURCE).toMatch(/ctaLabel=\{t\.skillAgent\.emptyUploadCta\}/);
    expect(SOURCE).toMatch(/ctaHint=\{t\.skillAgent\.emptyUploadHint\}/);
  });

  it('empty state renders ExistingSkillsChips', () => {
    expect(SOURCE).toMatch(/import\s+ExistingSkillsChips\s+from\s+['"`]@\/components\/skillagent\/ExistingSkillsChips['"`]/);
    expect(SOURCE).toMatch(/<ExistingSkillsChips\s*\/>/);
  });

  it('empty state renders the "or" divider', () => {
    expect(SOURCE).toMatch(/t\.skillAgent\.or/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/SkillAgent.applySuggestion.test.jsx`
Expected: FAIL — `applySuggestion`, `prefilledHint`, etc. not in source.

- [ ] **Step 3: Add imports**

At the top of `frontend/src/pages/SkillAgent.jsx`, add to the existing imports (after the `SkillUploadButton` import line 6):

```javascript
import PrefilledHintPill from '@/components/common/PrefilledHintPill';
import ExistingSkillsChips from '@/components/skillagent/ExistingSkillsChips';
```

- [ ] **Step 4: Add `prefilledHint` state and `applySuggestion` helper**

After the existing `const [openArtifact, setOpenArtifact] = useState(null);` line (line 34), add:

```javascript
  const [prefilledHint, setPrefilledHint] = useState(false);
```

After the existing `startNewChat` function (ends at line 139), add:

```javascript
  function applySuggestion(text) {
    setInput(text);
    setPrefilledHint(true);
    inputRef.current?.focus();
  }
```

Also add `setPrefilledHint(false);` inside `startNewChat` after `setInput(prefillText);` (line 135) and inside `handleSend` after `setInput('');` (line 189).

- [ ] **Step 5: Replace the `?action=create` auto-fire site**

Find lines 52-54:
```javascript
    } else if (action === 'create') {
      startNewChat(t.skillAgent.suggestions.create);
    }
```
Replace with:
```javascript
    } else if (action === 'create') {
      applySuggestion(t.skillAgent.suggestions.create);
    }
```

- [ ] **Step 6: Replace the suggestion card `onClick`**

Find line 358:
```javascript
                    <button key={s.key} onClick={() => startNewChat(t.skillAgent.suggestions[s.key], { deferSend: s.key === 'learn' })} className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-3 text-left text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-secondary/60">
```
Replace with:
```javascript
                    <button key={s.key} onClick={() => applySuggestion(t.skillAgent.suggestions[s.key])} className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-3 text-left text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-secondary/60">
```

- [ ] **Step 7: Add the `PrefilledHintPill` to `renderInputArea` and clear-on-change hook**

In `renderInputArea` (lines 311-340), add the pill above the textarea and update the `onChange`:

Replace the `<textarea ... />` block (lines 323-332) with:
```jsx
      {prefilledHint && (
        <PrefilledHintPill label={t.skillAgent.prefilledHint} onDismiss={() => setPrefilledHint(false)} />
      )}
      <textarea
        ref={inputRef}
        value={input}
        onChange={(e) => { setInput(e.target.value); if (prefilledHint) setPrefilledHint(false); }}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); showEmpty ? startNewChat(input) : handleSend(input); } }}
        placeholder={t.skillAgent.placeholder}
        rows={2}
        disabled={loading}
        className="w-full resize-none bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
      />
```

- [ ] **Step 8: Redesign the empty state with upload CTA + chips**

Replace the empty-state `<div className="mt-8 w-full max-w-2xl">` block (lines 355-365) with:

```jsx
              <div className="mt-8 w-full max-w-2xl">
                <div className="mb-4 flex flex-col gap-2">
                  {SUGGESTIONS.map((s) => (
                    <button key={s.key} onClick={() => applySuggestion(t.skillAgent.suggestions[s.key])} className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-3 text-left text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-secondary/60">
                      <s.icon className="h-4 w-4 shrink-0 text-primary" />
                      <span>{t.skillAgent.suggestions[s.key]}</span>
                    </button>
                  ))}
                </div>

                <div className="mb-4 flex items-center gap-3">
                  <div className="h-px flex-1 bg-border" />
                  <span className="text-[11px] text-muted-foreground">{t.skillAgent.or}</span>
                  <div className="h-px flex-1 bg-border" />
                </div>

                <div className="mb-4">
                  <SkillUploadButton
                    onUploaded={handleFilesUploaded}
                    disabled={loading}
                    variant="cta"
                    ctaLabel={t.skillAgent.emptyUploadCta}
                    ctaHint={t.skillAgent.emptyUploadHint}
                  />
                </div>

                <ExistingSkillsChips />

                {renderInputArea(true)}
              </div>
```

- [ ] **Step 9: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/SkillAgent.applySuggestion.test.jsx`
Expected: PASS — all 9 assertions green.

- [ ] **Step 10: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/SkillAgent.jsx frontend/src/pages/SkillAgent.applySuggestion.test.jsx && git commit -m "fix(skill-agent): replace auto-fire suggestion cards with applySuggestion

?action=create and the 4 suggestion cards now pre-fill the input and
focus it (applySuggestion) instead of auto-creating a conversation and
sending the suggestion text to the backend. The user edits + presses
Enter to actually start the chat.

Empty state redesigned: divider + SkillUploadButton variant=\"cta\" +
ExistingSkillsChips row, so users can pick up an existing skill or
upload a .skill/.zip file instead of typing a prompt.

Intentional auto-send paths (?skill=, ?files=, Enter-on-empty) are
preserved — only the generic suggestion/auto-create paths change."
```

---

## Task 8: Update `AgentBuilder.jsx` — replace auto-fire sites + render pill + redesign empty state

**Files:**
- Modify: `frontend/src/pages/AgentBuilder.jsx`
- Test: `frontend/src/pages/AgentBuilder.applySuggestion.test.jsx`

**Interfaces:**
- Consumes: `PrefilledHintPill` (Task 2), `ExistingAgentsChips` (Task 5), `builder.prefilledHint` / `builder.setPrefilledHint` / `builder.applySuggestion` (Task 6), `t.agentBuilder.prefilledHint` / `t.agentBuilder.or` (Task 1).
- Produces: an `AgentBuilder` page where `?prefill=...` and the 3 suggestion cards call `applySuggestion`, not `startNewChat`. The empty state shows existing-agents chips.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/pages/AgentBuilder.applySuggestion.test.jsx`:

```jsx
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './AgentBuilder.jsx'), 'utf8');

describe('AgentBuilder.jsx — wait-for-input contract', () => {
  it('imports PrefilledHintPill', () => {
    expect(SOURCE).toMatch(/import\s+PrefilledHintPill\s+from\s+['"`]@\/components\/common\/PrefilledHintPill['"`]/);
  });

  it('imports ExistingAgentsChips', () => {
    expect(SOURCE).toMatch(/import\s+ExistingAgentsChips\s+from\s+['"`]@\/components\/agentbuilder\/ExistingAgentsChips['"`]/);
  });

  it('destructures prefilledHint, setPrefilledHint, applySuggestion from builder', () => {
    expect(SOURCE).toMatch(/prefilledHint,/);
    expect(SOURCE).toMatch(/setPrefilledHint,/);
    expect(SOURCE).toMatch(/applySuggestion,/);
  });

  it('?prefill= calls applySuggestion, NOT startNewChat', () => {
    const branch = SOURCE.match(/if\s*\(prefill\)\s*([^\n]+)/);
    expect(branch, '?prefill branch not found').not.toBeNull();
    expect(branch[1]).toMatch(/applySuggestion\s*\(\s*prefill\s*\)/);
    expect(branch[1]).not.toMatch(/startNewChat/);
  });

  it('suggestion card onClick calls applySuggestion, NOT startNewChat', () => {
    const card = SOURCE.match(/onClick=\{\(\)\s*=>\s*([^\n]+)\}.*hover:bg-secondary\/60/);
    expect(card, 'suggestion card onClick not found').not.toBeNull();
    expect(card[1]).toMatch(/applySuggestion/);
    expect(card[1]).not.toMatch(/startNewChat/);
  });

  it('intentional paths are preserved (?edit=, startWithEdit, Enter-on-empty)', () => {
    expect(SOURCE).toMatch(/else\s+if\s*\(editId\)\s+startWithEdit\(editId\)/);
    expect(SOURCE).toMatch(/showEmpty\s*\?\s*startNewChat\(input\)\s*:\s*handleSend\(input\)/);
  });

  it('renders PrefilledHintPill in renderInputArea', () => {
    expect(SOURCE).toMatch(/<PrefilledHintPill\s+label=\{t\.agentBuilder\.prefilledHint\}\s+onDismiss=\{\(\)\s*=>\s*setPrefilledHint\(false\)\}\s*\/>/);
  });

  it('clears prefilledHint on input change', () => {
    expect(SOURCE).toMatch(/onChange=\{\(e\)\s*=>\s*\{\s*setInput\(e\.target\.value\);\s*if\s*\(prefilledHint\)\s*setPrefilledHint\(false\);\s*\}\}/);
  });

  it('empty state renders ExistingAgentsChips', () => {
    expect(SOURCE).toMatch(/<ExistingAgentsChips\s*\/>/);
  });

  it('empty state renders the "or" divider', () => {
    expect(SOURCE).toMatch(/t\.agentBuilder\.or/);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/AgentBuilder.applySuggestion.test.jsx`
Expected: FAIL.

- [ ] **Step 3: Add imports**

In `frontend/src/pages/AgentBuilder.jsx`, after line 14 (the `lucide-react` import), add:

```javascript
import PrefilledHintPill from '@/components/common/PrefilledHintPill';
import ExistingAgentsChips from '@/components/agentbuilder/ExistingAgentsChips';
```

- [ ] **Step 4: Destructure new fields from `builder`**

In the `const { ... } = builder;` block (lines 27-57), add after `setLoading,` (around line 49):

```javascript
    prefilledHint,
    setPrefilledHint,
    applySuggestion,
```

- [ ] **Step 5: Replace the `?prefill=` auto-fire site**

Find lines 59-66 (the `useEffect`). Replace line 63:
```javascript
    if (prefill) startNewChat(prefill);
```
with:
```javascript
    if (prefill) applySuggestion(prefill);
```

- [ ] **Step 6: Replace the suggestion card `onClick`**

Find line 112:
```javascript
                  <button key={s.key} onClick={() => startNewChat(t.agentBuilder.suggestions[s.key])} className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-3 text-left text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-secondary/60">
```
Replace with:
```javascript
                  <button key={s.key} onClick={() => applySuggestion(t.agentBuilder.suggestions[s.key])} className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-3 text-left text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-secondary/60">
```

- [ ] **Step 7: Add the `PrefilledHintPill` to `renderInputArea` and clear-on-change hook**

In `renderInputArea` (lines 70-89), replace the `<textarea ... />` block (lines 72-81) with:

```jsx
      {prefilledHint && (
        <PrefilledHintPill label={t.agentBuilder.prefilledHint} onDismiss={() => setPrefilledHint(false)} />
      )}
      <textarea
        ref={inputRef}
        value={input}
        onChange={(e) => { setInput(e.target.value); if (prefilledHint) setPrefilledHint(false); }}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); showEmpty ? startNewChat(input) : handleSend(input); } }}
        placeholder={t.agentBuilder.placeholder}
        rows={2}
        disabled={loading || initializing}
        className="w-full resize-none bg-transparent px-4 py-3 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
      />
```

- [ ] **Step 8: Redesign the empty state with chips + divider**

Replace the empty-state `<div className="mt-8 w-full max-w-2xl">` block (lines 109-119) with:

```jsx
            <div className="mt-8 w-full max-w-2xl">
              <div className="mb-4 flex flex-col gap-2">
                {SUGGESTIONS.map((s) => (
                  <button key={s.key} onClick={() => applySuggestion(t.agentBuilder.suggestions[s.key])} className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-3 text-left text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-secondary/60">
                    <s.icon className="h-4 w-4 shrink-0 text-primary" />
                    <span>{t.agentBuilder.suggestions[s.key]}</span>
                  </button>
                ))}
              </div>

              <div className="mb-4 flex items-center gap-3">
                <div className="h-px flex-1 bg-border" />
                <span className="text-[11px] text-muted-foreground">{t.agentBuilder.or}</span>
                <div className="h-px flex-1 bg-border" />
              </div>

              <ExistingAgentsChips />

              {renderInputArea()}
            </div>
```

- [ ] **Step 9: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/AgentBuilder.applySuggestion.test.jsx`
Expected: PASS — all 10 assertions green.

- [ ] **Step 10: Commit**

```bash
cd /root/zhanlu && git add frontend/src/pages/AgentBuilder.jsx frontend/src/pages/AgentBuilder.applySuggestion.test.jsx && git commit -m "fix(agent-builder): replace auto-fire suggestion cards with applySuggestion

?prefill= and the 3 suggestion cards now pre-fill the input and focus it
(applySuggestion) instead of auto-creating a conversation. The user edits
+ presses Enter to actually start the chat.

Empty state redesigned: divider + ExistingAgentsChips row so users can
pick up an existing agent for editing.

Intentional auto-send paths (?edit=, Enter-on-empty) are preserved."
```

---

## Task 9: Final verification — full build + memory note

**Files:**
- No new files. Runs verification commands and appends to `/root/.codebuddy/memory/2026-07-28.md`.

- [ ] **Step 1: Run all new tests together**

Run: `cd /root/zhanlu/frontend && npx vitest run src/lib/translations.skillAgentKeys.test.jsx src/components/common/PrefilledHintPill.test.jsx src/components/skillagent/SkillUploadButton.variant.test.jsx src/components/skillagent/ExistingSkillsChips.test.jsx src/components/agentbuilder/ExistingAgentsChips.test.jsx src/hooks/useAgentBuilder.applySuggestion.test.jsx src/pages/SkillAgent.applySuggestion.test.jsx src/pages/AgentBuilder.applySuggestion.test.jsx`
Expected: ALL PASS.

- [ ] **Step 2: Run a quick check that no existing tests break**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/ChatInput.ungroupedChip.test.jsx src/components/agentbuilder/AgentToolsPanel.test.js`
Expected: PASS (these are the nearest neighbors; full suite is OOM-risky per project memory).

- [ ] **Step 3: Run the production build**

Check `free -h` first (per project memory, vite build needs >2 GB free). If RAM is sufficient, run:

```bash
cd /root/zhanlu/frontend && ./node_modules/.bin/vite build
```

Expected: build succeeds, new bundle hash in `dist/index.html`.

- [ ] **Step 4: Verify the new strings are present in the built bundle**

```bash
cd /root/zhanlu && grep -c 'prefilledHint\|Suggestion applied\|已填入建议\|Your skills\|你的技能\|Your agents\|你的智能体' frontend/dist/assets/*.js
```

Expected: count > 0 for each pattern.

- [ ] **Step 5: Verify old auto-fire pattern is gone from the bundle**

```bash
cd /root/zhanlu && grep -o 'deferSend:\s*s\.key\s*===\s*["\x27]learn["\x27]' frontend/dist/assets/*.js | head
```

Expected: zero matches (the old `deferSend` special-case has been removed).

- [ ] **Step 6: Append a memory note**

Read `/root/.codebuddy/memory/2026-07-28.md` first, then append (using `replace_in_file`, NOT `write_to_file`):

```markdown

## Skill Agent / Agent Builder — wait-for-input guided flow (spec + plan + execute)

**Bug**: Skill Agent auto-built skills without waiting for user input. `?action=create` and the 4 suggestion cards called `startNewChat(...)` which auto-fired `handleSend` after 100ms.

**Spec**: `docs/superpowers/specs/2026-07-28-skill-agent-wait-for-input-design.md`
**Plan**: `docs/superpowers/plans/2026-07-28-skill-agent-wait-for-input.md`

**Fix** (frontend-only):
- New `applySuggestion(text)` helper in both `SkillAgent.jsx` and `useAgentBuilder.js`: `setInput(text); setPrefilledHint(true); inputRef.current?.focus();` — no backend call.
- Replaces 4 auto-fire call sites: `?action=create` (SkillAgent:52), suggestion onClick (SkillAgent:358), `?prefill=` (AgentBuilder:63), suggestion onClick (AgentBuilder:112).
- Intentional auto-send paths preserved: `?skill=`, `?files=`, `?edit=`, Enter-on-empty-state.
- New `PrefilledHintPill` shared component + `variant="cta"` on `SkillUploadButton` + `ExistingSkillsChips` / `ExistingAgentsChips` chip rows.
- 8 new i18n keys in zh + en.
- Tests use the source-string scan pattern (precedent: `ChatInput.ungroupedChip.test.jsx`).
- Bundle must be rebuilt (`vite build`) for changes to take effect in browser.
```

- [ ] **Step 7: Final commit**

```bash
cd /root/zhanlu && git add docs/superpowers/specs/2026-07-28-skill-agent-wait-for-input-design.md docs/superpowers/plans/2026-07-28-skill-agent-wait-for-input.md && git commit -m "docs(specs+plans): skill agent wait-for-input design + implementation plan

Spec: 9 sections covering root cause (4 auto-fire call sites), approach
(applySuggestion helper), component changes, behavior matrix, edge cases,
testing strategy, out-of-scope, rollout, and resolved open questions.

Plan: 9 bite-sized TDD tasks using the source-string scan test pattern.
Each task: failing test → minimal impl → passing test → commit. Final
task runs full vite build + string-presence verification + memory note."
```

- [ ] **Step 8: Hard-refresh instruction**

Tell the user: the changes are not visible in the browser until they hard-refresh (Cmd/Ctrl+Shift+R) to pick up the new bundle hash. Verify the four entry points behave per the spec's §4 behavior matrix:
- `/skill-agent?action=create` → input pre-filled, no chat started.
- Click each SkillAgent suggestion card → input pre-filled, no chat started.
- `/skill-agent?skill={id}` → chat auto-starts with edit prompt (preserved).
- `/agent-builder?prefill=...` → input pre-filled, no chat started.
- Click each AgentBuilder suggestion card → input pre-filled, no chat started.
- `/agent-builder?edit={id}` → chat auto-starts with edit prompt (preserved).

---

## Self-Review

**1. Spec coverage:**
- §1 Root cause (4 call sites) → Tasks 7 + 8 replace each.
- §2 Approach (applySuggestion helper) → Tasks 6 + 7 + 8.
- §3.1 SkillAgent.jsx changes → Task 7 (all 5 sub-edits).
- §3.2 useAgentBuilder.js → Task 6.
- §3.3 AgentBuilder.jsx → Task 8 (all 4 sub-edits).
- §3.4 translations.js → Task 1.
- §3.5 SkillUploadButton variant → Task 3.
- §4 Behavior matrix → covered by Task 7 + 8 test assertions (intentional paths preserved).
- §5 Edge cases → covered by source-scan tests (e.g. `applySuggestion` doesn't call startNewChat).
- §6 Testing strategy → source-scan tests + final vite build.
- §7 Out of scope (main /agent chat) → not touched.
- §8 Rollout → Task 9 final commit + memory note.
- §9 Open questions → resolved with defaults in spec; plan proceeds on those defaults.

**2. Placeholder scan:** No "TBD", "implement later", "similar to Task N" patterns. Every step has actual code.

**3. Type consistency:**
- `applySuggestion(text)` signature is consistent across Task 6 (hook), Task 7 (SkillAgent local helper), Task 8 (consumes from hook).
- `prefilledHint` / `setPrefilledHint` named identically everywhere.
- `PrefilledHintPill` props `({ label, onDismiss })` consistent in Task 2 definition and Tasks 7/8 usage.
- `ExistingSkillsChips` / `ExistingAgentsChips` props `({ onError })` consistent in Tasks 4/5 definitions and Tasks 7/8 usage (no `onError` passed → optional, defaults to no-op).
- `SkillUploadButton` new props `(variant, ctaLabel, ctaHint)` consistent in Task 3 definition and Task 7 usage.

No issues found.
