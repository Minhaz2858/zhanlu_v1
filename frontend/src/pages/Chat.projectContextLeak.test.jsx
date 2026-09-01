/**
 * Regression (2026-08-05): the project context was leaking from a
 * previous navigation into brand-new "default" chats. A user who had
 * earlier visited a project-scoped URL (e.g. ``/chat?projectName=X``)
 * would later open a fresh ``/chat`` (no project param) and find
 * their new conversation auto-tagged with that project — instead of
 * landing in the default ``general_assistant`` chat.
 *
 * Cause: ``handleAgentSend`` read the project context from the sticky
 * ``sessionStorage['zhanlu:lastProjectContext']`` value (set by a
 * previous visit to a project-scoped URL) and stamped it onto the
 * new ``AgentConversation`` row + wrote it back into the URL via
 * ``url.searchParams.set('projectName', ...)``.
 *
 * Intended behavior (per user spec):
 *   - URL has ``?projectName=X`` → the new conv is tagged with that project
 *   - URL has no projectName (default / general) → use general_assistant,
 *     new conv gets NO project_id.
 *
 * Source-text tests, same pattern as Chat.emptyPartials.test.jsx —
 * no DOM, no router, no mocks. The contract is in the JSX.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx project context leak', () => {
  it('handleAgentSend reads project context from URL params, not sessionStorage', () => {
    // Find the handleAgentSend block where a new AgentConversation is
    // created (``if (!convId) { ... }``). The projectCtx source must
    // be the current URL's ``?project=`` and ``?projectName=`` params,
    // NOT the sticky sessionStorage value. The sessionStorage read
    // is the leak — it persists across navigations, so a fresh /chat
    // page (no projectName in URL) would inherit the previous
    // project's context.
    //
    // Locate the projectCtx initialization inside the ``if (!convId)``
    // block. We accept either:
    //   - read URLSearchParams directly, OR
    //   - read window.location.href and parse it.
    // We do NOT accept a bare ``sessionStorage.getItem(...)`` call
    // as the source of projectCtx — that's the leak being fixed.
    const newConvBlock = SOURCE.match(/if\s*\(\s*!convId\s*\)\s*\{([\s\S]{0,2000}?)const conv = await createAgentConversation/);
    expect(newConvBlock, 'if (!convId) { ... } block before createAgentConversation not found').not.toBeNull();
    const blockSrc = newConvBlock[1];
    // Must reference the URL (window.location or URLSearchParams),
    // not just sessionStorage.
    expect(blockSrc).toMatch(/window\.location|URLSearchParams/);
    // The sessionStorage read must be GONE (or only used as a last
    // resort with a comment explaining the carry-over). The simplest
    // way to pin "no leak" is to assert sessionStorage is NOT the
    // primary source.
    expect(blockSrc).not.toMatch(/^[\s]*const raw = sessionStorage\.getItem\('zhanlu:lastProjectContext'\)/m);
  });
});
