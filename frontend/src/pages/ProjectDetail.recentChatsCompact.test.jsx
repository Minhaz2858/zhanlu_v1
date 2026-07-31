/**
 * Regression: the Project Detail "Recent Chats" list must be a
 * compact, one-line-per-row layout, and each row must be a button
 * that calls onOpenConv with the conversation when clicked.
 *
 * Why this exists
 * ---------------
 * A user reported that the Recent Chats list was visually heavy
 * (each row took ~3 lines: avatar + title + agent badge + status
 * badge + last-message preview + time) and that they had no way
 * to re-open an old session. The fix collapses each row to a
 * single line (avatar + title + time) and turns the row into a
 * <button> that calls onOpenConv(conv) on click. This test pins
 * both halves of the fix so a future refactor can't quietly undo
 * them.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './ProjectDetail.jsx'), 'utf8');

describe('ProjectDetail.jsx Recent Chats list', () => {
  it('renders each conversation as a <button>, not a <div>', () => {
    // Locate the ConversationsSection function and assert the row
    // element is a <button>. Clicking the row should fire
    // onOpenConv(conv).
    const section = SOURCE.match(
      /function ConversationsSection[\s\S]*?\n\}/,
    );
    expect(section, 'ConversationsSection not found').not.toBeNull();
    // The map() inside should use a <button> as its outer element.
    const mapBlock = section[0].match(
      /conversations\.map\(\(c\)\s*=>[\s\S]*?\}\)\}/,
    );
    expect(mapBlock, 'conversations.map block not found').not.toBeNull();
    expect(mapBlock[0]).toMatch(/<button/);
    expect(mapBlock[0]).toMatch(/onClick=\{\(\)\s*=>\s*onOpenConv/);
  });

  it('does not render the agent name as a Badge inside the row (one-line layout)', () => {
    // The compact layout is one line — title + time only. There
    // must NOT be a Badge for agent_name or status inside the
    // ConversationsSection row. (The agent is already chosen by
    // the user when they click "Chat with this agent" on the
    // project page; the list only needs to identify the session.)
    const section = SOURCE.match(
      /function ConversationsSection[\s\S]*?\n\}/,
    );
    expect(section, 'ConversationsSection not found').not.toBeNull();
    expect(section[0]).not.toMatch(/<Badge[\s\S]*?\{c\.agent_name/);
    expect(section[0]).not.toMatch(/<Badge[\s\S]*?\{c\.status/);
  });

  it('does not render the last-message preview inside the row', () => {
    // The previous design showed a ``{lastMsg.content.slice(0, 80)}``
    // preview on the second line. The compact layout drops it.
    const section = SOURCE.match(
      /function ConversationsSection[\s\S]*?\n\}/,
    );
    expect(section, 'ConversationsSection not found').not.toBeNull();
    expect(section[0]).not.toMatch(/lastMsg/);
  });

  it('wraps the title in a single-line truncate so long titles do not wrap', () => {
    // The row must keep titles on one line. We assert the presence
    // of a ``truncate`` class on the title's parent (the canonical
    // Tailwind utility for single-line text with ellipsis).
    const section = SOURCE.match(
      /function ConversationsSection[\s\S]*?\n\}/,
    );
    expect(section, 'ConversationsSection not found').not.toBeNull();
    expect(section[0]).toMatch(/<span className="[^"]*truncate[^"]*"[^>]*>\s*\{title\}/);
  });

  it('wires the onOpenConv callback from the parent and navigates to /?conv=...', () => {
    // The parent component must pass an onOpenConv callback that
    // navigates to ``/?conv={id}`` (and ``&agentName=...`` when
    // available). This is the contract the Chat page relies on
    // to rehydrate the old session.
    expect(SOURCE).toMatch(/onOpenConv=\{[\s\S]*?navigate\(`\/\?/);
    expect(SOURCE).toMatch(/qs\.set\('conv',\s*conv\.id\)/);
    expect(SOURCE).toMatch(/qs\.set\('agentName',\s*conv\.agent_name\)/);
  });
});
