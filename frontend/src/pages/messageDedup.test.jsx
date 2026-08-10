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
      content: "Hello! I'm your EDIA assistant.",
      created_date: '2026-08-06T10:00:01.000000',
    };
    const convCopy = {
      id: 'conv-json-uuid',
      role: 'assistant',
      content: "Hello! I'm your EDIA assistant.",
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
});
