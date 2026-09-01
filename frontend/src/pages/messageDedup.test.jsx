/**
 * Regression: cross-store content-fingerprint dedup
 * --------------------------------------------------
 * Chat.jsx renders the active conversation by merging two parallel
 * stores whose ids are disjoint:
 *
 *   1. The `agent_conversations.messages` JSON column — populated by
 *      the backend at agents.py:4971-4979 (user msg) and 5327-5341
 *      (assistant msg) with `created_date = datetime.utcnow().isoformat()`.
 *
 *   2. The `chat_messages` table — populated by the frontend via
 *      `base44.entities.ChatMessage.create()` and the backend's
 *      `Message()` ORM row at agents.py:5298-5309. The DB default
 *      `created_date` is set to a different timestamp than the JSON
 *      column entry, so the two stores NEVER agree on
 *      `created_date` for the same logical message.
 *
 * The conv-rehydration useEffect (`setMessages(conv.messages)` in
 * Chat.jsx) and the v3-stream `loadMessages` polling both surface
 * these cross-store copies. A naive id-based merge keeps both.
 *
 * The fix: a `dedupeMessagesByFingerprint` helper that collapses
 * entries by `role::content[:4000]` — NOT including `created_date`,
 * because that field is intentionally different across stores. First
 * occurrence wins (preserves the order callers passed in).
 */
import { describe, it, expect } from 'vitest';
import { dedupeMessagesByFingerprint } from './messageDedup';

