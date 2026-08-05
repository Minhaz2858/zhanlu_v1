/**
 * Regression (2026-08-05): the project context was leaking from a
 * previous navigation into brand-new "default" chats. A user who had
 * earlier visited ``/chat?projectName=Ecisco+BI`` would later open a
 * fresh ``/chat`` (no project param) and find their new conversation
 * auto-tagged with the Ecisco+BI project — and consequently auto-
 * routed to ``ecisco_bi_assistant`` instead of the default
 * ``general_assistant``.
 *
 * Two compounding leaks caused this:
 *
 * 1. ``handleAgentSend`` read the project context from the sticky
 *    ``sessionStorage['zhanlu:lastProjectContext']`` value (set by a
 *    previous visit to a project-scoped URL) and stamped it onto the
 *    new ``AgentConversation`` row + wrote it back into the URL via
 *    ``url.searchParams.set('projectName', ...)``.
 * 2. The mount-time ``ecisco_bi_assistant`` auto-select was not gated
 *    on ``!convId``, so opening an existing conv that had been
 *    created with ``general_assistant`` could race against the conv
 *    rehydration (which sets ``activeAgent`` from ``conv.agent_name``)
 *    and flip the agent to ``ecisco_bi_assistant`` on render.
 *
 * Intended behavior (per user spec):
 *   - URL has ``?projectName=Ecisco+BI`` → use ecisco_bi_assistant
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

  it('mount-time ecisco_bi_assistant auto-select is gated on !convId', () => {
    // The mount-time auto-select IIFE that sets activeAgent to
    // ecisco_bi_assistant must be gated so it does NOT fire when
    // the URL has ``?conv=`` — the existing conv's stored
    // ``agent_name`` is the source of truth there (set by the conv
    // rehydration block right below). Without the ``!convId`` gate,
    // the two async operations race and an existing general_assistant
    // conv can flip to ecisco_bi_assistant on render.
    //
    // The condition must include a convId check. We accept either
    // ``!convId`` or ``convId === null`` or equivalent.
    const autoSelectMatch = SOURCE.match(/if\s*\(\s*!agentId\s*&&\s*projectName([\s\S]{0,400}?)\{[\s\S]{0,800}?setActiveAgent\(eciscoAgent\)/);
    expect(autoSelectMatch, 'ecisco_bi_assistant auto-select block not found').not.toBeNull();
    const conditionAndBody = autoSelectMatch[0];
    // The condition must include a convId guard. The convId variable
    // is read from params.get('conv') somewhere in the same
    // useEffect — we just assert the auto-select block references
    // convId.
    expect(conditionAndBody).toMatch(/convId/);
  });
});
