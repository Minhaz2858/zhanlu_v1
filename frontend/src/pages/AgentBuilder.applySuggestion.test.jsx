/**
 * Regression: AgentBuilder.jsx must use applySuggestion (pre-fill + focus)
 * instead of startNewChat (auto-create + auto-send) for the ?prefill= URL
 * param and the 3 suggestion cards. Intentional auto-send paths (?edit=,
 * Enter-on-empty) are preserved. See spec
 * 2026-07-28-skill-agent-wait-for-input-design.md §3.3 + §4.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './AgentBuilder.jsx'), 'utf8');

describe('AgentBuilder.jsx — wait-for-input contract', () => {
  it('imports PrefilledHintPill', () => {
    expect(SOURCE).toMatch(/import\s+PrefilledHintPill\s+from\s+['"]@\/components\/common\/PrefilledHintPill['"]/);
  });

  it('imports ExistingAgentsChips', () => {
    expect(SOURCE).toMatch(/import\s+ExistingAgentsChips\s+from\s+['"]@\/components\/agentbuilder\/ExistingAgentsChips['"]/);
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
