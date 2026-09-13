/**
 * PlusMenu.jsx skill-catalog contract tests:
 *  - the "Select skill" picker lists the FULL catalog (not just the rows
 *    the current user created), fetched SPARSELY so the Tool body column
 *    (skill_md ~14KB/row) never ships for a 900+ row dropdown
 *  - legacy marketplace/custom name duplicates are collapsed by dedupeSkills
 *  - the search box matches name, description AND trigger (matches the
 *    placeholder "Search by name, description or trigger…")
 *
 * Source-text contract tests — same pattern as the other PlusMenu tests.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './PlusMenu.jsx'), 'utf8');

describe('PlusMenu.jsx skill catalog', () => {
  it('fetches the full catalog with a sparse fields list (no skill_md)', () => {
    expect(SOURCE).toMatch(/Tool\.list\('-updated_date', 2000, 0, \[/);
    // The body column must never be requested by the picker list.
    expect(SOURCE).toMatch(/'id', 'name', 'description', 'trigger', 'created_by_id', 'source', 'updated_date'/);
    expect(SOURCE).not.toMatch(/Tool\.list\([^)]*skill_md/);
  });

  it('does NOT filter the catalog to the current user (owner filter removed)', () => {
    // Regression: the picker used to show only created_by_id === me, which
    // hid the catalog entirely. It must now render every skill.
    expect(SOURCE).not.toMatch(/filter\(\(x\) => x\.created_by_id ===/);
    expect(SOURCE).not.toMatch(/auth\.me\(\).*created_by_id/s);
  });

  it('dedupes legacy marketplace/custom name duplicates preferring owned rows', () => {
    expect(SOURCE).toMatch(/function dedupeSkills\(rows\)/);
    expect(SOURCE).toMatch(/score = \(x\) => \(x\.created_by_id \? 2 : 0\) \+ \(x\.source === 'custom' \? 1 : 0\)/);
    expect(SOURCE).toMatch(/setSkills\(dedupeSkills\(tools\)\)/);
  });

  it('searches name, description and trigger', () => {
    expect(SOURCE).toMatch(/\(s\.name \|\| ''\)\.toLowerCase\(\)\.includes\(ql\)/);
    expect(SOURCE).toMatch(/\(s\.description \|\| ''\)\.toLowerCase\(\)\.includes\(ql\)/);
    expect(SOURCE).toMatch(/\(s\.trigger \|\| ''\)\.toLowerCase\(\)\.includes\(ql\)/);
  });
});
