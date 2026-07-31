/**
 * Regression: Chat.jsx must REUSE the same AgentConversation across
 * multiple sends in the same ChatSession — it must NOT create a new
 * conversation for every message.
 *
 * Why this exists
 * ---------------
 * A user reported that the "Recent Chats" list on the Project Detail
 * page showed many duplicate "general_assistant" entries (one per
 * user message), and that the chat page URL never had a `?conv=`
 * query string even mid-conversation.
 *
 * Root cause: handleSend() called handleAgentSend() with
 * ``sessionId: null`` hardcoded. The handleAgentSend() function
 * reuses the AgentConversation id when ``sessionId`` is truthy, but
 * falls through to ``createAgentConversation()`` when it is null —
 * so every message created a brand-new AgentConversation row. The
 * chat id was also never pushed to the URL, so reloads couldn't
 * resume the session.
 *
 * This test pins the fix:
 *   1. The handleSend → handleAgentSend call passes the current
 *      streamingConvId (or session.conversation_id) as sessionId —
 *      not null.
 *   2. handleAgentSend pushes the URL to ``/?conv={id}`` after
 *      creating a new conversation, so reloads preserve it.
 *   3. handleNewChat clears the URL so a new session starts fresh.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx AgentConversation reuse', () => {
  it('does NOT pass sessionId: null to handleAgentSend', () => {
    // The fix replaces the hardcoded null with a value read from
    // the in-flight streamingConvId state, the active session's
    // conversation_id, or the URL. Pin the call site so a future
    // refactor can't quietly reintroduce the bug.
    const callSite = SOURCE.match(
      /handleAgentSend\([\s\S]{0,400}?\}\)/,
    );
    expect(callSite, 'handleAgentSend call site not found').not.toBeNull();
    expect(callSite[0]).not.toMatch(/sessionId:\s*null/);
  });

  it('passes a real sessionId to handleAgentSend (streamingConvId / session / URL)', () => {
    // The sessionId must come from one of:
    //   - the in-flight streamingConvId state (reuses the conv
    //     created in the same session, across message turns)
    //   - getSession(sid)?.conversation_id (the ChatSession's
    //     stored link — survives page reload if the write
    //     completed)
    //   - the URL ?conv= param (works before the ChatSession
    //     write lands, and on reloads)
    //
    // The resolve block typically lives ABOVE the handleAgentSend
    // call site — the call itself just passes a non-null
    // sessionId. We split the assertion into two halves so the
    // test is robust to where the resolve block lives in the
    // function:
    //
    //   1. The call site must pass a non-null sessionId.
    //   2. The file must contain at least one of the three
    //      resolution sources (state ref, getSession,
    //      URLSearchParams) near the call.
    const callSite = SOURCE.match(
      /handleAgentSend\([\s\S]{0,400}?\}\);/,
    );
    expect(callSite, 'handleAgentSend call site not found').not.toBeNull();
    expect(callSite[0]).toMatch(/sessionId:\s*(?!null)/);
    // Locate the call's start position and look at the 2000
    // chars of context immediately above it for a resolve
    // block. This handles the common pattern where the
    // resolution happens right before the call.
    const callStart = SOURCE.indexOf('handleAgentSend(');
    const beforeCall = SOURCE.slice(Math.max(0, callStart - 2000), callStart);
    const usesSession = /getSession\s*\(/.test(beforeCall);
    const usesUrl = /URLSearchParams/.test(beforeCall);
    const usesState = /streamingConvId/.test(beforeCall);
    expect(
      usesSession || usesUrl || usesState,
      'sessionId must be resolved from streamingConvId, getSession, or URLSearchParams',
    ).toBe(true);
  });

  it('handleAgentSend pushes the new conv id to the URL after creating it', () => {
    // After createAgentConversation() returns the new conv, the
    // code must push the URL to /?conv={id} so the user can
    // bookmark, share, or reload the session. We accept either
    // implementation: a literal ``'?conv=' + convId`` in
    // history.replaceState, or the safer ``url.searchParams.set
    // ('conv', convId)`` approach.
    const fn = SOURCE.match(
      /async function handleAgentSend[\s\S]*?^  \}/m,
    );
    expect(fn, 'handleAgentSend function not found').not.toBeNull();
    const pushes = (
      /history\.(?:push|replace)State\([^)]*['"`][^'"`]*\?conv=/.test(fn[0]) ||
      /url\.searchParams\.set\(['"`]conv['"`]/.test(fn[0])
    );
    expect(pushes, 'handleAgentSend must set ?conv= in the URL after create').toBe(true);
  });

  it('handleNewChat clears the ?conv= URL parameter', () => {
    // When the user clicks "+ New Task" (handleNewChat), the
    // active session is reset and the URL must be cleared of the
    // ?conv= param so a fresh send doesn't accidentally resume
    // the previous conversation. We accept either:
    //   - a full ``window.history.replaceState({}, '', '/')`` that
    //     wipes the URL entirely, OR
    //   - a ``url.searchParams.delete('conv')`` followed by a
    //     ``replaceState({}, '', url.toString())`` that preserves
    //     other useful params (project, projectName) for the
    //     new chat.
    const fn = SOURCE.match(
      /function handleNewChat[\s\S]*?\n  \}/,
    );
    expect(fn, 'handleNewChat function not found').not.toBeNull();
    const clears = (
      /history\.replaceState\([^)]*['"`]\/['"`]/.test(fn[0]) ||
      /url\.searchParams\.delete\(['"`]conv['"`]\)/.test(fn[0])
    );
    expect(clears, 'handleNewChat must clear the ?conv= URL param').toBe(true);
  });

  it('handleAgentSend reuses sessionId when provided (no fallback to createAgentConversation)', () => {
    // The contract: ``let convId = sessionId; if (!convId) { create
    // new conv }`` — pin both halves. If a future refactor moves
    // the createAgentConversation call above the sessionId check,
    // this test fails.
    const fn = SOURCE.match(
      /async function handleAgentSend[\s\S]*?^  \}/m,
    );
    expect(fn, 'handleAgentSend function not found').not.toBeNull();
    // The reuse check must appear BEFORE the createAgentConversation
    // call. We pin the order with a positional check.
    const reuseIdx = fn[0].search(/let convId\s*=\s*sessionId/);
    const createIdx = fn[0].search(/createAgentConversation\s*\(/);
    expect(reuseIdx, 'reuse line not found').toBeGreaterThan(-1);
    expect(createIdx, 'create line not found').toBeGreaterThan(-1);
    expect(reuseIdx).toBeLessThan(createIdx);
  });

  it('createAgentConversation does NOT send ``metadata.name`` (was misusing it as the title)', () => {
    // Regression: the chat page used to send
    //   ``metadata: { name: activeAgent.name, ... }``
    // which the backend (``agents.py`` line 1854-1855) silently
    // treated as the conversation title. Result: every Recent
    // Chats row on the Project Detail page showed the agent's
    // name ("general_assistant") instead of a meaningful title
    // derived from the user's first message.
    //
    // The fix: don't put ``name`` in metadata at all (it's
    // redundant with the top-level ``agent_name`` field). The
    // title is sent as a top-level ``title`` field — see the
    // next test case for that half of the contract.
    //
    // We pin the fix by asserting that the entire call site
    // doesn't contain ``name: activeAgent.name``. (If a future
    // refactor moves the field somewhere else, this test still
    // catches the regression because the backend will still
    // treat ``metadata.name`` as the title.)
    const createCall = SOURCE.match(
      /await createAgentConversation\([\s\S]*?\);\s*$/m,
    );
    expect(createCall, 'createAgentConversation call not found').not.toBeNull();
    expect(createCall[0]).not.toMatch(/name:\s*activeAgent\.name/);
    // Also assert the call has THREE arguments (agentName,
    // metadata, title) — the old call had only two.
    const commas = (createCall[0].match(/,\s*[^{]/g) || []).length;
    // We don't count commas inside nested {} objects, so the
    // count is approximate. What matters is that there's at
    // least one comma AFTER the metadata object's closing
    // brace — i.e., a third argument exists.
    expect(createCall[0]).toMatch(/\}\s*,\s*\n\s*\/\/ Derive the conversation title/);
  });

  it('createAgentConversation sends a ``title`` derived from the first user message', () => {
    // The other half of the contract: the title comes from the
    // ``text`` parameter to handleAgentSend (the first user
    // message), not from the agent name. We pin that the call
    // has a third argument (the title) and that the value
    // references ``text`` so it tracks the user's message.
    const createCall = SOURCE.match(
      /await createAgentConversation\([\s\S]*?\);\s*$/m,
    );
    expect(createCall, 'createAgentConversation call not found').not.toBeNull();
    // The third argument appears AFTER the metadata object's
    // closing ``},`` and BEFORE the call's closing ``);``. We
    // match that whole range and assert it references ``text``.
    const thirdArg = createCall[0].match(
      /\}\s*,\s*([\s\S]+?)\)\s*;?\s*$/,
    );
    expect(thirdArg, 'third argument not found').not.toBeNull();
    expect(thirdArg[1]).toMatch(/\btext\b/);
  });

  it('chat input draft is persisted to localStorage (survives page refresh)', () => {
    // Regression: a user reported that typing in the chat input and
    // refreshing the page wiped the typed text — they expected a
    // draft-style persistence like Gmail/Slack. The fix persists
    // inputValue to localStorage via the shared draftManager and
    // restores it on mount, so a typed-but-not-sent message survives
    // a page refresh (and a route change that re-mounts this page).
    //
    // We pin the contract as source-pattern assertions:
    //   1. saveDraft and clearDraft are imported from
    //      ``@/lib/draftManager``.
    //   2. inputValue's useState uses a LAZY initializer (function
    //      form) that reads from localStorage — not the eager
    //      ``useState('')`` that would lose the restored value on
    //      the first render.
    //   3. A useEffect mirrors inputValue to localStorage via
    //      ``saveDraft('chat_input_draft', inputValue, ...)``.
    //   4. ``clearDraft('chat_input_draft')`` is called at the
    //      primary send site (handleSend) so a sent message doesn't
    //      leave a stale draft that re-appears on refresh.

    // (1) Imports
    expect(SOURCE).toMatch(
      /import\s*\{\s*saveDraft\s*,\s*clearDraft\s*\}\s*from\s*['"]@\/lib\/draftManager['"]/,
    );

    // (2) Lazy initializer for inputValue — must read from
    // localStorage, not just default to ''. Pin the localStorage
    // key name so a future "per-session draft" refactor doesn't
    // silently break the contract. We don't try to regex-parse the
    // multi-line arrow function; we just assert the key building
    // blocks are present in the source.
    expect(SOURCE).toMatch(
      /const\s*\[\s*inputValue\s*,\s*setInputValue\s*\]\s*=\s*useState\(\s*\(\s*\)\s*=>/,
    );
    expect(SOURCE).toMatch(
      /localStorage\.getItem\(\s*['"]draft:chat_input_draft['"]\s*\)/,
    );

    // (3) useEffect that mirrors inputValue to localStorage
    expect(SOURCE).toMatch(
      /useEffect\s*\(\s*\(\s*\)\s*=>\s*\{[\s\S]*?saveDraft\(\s*['"]chat_input_draft['"]\s*,\s*inputValue/,
    );

    // (4) clearDraft is called in the primary send path. We don't
    // pin the exact line number (the send flow has multiple
    // branches — steer, normal send, + New Task) but we require
    // AT LEAST one clearDraft call inside handleSend so a sent
    // message doesn't leave a stale draft.
    const handleSendBody = SOURCE.match(
      /async function handleSend\([\s\S]*?^  \}/m,
    );
    expect(handleSendBody, 'handleSend function not found').not.toBeNull();
    expect(handleSendBody[0]).toMatch(/clearDraft\(\s*['"]chat_input_draft['"]\s*\)/);
  });
});
