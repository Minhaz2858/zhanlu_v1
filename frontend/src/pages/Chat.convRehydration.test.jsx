/**
 * Regression: Chat.jsx must set the active session from `?conv=` so
 * that the URL is preserved on refresh and the user can continue the
 * conversation.
 *
 * The bug
 * --------
 * 1. User opens ``/?conv=fea3f1b9-...`` in a chat.
 * 2. They refresh the page.
 * 3. The rehydration useEffect (~line 390-420) loads the conv's
 *    messages and resolves the agent — but it does NOT set
 *    ``activeId`` (the chat-session id) because the conv doesn't
 *    carry it; only the ChatSession does.
 * 4. The cleanup useEffect (~line 535-544) sees ``activeId === null``
 *    and calls ``url.searchParams.delete('conv')`` + ``replaceState``
 *    — wiping the deep link.
 * 5. The user now sees the agent's replies but has no active session,
 *    so the chat input is non-functional and the URL is ``/``.
 *
 * What the test pins
 * ------------------
 * - ``selectSession`` (from useChatSession) is destructured — required
 *   to call the context's setter.
 * - A useEffect in Chat.jsx reacts to ``sessions`` and ``activeId`` and
 *   calls ``selectSession`` when the URL has ``?conv=`` and a
 *   matching session is loaded.
 * - That useEffect is not gated on ``activeId === null`` only — it
 *   must also re-evaluate when sessions arrive (the typical case on
 *   refresh: rehydration effect runs first, sessions list loads
 *   asynchronously, so the dependency on ``sessions`` is required).
 *
 * Why source-text tests (not React Testing Library)?
 * --------------------------------------------------
 * Same pattern as the other Chat / ChatInput tests in this repo:
 * no DOM, no router, no mocks. The contract is the *code shape*;
 * a refactor that silently drops the conv→session link fails loudly.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx ?conv= rehydration', () => {
  it('destructures selectSession from useChatSession()', () => {
    // The new useEffect below calls ``selectSession(sid)`` to link
    // the conv deep-link back to its owning ChatSession. Without
    // destructuring, the symbol is undefined at runtime and the
    // effect silently no-ops.
    const destructure = SOURCE.match(
      /const\s*\{[\s\S]{0,2500}?\}\s*=\s*useChatSession\(\)/,
    );
    expect(destructure, 'useChatSession destructure not found').not.toBeNull();
    expect(destructure[0]).toMatch(/selectSession/);
  });

  it('defines a useEffect that resolves ?conv= to a session id', () => {
    // The new useEffect must:
    //   1. read ``?conv=`` from the URL
    //   2. find a session whose ``conversation_id`` matches
    //   3. call ``selectSession(<sid>)``
    const effects = [
      ...SOURCE.matchAll(/useEffect\(([\s\S]*?)\},\s*\[[^\]]+\]\)/g),
    ];
    expect(effects.length, 'no useEffect blocks found').toBeGreaterThan(0);
    const convEffect = effects.find((m) => {
      const body = m[1];
      return (
        /\.get\(['"]conv['"]\)/.test(body) &&
        /conversation_id/.test(body) &&
        /selectSession\s*\(/.test(body)
      );
    });
    expect(
      convEffect,
      'No useEffect reads ?conv=, looks up conversation_id, and calls selectSession',
    ).not.toBeNull();
  });

  it('the conv rehydration effect depends on sessions + activeId', () => {
    // The effect must re-run when ``sessions`` (the async list) or
    // ``activeId`` changes. Without ``sessions`` in deps, a refresh
    // that lands before refreshSessions() resolves will silently
    // no-op, and the cleanup effect at line 535-544 will then wipe
    // the ``?conv=`` from the URL.
    const effects = [
      ...SOURCE.matchAll(/useEffect\(([\s\S]*?)\},\s*\[([^\]]+)\]\)/g),
    ];
    const convEffect = effects.find((m) => {
      const body = m[1];
      return (
        /\.get\(['"]conv['"]\)/.test(body) &&
        /conversation_id/.test(body) &&
        /selectSession\s*\(/.test(body)
      );
    });
    expect(convEffect, 'conv rehydration effect not found').not.toBeNull();
    const deps = convEffect[2];
    expect(deps).toMatch(/sessions/);
    expect(deps).toMatch(/activeId/);
  });

  it('the ?conv= cleanup effect does NOT fire on initial mount (regression: refresh wipes deep link)', () => {
    // On a deep-link ``?conv=<id>`` the cleanup useEffect used to
    // fire on initial mount (because ``activeId`` starts as null)
    // and strip ``?conv=`` from the URL — before the deep-link
    // restore effect ever got a chance to look up the matching
    // ChatSession in the async-loaded ``sessions`` list. The
    // result: refresh lost the conversation. The fix tracks
    // previous activeId via a ref and only cleans up on the
    // actual non-null → null transition.
    // Find the cleanup block — the useEffect that handles
    // activeId becoming null and deletes ``?conv=``.
    const effectRe =
      /useEffect\(\s*\(\)\s*=>\s*\{[\s\S]*?url\.searchParams\.delete\(['"]conv['"]\)[\s\S]*?\}\s*,\s*\[([^\]]+)\]\s*\)/;
    const match = SOURCE.match(effectRe);
    expect(match, '?conv= cleanup useEffect not found').not.toBeNull();
    // The fix MUST use a useRef to track the previous activeId
    // value, and skip the cleanup when ``prev`` is falsy.
    const body = match[0];
    expect(body, 'cleanup effect must use useRef to track previous activeId').toMatch(
      /useRef\s*\(\s*null\s*\)/,
    );
    expect(body, 'cleanup effect must guard against initial mount (no prev)').toMatch(
      /if\s*\(\s*!\s*prev\s*\)\s*return/,
    );
    // The dep array still contains activeId (it still reacts to
    // the transition), but the effect body short-circuits on the
    // initial render.
    const deps = match[1];
    expect(deps).toMatch(/activeId/);
  });
});
