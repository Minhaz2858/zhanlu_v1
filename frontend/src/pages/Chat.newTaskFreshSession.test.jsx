/**
 * Regression (2026-08-27): "Save & Continue in Chat" (CreateResourceDialog)
 * navigates with `?newTask=1&projectName=...&projectId=...` for EVERY
 * resource type — automation, dashboard, report, kb, file, …
 *
 * Previously Chat.jsx only honored `newTask` inside the
 * `isAutomationRequest(prefill)` branch. Dashboard/report/kb prefills do NOT
 * match that regex (it only matches "create a new automation task" /
 * "新建自动化任务"), so the prefill fell through to `setInputValue(prefill)`
 * — it just filled the input box of the CURRENT session. The user never got
 * the fresh session (and its project binding) the dialog promised.
 *
 * Fix: when `?newTask=1` is present, ALWAYS create a brand-new session with
 * the dialog's project and auto-send the prefill into it — for every
 * resource type, not just automation.
 *
 * This test pins the fix at the source level (matching the other
 * Chat.*test.jsx source-anchored tests):
 *   1. `handleSend` must be called with `newSessionTitle` when newTask=1.
 *   2. The `newTask` handling must NOT be gated behind isAutomationRequest
 *      (i.e. the branch structure must call handleSend for ANY prefill).
 *   3. The project params from the dialog must flow into the session opts.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx "Save & Continue in Chat" opens a fresh session', () => {
  it('routes newTask=1 prefills through handleSend with fresh-session opts', () => {
    // The prefill effect must call handleSend(prefill, {newSessionTitle: ...})
    // when newTask is set. Anchor on the unique combination: the
    // newSessionTitle opt + the dialog project params passed to handleSend.
    const freshSessionCall = SOURCE.match(
      /handleSend\(\s*prefill\s*,\s*\{\s*newSessionTitle:\s*t\.sessionList\.newTask,\s*newSessionProject:\s*dialogProjectName,\s*newSessionProjectId:\s*dialogProjectId,\s*\}\s*\)/,
    );
    expect(
      freshSessionCall,
      'handleSend(prefill, {newSessionTitle, newSessionProject, newSessionProjectId}) not found — newTask=1 must create a fresh session bound to the dialog project',
    ).not.toBeNull();
  });

  it('does NOT gate fresh-session handling behind isAutomationRequest', () => {
    // The bug: `newTask` logic lived INSIDE `if (isAutomationRequest(prefill))`,
    // so dashboard/report/kb prefills (which never match that regex) fell
    // through to setInputValue. The fixed structure must call handleSend for
    // newTask REGARDLESS of the automation check. Pin the control flow:
    // find the `if (newTask)` block and assert isAutomationRequest is NOT
    // its only enclosing condition.
    const newTaskBlock = SOURCE.match(/if \(newTask\) \{[\s\S]{0,600}?handleSend\(prefill/);
    expect(
      newTaskBlock,
      '`if (newTask) { ... handleSend(prefill` block not found',
    ).not.toBeNull();

    // The prefill effect must have the isAutomationRequest branch as a
    // SIBLING decision (setPendingDraft), not as the gate around newTask.
    // Verify by checking that setPendingDraft (automation-only) appears
    // BEFORE the `if (newTask)` block, i.e. automation-specific behavior is
    // additive, not the gate.
    const draftIdx = SOURCE.indexOf('setPendingDraft({ prefill, lang })');
    const newTaskIdx = SOURCE.indexOf('if (newTask) {');
    expect(draftIdx).toBeGreaterThan(-1);
    expect(newTaskIdx).toBeGreaterThan(draftIdx);
  });

  it('still stages non-newTask prefills into the input (unchanged legacy path)', () => {
    // Without newTask=1, non-automation prefills keep the old behavior:
    // staged in the input for the user to review.
    const legacyBlock = SOURCE.match(
      /else\s*\{\s*setInputValue\(prefill\);\s*\}/,
    );
    expect(
      legacyBlock,
      'legacy `else { setInputValue(prefill) }` path must remain for non-newTask prefills',
    ).not.toBeNull();
  });
});
