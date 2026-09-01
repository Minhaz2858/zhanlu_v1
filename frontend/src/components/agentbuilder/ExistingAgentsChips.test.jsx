/**
 * Regression: ExistingAgentsChips must fetch AgentApp.list filtered by owner
 * and render top 8 as clickable chips navigating to /agent-builder?edit={id}.
 * Mirrors the ExistingSkillsChips pattern. See spec
 * 2026-07-28-skill-agent-wait-for-input-design.md §3.3.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ExistingAgentsChips.jsx'), 'utf8');

describe('ExistingAgentsChips.jsx', () => {
  it('imports useState + useEffect from react', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*useState\s*,\s*useEffect\s*\}\s*from\s*['"]react['"]/);
  });

  it('imports base44 from the client', () => {
    expect(SOURCE).toMatch(/import\s*\{\s*base44\s*\}\s*from\s*['"]@\/api\/base44Client['"]/);
  });

  it('calls base44.entities.AgentApp.list with an owner filter', () => {
    expect(SOURCE).toMatch(/base44\.entities\.AgentApp\.list\s*\(/);
    expect(SOURCE).toMatch(/created_by_id/);
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

  it('has a catch handler for fetch failure', () => {
    expect(SOURCE).toMatch(/\.catch\s*\(/);
  });
});
