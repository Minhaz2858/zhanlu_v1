/**
 * Regression test: the message-list wrapper in Chat.jsx must have a
 * ``space-y-*`` class so consecutive user/agent message turns have
 * a vertical gap.
 *
 * Why this exists
 * ---------------
 * A user reported that the UI packed consecutive turns flush with
 * each other — no gap between the user's bubble and the assistant's
 * card. The cause: the messages were mapped inside a bare ``<div>``
 * with no spacing utility, so each MessageBubble's root had nothing
 * separating it from the next one. The fix is ``space-y-6`` on the
 * wrapper div (line ~1167 of Chat.jsx). This test guards that fix
 * so a future refactor can't quietly drop it.
 *
 * The test does a substring check on the file content rather than
 * rendering Chat.jsx (which is heavy and slow). That's the same
 * approach used by tests/test_steer_wiring.test.js and is robust
 * to renames of the wrapper element.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

// Load the Chat.jsx source as a string. We deliberately use the
// source (not the rendered component) so this test stays a
// pure structural check — fast, deterministic, and robust to
// refactors that rename the wrapper element or move the
// messages.map.
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const CHAT_JSX = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx message list spacing', () => {
  it('wraps the messages.map output in a div with a space-y-* utility', () => {
    // The exact pattern we expect:
    //   <div className="space-y-6">
    //   ...
    //   {pendingDraft && ( ... )}
    //   {(() => {
    //     const visible = messages.filter((m) => ...);
    //     return visible.map((m) => { ... });
    //   })()}
    //   </div>
    // We allow any `space-y-N` class so the design can tighten or
    // loosen the gap without breaking this test.
    // NOTE (2026-08-31): the visible-list build moved from an inline
    // `messages.filter(...).map(...)` into an IIFE render block that
    // hoists `visible` (so the last-assistant Regenerate check can
    // reuse it). The spacing contract is unchanged.
    const messageMap = CHAT_JSX.match(
      /<div\b[^>]*>\s*\{pendingDraft[\s\S]*?\{\(\(\) => \{[\s\S]*?messages\.filter\(\(m\)\s*=>\s*[\s\S]{0,400}?\)\s*;[\s\S]*?visible\.map\([\s\S]*?}\)\(\)\}\s*<\/div>/,
    );
    expect(messageMap, 'Could not locate the messages.map wrapper div').not.toBeNull();
    const wrapper = messageMap[0];
    expect(wrapper).toMatch(/className="[^"]*space-y-\d/);
  });

  it('does not place messages.map directly under the loading skeleton div (regression guard)', () => {
    // The original bug was that the messages were inside a plain
    // <div> with no spacing. If a future refactor accidentally
    // moves the map directly under a div that has no `space-y-`
    // class, this test will catch it.
    const messageMap = CHAT_JSX.match(
      /\{\(\(\) => \{[\s\S]*?messages\.filter\(\(m\)\s*=>\s*[\s\S]{0,400}?\)\s*;[\s\S]*?visible\.map\([\s\S]*?}\)\(\)\}/,
    );
    expect(messageMap, 'Could not locate messages.map').not.toBeNull();
    // Walk backwards from the map and find the nearest opening div
    // with a className. It must have a space-y-* class.
    const startIdx = messageMap.index;
    const before = CHAT_JSX.slice(0, startIdx);
    const lastClassDiv = before.lastIndexOf('<div');
    const lastClassDivEnd = before.indexOf('>', lastClassDiv);
    const openTag = before.slice(lastClassDiv, lastClassDivEnd + 1);
    expect(openTag, 'messages.map is not wrapped in a div with className').toMatch(
      /className="[^"]*space-y-\d/,
    );
  });
});
