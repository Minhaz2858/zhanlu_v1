/**
 * Regression: SkillAgent.jsx must use applySuggestion (pre-fill + focus)
 * instead of startNewChat (auto-create + auto-send) for the ?action=create
 * URL param and the 4 suggestion cards. Intentional auto-send paths
 * (?skill=, ?files=, Enter-on-empty) are preserved. See spec
 * 2026-07-28-skill-agent-wait-for-input-design.md §3.1 + §4.
 */

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
    const branch = SOURCE.match(/action\s*===\s*['"]create['"]\s*\)\s*\{([^}]+)\}/);
    expect(branch, 'action=create branch not found').not.toBeNull();
    expect(branch[1]).toMatch(/applySuggestion\s*\(\s*t\.skillAgent\.suggestions\.create\s*\)/);
    expect(branch[1]).not.toMatch(/startNewChat/);
  });

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

  it('intentional paths are preserved (startWithSkill, startWithFiles, startNewChat-on-Enter)', () => {
    expect(SOURCE).toMatch(/startWithSkill\(skillId\)/);
    expect(SOURCE).toMatch(/startWithFiles\(urls\)/);
    expect(SOURCE).toMatch(/showEmpty\s*\?\s*startNewChat\(input\)\s*:\s*handleSend\(input\)/);
  });

  it('renders PrefilledHintPill in renderInputArea', () => {
    expect(SOURCE).toMatch(/import\s+PrefilledHintPill\s+from\s+['"]@\/components\/common\/PrefilledHintPill['"]/);
    expect(SOURCE).toMatch(/<PrefilledHintPill\s+label=\{t\.skillAgent\.prefilledHint\}\s+onDismiss=\{\(\)\s*=>\s*setPrefilledHint\(false\)\}\s*\/>/);
  });

  it('clears prefilledHint on input change', () => {
    expect(SOURCE).toMatch(/onChange=\{\(e\)\s*=>\s*\{\s*setInput\(e\.target\.value\);\s*if\s*\(prefilledHint\)\s*setPrefilledHint\(false\);\s*\}\}/);
  });
});
