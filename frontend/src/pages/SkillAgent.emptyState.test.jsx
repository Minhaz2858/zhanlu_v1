/**
 * Regression: SkillAgent.jsx empty state is chat-style (input first,
 * then 4 short quick-start chips). Rev 2 (2026-07-28) removed the
 * "or edit an existing skill" divider + ExistingSkillsChips that
 * rev 1 added; this test pins both the new structure and the
 * absence of the divider. See spec
 * 2026-07-28-skill-agent-empty-state-design.md §3.1 and §3.4.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './SkillAgent.jsx'), 'utf8');

describe('SkillAgent.jsx — chat-style empty state', () => {
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

  it('does not show the "Or edit an existing skill" divider (removed in revision 2)', () => {
    // The divider + ExistingSkillsChips was removed after the first build because
    // it surfaced duplicate-skill rows and added visual noise. See spec §3 (revision 2).
    expect(SOURCE).not.toMatch(/t\.skillAgent\.orEdit/);
    expect(SOURCE).not.toMatch(/<ExistingSkillsChips\b/);
  });

  it('shows the new subtitle next to the title', () => {
    expect(SOURCE).toMatch(/t\.skillAgent\.subtitle/);
  });
});
