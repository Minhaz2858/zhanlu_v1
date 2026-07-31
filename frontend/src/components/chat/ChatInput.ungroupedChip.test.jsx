/**
 * Regression: ChatInput.jsx must NOT render the "Ungrouped" chip
 * for the default (no-project) state. The chip is only for real
 * project names.
 *
 * Why this exists
 * ---------------
 * A user reported that the chat input showed a "Ungrouped" chip
 * with an X (close) button even when no project was selected.
 * "Ungrouped" is the *default* state (no project bound), not a
 * *selected* state with a removable tag — showing it as a chip
 * is confusing because clicking X would do nothing (there's
 * nothing selected to clear). The fix hides the chip when
 * ``pendingProject`` is the Ungrouped placeholder (in any
 * locale) or null/empty.
 *
 * This test pins the contract: the chip element's outer
 * condition must reference the isUngroupedProjectName helper
 * (or, for legacy code, an equivalent exact-match check). A
 * future refactor that drops the helper will fail this test.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ChatInput.jsx'), 'utf8');

describe('ChatInput.jsx project chip', () => {
  it('imports isUngroupedProjectName from @/lib/projectGrouping', () => {
    // The chip's visibility is gated by isUngroupedProjectName.
    // Pin the import so a tree-shake / refactor can't quietly
    // remove the gate.
    expect(SOURCE).toMatch(
      /import\s*\{[^}]*isUngroupedProjectName[^}]*\}\s*from\s*['"`]@\/lib\/projectGrouping['"`]/,
    );
  });

  it('gates the project chip on !isUngroupedProjectName(pendingProject)', () => {
    // The chip's outer condition must include
    // ``&& !isUngroupedProjectName(pendingProject)`` so the
    // Ungrouped placeholder never renders as a chip.
    const chipBlock = SOURCE.match(
      /\{pendingProject[\s\S]{0,200}?</,
    );
    expect(chipBlock, 'project chip block not found').not.toBeNull();
    expect(chipBlock[0]).toMatch(
      /!isUngroupedProjectName\(\s*pendingProject\s*\)/,
    );
  });
});
