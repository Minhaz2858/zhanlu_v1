/**
 * Regression: Chat.jsx must handle the ``?conv={id}`` URL param by
 * fetching the AgentConversation record and rehydrating the
 * message list and the active agent.
 *
 * Why this exists
 * ---------------
 * The Project Detail "Recent Chats" list navigates to
 * ``/?conv={id}&agentName=...`` when a row is clicked. The Chat
 * page is responsible for:
 *
 *   1. Reading the ``conv`` query param.
 *   2. Fetching the AgentConversation record.
 *   3. Setting ``messages`` to ``conv.messages`` so the chat pane
 *      shows the old session.
 *   4. Resolving the active agent by name so the right agent is
 *      loaded for follow-up turns.
 *
 * This test pins the source contract so a future refactor that
 * drops the conv-rehydration path is caught here.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE = readFileSync(resolve(__dirname, './Chat.jsx'), 'utf8');

describe('Chat.jsx ?conv= rehydration', () => {
  it('reads the conv URL param', () => {
    expect(SOURCE).toMatch(/params\.get\(['"]conv['"]\)/);
  });

  it('fetches the AgentConversation record by id', () => {
    // The handler must call base44.entities.AgentConversation.get(convId)
    // (or equivalent) and chain a .then() to rehydrate state.
    const block = SOURCE.match(
      /const convId\s*=\s*params\.get\(['"]conv['"]\);[\s\S]{0,800}/,
    );
    expect(block, 'convId block not found').not.toBeNull();
    expect(block[0]).toMatch(/base44\.entities\.AgentConversation\.get/);
  });

  it('hydrates messages from conv.messages', () => {
    // The .then() must call setMessages with conv.messages. This
    // is the only way the chat pane can render the old session.
    // 2026-08-06: the hydration was hardened with a functional updater
    // (session guard) + content-fingerprint dedup — conv.messages and
    // chat_messages are parallel stores with disjoint id spaces, so the
    // direct set call is wrapped in dedupeMessagesByFingerprint().
    const block = SOURCE.match(
      /AgentConversation\.get\([\s\S]{0,1200}\)\.then\([\s\S]*?\}\)\.catch/,
    );
    expect(block, 'AgentConversation.get().then() block not found').not.toBeNull();
    expect(block[0]).toMatch(/setMessages\(\(prev\)\s*=>\s*\{[\s\S]*?dedupeMessagesByFingerprint\(\s*Array\.isArray\(conv\.messages\)\s*\?\s*conv\.messages\s*:\s*\[\]\s*,?\s*\)/);
  });

  it('resolves the active agent by name from the conv record or ?agentName= param', () => {
    // The handler accepts a ?agentName= override (avoids an extra
    // round-trip from the caller) and falls back to conv.agent_name.
    const block = SOURCE.match(
      /AgentConversation\.get\([\s\S]{0,1500}\)\.then\([\s\S]*?\}\)\.catch/,
    );
    expect(block, 'AgentConversation.get().then() block not found').not.toBeNull();
    expect(block[0]).toMatch(/convAgentName\s*\|\|\s*conv\.agent_name/);
    expect(block[0]).toMatch(/base44\.entities\.AgentApp\.list\(\)/);
    expect(block[0]).toMatch(/setActiveAgent/);
  });
});
