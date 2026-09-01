/**
 * Regression: PrefilledHintPill must be a reusable, stateless component
 * that renders a label and a dismiss button. Used by SkillAgent and
 * AgentBuilder to indicate that a suggestion has been pre-filled into
 * the input and the user can edit + Enter to send.
 */

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
    expect(SOURCE).toMatch(/onClick=\{\(\)\s*=>\s*onDismiss\s*&&\s*onDismiss\(\)\}/);
  });

  it('returns null when label is falsy', () => {
    expect(SOURCE).toMatch(/if\s*\(\s*!label\s*\)\s*return\s*null/);
  });
});
