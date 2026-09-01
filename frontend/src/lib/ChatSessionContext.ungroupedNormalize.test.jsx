/**
 * Regression: ChatSessionContext.jsx must NOT write the
 * "Ungrouped" i18n placeholder string to ChatSession rows on
 * session create. It must also normalize the read-back so legacy
 * rows (which have ``project = "Ungrouped"``) are treated as
 * ungrouped (``pendingProject = null``).
 *
 * Why this exists
 * ---------------
 * The chat input used to show a "Ungrouped" chip with an X button
 * for the default (no-project) state. Two places caused the bug:
 *
 *   1. ``base44.entities.ChatSession.create({ project: pendingProject
 *      || t.sessionList.ungrouped })`` — the placeholder string was
 *      written to the row instead of null.
 *   2. ``setPendingProjectState(s.project || null)`` in
 *      selectSession — the placeholder string was read back as
 *      truthy, so the chip rendered.
 *
 * The fix changes both ends:
 *   - The create call uses ``project: pendingProject || null``.
 *   - The read normalizes via ``s.project && !isUngrouped
 *     ProjectName(s.project)`` so the project NAME string is
 *     the source of truth (matching the sidebar's grouping
 *     key). The ``project_id`` FK is a best-effort
 *     denormalization for joins; it's passed through as-is
 *     when present, but its absence (e.g. legacy rows whose
 *     project wasn't matched by migration 020's backfill)
 *     must NOT hide the project name from the chat input
 *     chip.
 *
 * This test pins both halves of the contract.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
// Chat.jsx is one level up from lib/.
const CHAT_SOURCE = readFileSync(
  resolve(__dirname, '../pages/Chat.jsx'),
  'utf8',
);
const CTX_SOURCE = readFileSync(
  resolve(__dirname, './ChatSessionContext.jsx'),
  'utf8',
);

describe('Chat.jsx ChatSession.create does not write the Ungrouped placeholder', () => {
  it('imports isUngroupedProjectName (or uses it via a helper) for the create path', () => {
    // The new code path is ``project: pendingProject || null``
    // — no helper import required, just a literal null. The
    // old code was ``project: pendingProject || t.sessionList
    // .ungrouped``. Pin the absence of the placeholder write.
    expect(CHAT_SOURCE).not.toMatch(
      /base44\.entities\.ChatSession\.create\([\s\S]{0,200}project:\s*pendingProject\s*\|\|\s*t\.sessionList\.ungrouped/,
    );
  });

  it('writes project: pendingProject || null in both create sites', () => {
    // The two create sites are handleSend (creates lazily on
    // first message) and the pre-create helper (eager create
    // when the user types and the session doesn't exist yet).
    // Both must use ``pendingProject || null``.
    //
    // We assert per-site by slicing the exact body of each
    // ``base44.entities.ChatSession.create({...})`` call —
    // matching braces — so the assertion doesn't accidentally
    // pick up the ``project: pendingProject || t.sessionList
    // .ungrouped`` patterns in OTHER writes (UserFile.create,
    // function invokes) that are out of scope for this fix.
    const bodies = [];
    let pos = 0;
    while (true) {
      const i = CHAT_SOURCE.indexOf(
        'base44.entities.ChatSession.create(',
        pos,
      );
      if (i < 0) break;
      // Find the opening ``{`` after the ``(``.
      const openBrace = CHAT_SOURCE.indexOf('{', i);
      if (openBrace < 0) break;
      // Walk forward counting braces, accounting for nested
      // ``{...}`` in spread expressions like
      // ``...(cond ? { x: 1 } : {})``.
      let depth = 0;
      let endIdx = -1;
      for (let k = openBrace; k < CHAT_SOURCE.length; k++) {
        const ch = CHAT_SOURCE[k];
        if (ch === '{') depth++;
        else if (ch === '}') {
          depth--;
          if (depth === 0) { endIdx = k; break; }
        }
      }
      if (endIdx < 0) break;
      bodies.push(CHAT_SOURCE.slice(i, endIdx + 2)); // include ``});``
      pos = endIdx;
    }
    expect(
      bodies.length,
      'expected at least 2 ChatSession.create sites',
    ).toBeGreaterThanOrEqual(2);
    bodies.forEach((m, idx) => {
      expect(
        m,
        `ChatSession.create site #${idx + 1} should use project: ... || pendingProject || null`,
      ).toMatch(/project:\s*(?:newSessionProject\s*\|\|\s*)?pendingProject\s*\|\|\s*null/);
      expect(
        m,
        `ChatSession.create site #${idx + 1} should not write t.sessionList.ungrouped`,
      ).not.toMatch(/project:\s*pendingProject\s*\|\|\s*t\./);
    });
  });
});

describe('ChatSessionContext.jsx read-back normalization', () => {
  it('imports isUngroupedProjectName from @/lib/projectGrouping', () => {
    expect(CTX_SOURCE).toMatch(
      /import\s*\{[^}]*isUngroupedProjectName[^}]*\}\s*from\s*['"`]@\/lib\/projectGrouping['"`]/,
    );
  });

  it('selectSession uses s.project (the string) as the source of truth for the project name', () => {
    // The legacy code was ``setPendingProjectState(s.project ||
    // null)`` which would happily set pendingProject to
    // "Ungrouped" for legacy rows. The original fix gated on
    // ``s.project_id`` (FK), but that threw away the project
    // name for legacy rows whose FK wasn't backfilled by
    // migration 020 — so the project chip disappeared on
    // reopen even though the sidebar grouped the session
    // correctly. The current fix gates on ``s.project`` (the
    // string, matching the sidebar's grouping key) and only
    // applies the Ungrouped-string defense.
    const selectBlock = CTX_SOURCE.match(
      /const selectSession\s*=\s*useCallback[\s\S]*?\}, \[sessions\]\);/,
    );
    expect(selectBlock, 'selectSession not found').not.toBeNull();
    expect(selectBlock[0]).toMatch(
      /s\.project\s*&&\s*!isUngroupedProjectName/,
    );
    // The old FK-gated pattern must be gone (it caused the
    // "project chip disappears on reopen" regression).
    expect(selectBlock[0]).not.toMatch(
      /s\.project_id\s*&&\s*!isUngroupedProjectName/,
    );
    // The old single-line pattern must be gone.
    expect(selectBlock[0]).not.toMatch(
      /setPendingProjectState\(s\.project\s*\|\|\s*null\)/,
    );
  });

  it('adoptSessionProject uses the same normalization (s.project as source of truth)', () => {
    const adoptBlock = CTX_SOURCE.match(
      /const adoptSessionProject\s*=\s*useCallback[\s\S]*?\}, \[sessions\]\);/,
    );
    expect(adoptBlock, 'adoptSessionProject not found').not.toBeNull();
    expect(adoptBlock[0]).toMatch(
      /s\.project\s*&&\s*!isUngroupedProjectName/,
    );
    expect(adoptBlock[0]).not.toMatch(
      /s\.project_id\s*&&\s*!isUngroupedProjectName/,
    );
  });

  it('selectSession and adoptSessionProject fall back to s.project when project_id is null (legacy rows)', () => {
    // Regression: migration 020 only backfills project_id when
    // the project name + created_by_id matches an existing
    // Project row. Sessions created before the migration (or
    // whose project was created by a different user) keep
    // ``project = "ACME"`` but have ``project_id = null``.
    // The sidebar groups these sessions under "ACME" by
    // reading ``s.project``, so the chat input must do the
    // same — otherwise the project chip disappears on reopen
    // even though the session is correctly grouped.
    //
    // The fix: gate the project NAME on ``s.project`` (the
    // string), NOT on ``s.project_id`` (the FK). The FK is a
    // best-effort denormalization for joins; it's passed
    // through as-is to pendingProjectId when present, but its
    // absence must not hide the project name.
    for (const blockName of ['selectSession', 'adoptSessionProject']) {
      const block = CTX_SOURCE.match(
        new RegExp(
          `const ${blockName}\\s*=\\s*useCallback[\\s\\S]*?\\}, \\[sessions\\]\\);`,
        ),
      );
      expect(block, `${blockName} not found`).not.toBeNull();
      expect(block[0]).toMatch(/s\.project\s*&&\s*!isUngroupedProjectName/);
      expect(block[0]).not.toMatch(/s\.project_id\s*&&\s*!isUngroupedProjectName/);
      expect(block[0]).toMatch(/setPendingProjectId\(s\.project_id\s*\|\|\s*null\)/);
    }
  });

  it('newChat normalizes the Ungrouped placeholder to null', () => {
    // newChat is called by handleNewChat() with the current
    // project (or null). For defense in depth, if a caller
    // passes the placeholder string, it must be treated as
    // null.
    const newChatBlock = CTX_SOURCE.match(
      /const newChat\s*=\s*useCallback[\s\S]*?\}, \[\]\);/,
    );
    expect(newChatBlock, 'newChat not found').not.toBeNull();
    expect(newChatBlock[0]).toMatch(
      /isUngroupedProjectName\([^)]*\)\s*\?\s*null/,
    );
  });
});
