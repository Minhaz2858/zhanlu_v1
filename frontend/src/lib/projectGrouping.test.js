/**
 * Tests for the projectGrouping helper — single source of truth
 * for "is this project name the Ungrouped placeholder?".
 *
 * Why this exists
 * ---------------
 * The chat input used to show a "Ungrouped" chip with an X button
 * for sessions without a bound project. The "Ungrouped" state is
 * the *default* — there's nothing selected, no tag to clear. The
 * fix: hide the chip when pendingProject matches the Ungrouped
 * placeholder (in any locale), and don't write the placeholder
 * string to ChatSession rows in the first place.
 *
 * These tests pin the helper so the i18n coverage and the
 * null/empty handling are stable. A future refactor that drops
 * the Chinese entry, or that treats empty-string as a real
 * project, will fail one of these.
 */

import { describe, it, expect } from 'vitest';
import { isUngroupedProjectName, UNGROUPED_PROJECT_NAMES } from './projectGrouping';

describe('isUngroupedProjectName', () => {
  it('returns true for null', () => {
    expect(isUngroupedProjectName(null)).toBe(true);
  });

  it('returns true for undefined', () => {
    expect(isUngroupedProjectName(undefined)).toBe(true);
  });

  it('returns true for the empty string', () => {
    expect(isUngroupedProjectName('')).toBe(true);
  });

  it('returns true for the English placeholder "Ungrouped"', () => {
    expect(isUngroupedProjectName('Ungrouped')).toBe(true);
  });

  it('returns true for the Chinese placeholder "未分组"', () => {
    expect(isUngroupedProjectName('未分组')).toBe(true);
  });

  it('returns false for real project names', () => {
    expect(isUngroupedProjectName('Acme Corp')).toBe(false);
    expect(isUngroupedProjectName('Marketing Team')).toBe(false);
    expect(isUngroupedProjectName('Customer Support')).toBe(false);
  });

  it('returns false for names that *contain* the placeholder as a substring', () => {
    // The set is an exact-match set — "Ungrouped chat" is a
    // legitimate project name (unlikely, but the helper should
    // not over-match).
    expect(isUngroupedProjectName('Ungrouped chat')).toBe(false);
    expect(isUngroupedProjectName('My Ungrouped Project')).toBe(false);
  });

  it('returns false for case variants of the placeholder', () => {
    // The set is case-sensitive. "ungrouped" (lowercase) is a
    // legitimate project name; the helper should not over-match.
    expect(isUngroupedProjectName('ungrouped')).toBe(false);
    expect(isUngroupedProjectName('UNGROUPED')).toBe(false);
  });

  it('returns false for non-string inputs (defense in depth)', () => {
    expect(isUngroupedProjectName(0)).toBe(false);
    expect(isUngroupedProjectName(false)).toBe(false);
    expect(isUngroupedProjectName({})).toBe(false);
    expect(isUngroupedProjectName([])).toBe(false);
  });

  it('exposes a frozen set of placeholder names for iteration', () => {
    // The set is exported (UNGROUPED_PROJECT_NAMES) so the UI
    // can iterate it for an "exclude" filter. Pin the freeze
    // so a refactor can't accidentally mutate it.
    expect(Array.isArray(UNGROUPED_PROJECT_NAMES)).toBe(true);
    expect(Object.isFrozen(UNGROUPED_PROJECT_NAMES)).toBe(true);
    // Pin the minimum coverage: English and Chinese must be in
    // the set. If a new locale is added, this test should be
    // updated to assert the new entry.
    expect(UNGROUPED_PROJECT_NAMES).toContain('Ungrouped');
    expect(UNGROUPED_PROJECT_NAMES).toContain('未分组');
  });
});