describe('dedupeMessagesByFingerprint', () => {
  it('is exported from messageDedup', () => {
    expect(typeof dedupeMessagesByFingerprint, 'helper must be a function').toBe('function');
  });

  it('returns an empty array for empty input', () => {
    expect(dedupeMessagesByFingerprint([])).toEqual([]);
    expect(dedupeMessagesByFingerprint(null)).toEqual([]);
    expect(dedupeMessagesByFingerprint(undefined)).toEqual([]);
  });

  it('preserves a single message', () => {
    const m = { id: 'a', role: 'user', content: 'hello' };
    expect(dedupeMessagesByFingerprint([m])).toEqual([m]);
  });

  it('passes through unique messages unchanged', () => {
    const msgs = [
      { id: 'a', role: 'user', content: 'hello' },
      { id: 'b', role: 'assistant', content: 'hi there' },
      { id: 'c', role: 'user', content: 'how are you?' },
    ];
    const out = dedupeMessagesByFingerprint(msgs);
    expect(out).toHaveLength(3);
    expect(out.map((m) => m.id)).toEqual(['a', 'b', 'c']);
  });

  it('collapses cross-store copies with same role+content but DIFFERENT created_date', () => {
    // This is the EXACT scenario that produced the bug:
    //   - The conv.messages JSON-column entry was written at 10:00:00.123456
    //     (backend's `datetime.utcnow().isoformat()`).
    //   - The chat_messages table row was written at 10:00:00.789012
    //     (DB default when frontend's `ChatMessage.create` reached the DB).
    // Same role, same content, DIFFERENT created_date → ids are different
    // and the id-based merge keeps BOTH. The fingerprint must ignore
    // created_date to collapse them.
    const convCopy = {
      id: 'uuid-from-conv-messages',
      role: 'user',
      content: 'hello',
      created_date: '2026-08-06T10:00:00.123456',
    };
    const chatCopy = {
      id: 'uuid-from-chat-messages-table',
      role: 'user',
      content: 'hello',
      created_date: '2026-08-06T10:00:00.789012',
    };
    const out = dedupeMessagesByFingerprint([convCopy, chatCopy]);
    expect(out, 'cross-store copies with same role+content must collapse to one').toHaveLength(1);
    // First occurrence wins — caller controls priority by ordering.
    expect(out[0].id).toBe('uuid-from-conv-messages');
  });

  it('collapses cross-store copies of an assistant response', () => {
    // The same dual-store pattern applies to assistant messages: one
    // copy in the chat_messages Message() row (agents.py:5298-5309),
    // one in conv.messages (agents.py:5327-5341).
    const chatCopy = {
      id: 'msg-table-uuid',
      role: 'assistant',
      content: "Hello! I'm your assistant.",
      created_date: '2026-08-06T10:00:01.000000',
    };
    const convCopy = {
      id: 'conv-json-uuid',
      role: 'assistant',
      content: "Hello! I'm your assistant.",
      created_date: '2026-08-06T10:00:01.500000',
    };
    const out = dedupeMessagesByFingerprint([chatCopy, convCopy]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe('msg-table-uuid');
  });

  it('keeps two messages with the same role but DIFFERENT content', () => {
    const a = { id: '1', role: 'user', content: 'hello' };
    const b = { id: '2', role: 'user', content: 'goodbye' };
    const out = dedupeMessagesByFingerprint([a, b]);
    expect(out).toHaveLength(2);
  });

  it('keeps two messages with the same content but DIFFERENT role', () => {
    const user = { id: '1', role: 'user', content: 'yes' };
    const asst = { id: '2', role: 'assistant', content: 'yes' };
    const out = dedupeMessagesByFingerprint([user, asst]);
    expect(out).toHaveLength(2);
  });

  it('truncates content at 4000 chars when fingerprinting (matches backend cap)', () => {
    // Two 5000-char messages with the same first 4000 chars must collapse.
    const long1 = { id: '1', role: 'assistant', content: 'A'.repeat(5000) };
    const long2 = { id: '2', role: 'assistant', content: 'A'.repeat(5000) };
    const out = dedupeMessagesByFingerprint([long1, long2]);
    expect(out).toHaveLength(1);
  });

  it('treats undefined/empty content as the same fingerprint', () => {
    // Defensive: a message with `content: undefined` and a message
    // with `content: ''` should be considered the same (both have
    // nothing to show). The fingerprint must not crash on missing
    // content.
    const a = { id: '1', role: 'user' };
    const b = { id: '2', role: 'user', content: '' };
    const out = dedupeMessagesByFingerprint([a, b]);
    expect(out).toHaveLength(1);
  });

  it('does not mutate the input array', () => {
    const msgs = [
      { id: '1', role: 'user', content: 'hello' },
      { id: '2', role: 'user', content: 'hello' },
    ];
    const before = JSON.stringify(msgs);
    dedupeMessagesByFingerprint(msgs);
    expect(JSON.stringify(msgs)).toBe(before);
  });

  it('preserves the first occurrence when 3+ copies exist', () => {
    const a = { id: 'first', role: 'user', content: 'hello', created_date: '2026-08-06T10:00:00.1' };
    const b = { id: 'second', role: 'user', content: 'hello', created_date: '2026-08-06T10:00:00.2' };
    const c = { id: 'third', role: 'user', content: 'hello', created_date: '2026-08-06T10:00:00.3' };
    const out = dedupeMessagesByFingerprint([a, b, c]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe('first');
  });

  // -------- Second-Run-Now dedup regression (2026-08-12) --------
  // The "Run Automation Task" synthetic user card has identical content
  // on every click (5-bullet summary: Name / Type / Schedule / Output
  // format / Project / Description). Without execution_id in the
  // fingerprint, the second click's bubble was being collapsed and the
  // user saw the prompt disappear.

  it('keeps two Run Now user bubbles with identical content but different phase.execution_id', () => {
    const run1 = {
      id: 'm1',
      role: 'user',
      content: 'Run Automation Task：\n- Name：Daily Sync\n- Type：Data Sync\n- Schedule：Daily 08:00\n- Output format：HTML report\n- Project：ACME',
      phase: { execution_id: 'exe-A', kind: 'run_request' },
    };
    const run2 = {
      id: 'm2',
      role: 'user',
      content: 'Run Automation Task：\n- Name：Daily Sync\n- Type：Data Sync\n- Schedule：Daily 08:00\n- Output format：HTML report\n- Project：ACME',
      phase: { execution_id: 'exe-B', kind: 'run_request' },
    };
    const out = dedupeMessagesByFingerprint([run1, run2]);
    expect(out).toHaveLength(2);
    expect(out.map((m) => m.phase.execution_id)).toEqual(['exe-A', 'exe-B']);
  });

  it('keeps the empty assistant bubble of each execution distinct', () => {
    // `_post_run_request_marker` writes an empty assistant bubble per
    // execution to receive the activity_steps mirror. They start with
    // empty content, so without execution_id they'd be one row total.
    const a1 = { id: 'a1', role: 'assistant', content: '', phase: { execution_id: 'exe-A' } };
    const a2 = { id: 'a2', role: 'assistant', content: '', phase: { execution_id: 'exe-B' } };
    const out = dedupeMessagesByFingerprint([a1, a2]);
    expect(out).toHaveLength(2);
  });

  it('still dedupes a chat_messages row and an agent_conversations mirror of the same execution', () => {
    // Cross-store mirror: same role+content+execution_id → one survives.
    const mirror1 = {
      id: 'cm-1',
      role: 'user',
      content: 'Run Automation Task：\n- Name：X',
      phase: { execution_id: 'exe-A' },
    };
    const mirror2 = {
      id: 'ac-1',
      role: 'user',
      content: 'Run Automation Task：\n- Name：X',
      phase: { execution_id: 'exe-A' },
    };
    const out = dedupeMessagesByFingerprint([mirror1, mirror2]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe('cm-1');
  });

  it('treats missing/non-object phase as no execution_id (back-compat)', () => {
    // A regular chat message has no phase at all → identical to the
    // old fingerprint. A phase that is a string (defensive) → also
    // treated as no execution_id.
    const a = { id: 'a', role: 'user', content: 'hi' };
    const b = { id: 'b', role: 'user', content: 'hi' };
    const c = { id: 'c', role: 'user', content: 'hi', phase: 'oops' };
    const d = { id: 'd', role: 'user', content: 'hi', phase: null };
    const out = dedupeMessagesByFingerprint([a, b, c, d]);
    expect(out).toHaveLength(1);
  });

  // -------- Repeated-question regression (2026-08-22) --------
  // User re-asks the SAME question after a failed turn (e.g. "Could you
  // try again with a more specific request?"). The second user prompt has
  // no phase.execution_id, so its fingerprint is identical to the first.
  // A GLOBAL seen-set drops it — the second prompt silently disappears
  // from the chat. Only CONSECUTIVE identical fingerprints are
  // cross-store copies of the SAME logical message (written in the same
  // turn, ms apart); a genuine repeat is separated by the assistant
  // response and must be kept.

  it('keeps identical user prompts separated by an assistant response (repeated question)', () => {
    const user1 = { id: 'u1', role: 'user', content: 'Show me the latest intelligence events' };
    const asst = {
      id: 'a1',
      role: 'assistant',
      content:
        'I gathered some information but had trouble putting it all together. Could you try again with a more specific request?',
    };
    const user2 = { id: 'u2', role: 'user', content: 'Show me the latest intelligence events' };
    const out = dedupeMessagesByFingerprint([user1, asst, user2]);
    expect(out).toHaveLength(3);
    expect(out.map((m) => m.id)).toEqual(['u1', 'a1', 'u2']);
  });

  it('collapses adjacent cross-store copies but keeps repeated turns apart', () => {
    // Chat.jsx merge order: [chat_messages copy, agent_conversations
    // copy] are ADJACENT per turn (same logical message, timestamps ms
    // apart); the repeated question is a NEW turn separated by the
    // assistant response.
    const user1Chat = { id: 'cm-1', role: 'user', content: 'hello', created_date: '2026-08-06T10:00:00.1' };
    const user1Conv = { id: 'ac-1', role: 'user', content: 'hello', created_date: '2026-08-06T10:00:00.2' };
    const asst = { id: 'a1', role: 'assistant', content: 'hi there', created_date: '2026-08-06T10:00:01.0' };
    const user2Chat = { id: 'cm-2', role: 'user', content: 'hello', created_date: '2026-08-06T10:00:02.1' };
    const user2Conv = { id: 'ac-2', role: 'user', content: 'hello', created_date: '2026-08-06T10:00:02.2' };
    const out = dedupeMessagesByFingerprint([user1Chat, user1Conv, asst, user2Chat, user2Conv]);
    // One bubble per turn — cross-store copies collapse within a turn,
    // but the repeated question in the second turn must survive.
    expect(out.map((m) => m.id)).toEqual(['cm-1', 'a1', 'cm-2']);
  });
});
