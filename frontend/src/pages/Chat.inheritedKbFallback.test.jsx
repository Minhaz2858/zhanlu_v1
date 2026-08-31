/**
 * Regression (2026-08-07): the ``inheritedKbCount`` useEffect in Chat.jsx
 * only depended on ``pendingProjectId`` and gated its fetch on
 * ``if (!pendingProjectId) { setInheritedKbCount(0); return; }``.
 *
 * That made the badge inconsistent — same project, sometimes "1",
 * sometimes hidden — depending on which entry flow set the
 * pendingProject state:
 *
 *   - ``?projectName=ACME`` URL (no ``?project=ID``) → name set, id null
 *   - Sidebar "+ New Chat" → name set, id unchanged (often null)
 *   - ``selectSession`` of a legacy row (no ``project_id`` column populated)
 *
 * Meanwhile the server's ``_extend_with_project_kbs``
 * (``data_source_runtime.py:435``) gates on **either**
 * ``selected_project_id`` OR ``selected_project_name``:
 *     if selected_project_id or _normalize_project_name(selected_project_name):
 *         bound_ids = _extend_with_project_kbs(...)
 *
 * So the UI badge should mirror that dual-mode behavior. The fix is:
 *   - Add ``pendingProject`` to the useEffect dep list.
 *   - When ``pendingProjectId`` is null but ``pendingProject`` is set,
 *     fall back to filtering KBs by the legacy ``project`` name column.
 *
 * These tests pin both halves of the fix.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

/**
 * Locate the useEffect that fetches `inheritedKbCount`. We anchor on
 * the comment ("inheriting N data sources") so the regex doesn't drift
 * if the body of the effect is refactored.
 */
function locateInheritedKbEffect(source) {
  // Match from "useEffect(() => {" through the matching ", [...deps]);".
  // The effect body may contain nested braces, so a non-greedy match
  // would mis-fire; instead anchor on the trailing ", [deps]);" and
  // search backwards from the comment.
  const anchor = source.indexOf('inheriting N data sources');
  if (anchor < 0) return null;
  // Find the next "useEffect(" after the comment block.
  const startIdx = source.indexOf('useEffect(', anchor);
  if (startIdx < 0) return null;
  // Find the closing `, [deps]);` — the effect is identified by its
  // dependency array being "pendingProjectId" (or whatever the
  // current one is). Walk from startIdx forward.
  const tail = source.slice(startIdx);
  // The dep array is the only `[ ... ]` block whose contents end with `]);`
  const depMatch = tail.match(/,\s*\[([^\]]{0,200})\]\s*\)/);
  return {
    body: tail,
    anchorIdx: anchor,
    startIdx,
    depsRaw: depMatch ? depMatch[1] : null,
  };
}

describe('Chat.jsx inheritedKbCount useEffect fallback', () => {
  it('declares the useEffect that computes inheritedKbCount', () => {
    const found = locateInheritedKbEffect(SOURCE);
    expect(found, 'useEffect anchored by "inheriting N data sources" comment not found').not.toBeNull();
    expect(found.body).toMatch(/setInheritedKbCount/);
  });

  it('depends on pendingProjectId AND pendingProject', () => {
    // The dep array must include BOTH state vars so the effect re-runs
    // when either changes. Without `pendingProject` in the deps, the
    // badge would never refresh when only the name is set.
    const found = locateInheritedKbEffect(SOURCE);
    expect(found).not.toBeNull();
    const deps = (found.depsRaw || '').split(',').map((s) => s.trim());
    expect(deps, 'useEffect dep array missing pendingProjectId').toContain('pendingProjectId');
    expect(deps, 'useEffect dep array missing pendingProject').toContain('pendingProject');
  });

  it('does not short-circuit to 0 when only pendingProject (name) is set', () => {
    // The old code: `if (!pendingProjectId) { setInheritedKbCount(0); return; }`.
    // That gates the badge off whenever the entry flow forgot the id
    // (URL with no ?project=, sidebar + New Chat, legacy rows). The
    // fix keeps going when pendingProject is also set.
    const found = locateInheritedKbEffect(SOURCE);
    expect(found).not.toBeNull();
    const body = found.body;
    // Must not contain the old guard unconditionally.
    expect(
      body,
      'useEffect still unconditionally returns when pendingProjectId is null — must also keep going if pendingProject is set',
    ).not.toMatch(/if\s*\(\s*!\s*pendingProjectId\s*\)\s*\{\s*setInheritedKbCount\(0\)\s*;\s*return\s+undefined\s*;?\s*\}/);
    // The new guard must check both ids.
    expect(body).toMatch(/if\s*\(\s*!\s*pendingProjectId\s*&&\s*!\s*pendingProject\s*\)/);
  });

  it('filters KBs by project_id when pendingProjectId is set', () => {
    const found = locateInheritedKbEffect(SOURCE);
    expect(found).not.toBeNull();
    expect(found.body).toMatch(/kb\.project_id\s*===\s*pendingProjectId/);
  });

  it('falls back to filtering KBs by project name when only pendingProject is set', () => {
    // This is the regression: previously, when pendingProjectId was
    // null, the count was always 0 and the badge was never shown.
    // The fix filters by `kb.project === pendingProject` as a
    // fallback (the legacy `project` name column).
    const found = locateInheritedKbEffect(SOURCE);
    expect(found).not.toBeNull();
    const body = found.body;
    // Must have an else-if (or chained ternary) that filters by name.
    expect(body).toMatch(/kb\.project\s*===\s*pendingProject/);
  });
});
