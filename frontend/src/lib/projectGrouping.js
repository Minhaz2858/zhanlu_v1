// Single source of truth for "is this project name the Ungrouped
// placeholder?" — used in three places to keep the chat input
// chip, the session-create write, and the session-read
// normalization in agreement.
//
// Background: a chat session is "Ungrouped" when no project has
// been bound to it. The "Ungrouped" label is purely a UI string
// (it's shown in the sidebar's group label and in the project
// picker), not a stored value. Older code used to write
// ``project: pendingProject || t.sessionList.ungrouped`` which
// stored the literal string "Ungrouped" / "未分组" on ChatSession
// rows, and then read it back into the UI state and rendered it
// as a chip with an X (close) button — which is wrong, because
// "Ungrouped" is the *default* state, not a *selected* state
// with something to clear.
//
// This module lets the rest of the code normalize both ways:
//   - on write: don't store the placeholder, store null
//   - on read: treat placeholder strings as null (for legacy
//     data that was written before the fix)
//   - in the UI: don't render a chip for placeholder names

// The set of names that mean "ungrouped" across locales. Add to
// this set when a new locale is added; the comparison is exact
// (case-sensitive, no normalization) because the chat UI only
// ever produces these exact strings from the i18n catalog.
const UNGROUPED_PROJECT_NAMES = Object.freeze([
  'Ungrouped',  // English — t.sessionList.ungrouped / t.automation.ungrouped / t.automation.globalProject
  '未分组',      // Chinese — same three i18n keys
]);

const _set = new Set(UNGROUPED_PROJECT_NAMES);

/**
 * @param {unknown} name
 * @returns {boolean} true if ``name`` is null/undefined/empty OR
 *   matches one of the known Ungrouped placeholder strings.
 */
export function isUngroupedProjectName(name) {
  if (name == null) return true;
  if (typeof name !== 'string') return false;
  if (name.length === 0) return true;
  return _set.has(name);
}

export { UNGROUPED_PROJECT_NAMES };
