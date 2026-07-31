/**
 * Regression: ExistingSkillsChips must fetch Tool.list filtered by owner
 * and render top 8 as clickable chips navigating to /skill-agent?skill={id}.
 * Mirrors the owner-id filter pattern from Toolkit.jsx:56. See spec
 * 2026-07-28-skill-agent-wait-for-input-design.md §3.1.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ExistingSkillsChips.jsx'), 'utf8');

describe('ExistingSkillsChips.jsx', () => {
  it('imports useState + useEffect from react', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*useState\s*,\s*useEffect\s*\}\s*from\s*['"]react['"]/);
  });

  it('imports base44 from the client', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*base44\s*\}\s*from\s*['"]@\/api\/base44Client['"]/);
  });

  it('calls base44.entities.Tool.list with an owner filter', () => {
    expect(SOURCE).toMatch(/base44\.entities\.Tool\.list\s*\(/);
    expect(SOURCE).toMatch(/created_by_id/);
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
    expect(SOURCE).toMatch(/\.catch\s*\(/);
  });
});
