/**
 * Regression: the Project Detail "Shared Memory" panel must render
 * the project-scoped AgentMemory entries with a usage bar, an inline
 * add form, and per-entry pin / edit / delete actions wired to the
 * project_memories router handlers (onAdd / onUpdate / onDelete).
 *
 * Why this exists
 * ---------------
 * The memory review/edit feature (backend/app/routers/project_memories.py)
 * is backend-complete; this pins the frontend surface that was added
 * on top of it. Without these assertions a refactor could silently
 * drop the pin toggle, the edit form, or the hard-delete path.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ProjectDetail.jsx'), 'utf8');

describe('ProjectDetail.jsx Shared Memory panel', () => {
  it('registers a scrollable memory section in SECTIONS with the Brain icon', () => {
    const sectionsBlock = SOURCE.match(/const SECTIONS = \[[\s\S]*?\];/);
    expect(sectionsBlock, 'SECTIONS not found').not.toBeNull();
    expect(sectionsBlock[0]).toMatch(/\{ key: 'memory', icon: Brain, scrollable: true \}/);
  });

  it('renders the Shared Memory section card with the MemorySection component', () => {
    expect(SOURCE).toMatch(/id="project-section-memory"/);
    expect(SOURCE).toMatch(/ref=\{setRef\('memory'\)\}/);
    expect(SOURCE).toMatch(/<MemorySection/);
    expect(SOURCE).toMatch(/sections\?\.memory \|\| \(isEn \? 'Shared Memory' : '共享记忆'\)/);
  });

  it('loads memories from the project_memories router on loadAll', () => {
    expect(SOURCE).toMatch(/authFetch\(`\/api\/projects\/\$\{projectId\}\/memories`\)/);
    expect(SOURCE).toMatch(/Array\.isArray\(data\.entries\) \? data\.entries : \[\]/);
    expect(SOURCE).toMatch(/setMemories\(settled\(me, \[\]\)\)/);
  });

  it('wires add / update / delete handlers to the project_memories REST endpoints', () => {
    // POST create
    expect(SOURCE).toMatch(/authFetch\(`\/api\/projects\/\$\{projectId\}\/memories`,\s*\{\s*method: 'POST'/);
    // PATCH update (pin toggle + edit both go through updateMemory)
    expect(SOURCE).toMatch(/authFetch\(`\/api\/projects\/\$\{projectId\}\/memories\/\$\{id\}`,\s*\{\s*method: 'PATCH'/);
    // DELETE hard-delete
    expect(SOURCE).toMatch(/authFetch\(`\/api\/projects\/\$\{projectId\}\/memories\/\$\{id\}`,\s*\{\s*method: 'DELETE'/);
  });

  it('MemorySection renders a usage bar, add form, and per-entry pin/edit/delete actions', () => {
    const section = SOURCE.match(/function MemorySection[\s\S]*?\n\}/);
    expect(section, 'MemorySection not found').not.toBeNull();
    const body = section[0];
    // Usage bar (2200-char budget)
    expect(body).toMatch(/LIMIT = 2200/);
    expect(body).toMatch(/usagePct/);
    // Inline add form
    expect(body).toMatch(/setAdding\(true\)/);
    expect(body).toMatch(/onAdd\(\{ content, importance: draftImportance \}\)/);
    // Pin toggle calls onUpdate with pinned flipped
    expect(body).toMatch(/onUpdate\(m\.id, \{ pinned: !m\.pinned \}\)/);
    // Edit path
    expect(body).toMatch(/startEdit\(m\)/);
    expect(body).toMatch(/onUpdate\(m\.id, \{ content, importance: editImportance \}\)/);
    // Hard delete
    expect(body).toMatch(/onDelete\(m\.id\)/);
    // Empty state exists
    expect(body).toMatch(/No shared memory yet/);
  });
});
