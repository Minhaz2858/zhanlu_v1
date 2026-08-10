/**
 * Cross-store content-fingerprint dedup helper for Chat.jsx.
 *
 * The chat surface merges messages from two parallel stores whose ids
 * are disjoint:
 *
 *   1. `agent_conversations.messages` (JSON column, backend writes
 *      `created_date = datetime.utcnow().isoformat()` at
 *      agents.py:4971-4979 / 5327-5341).
 *
 *   2. `chat_messages` table (DB row, frontend's `ChatMessage.create`
 *      plus backend's `Message()` row at agents.py:5298-5309 — the DB
 *      default `created_date` is set to a different timestamp).
 *
 * Because the timestamps differ across stores, the fingerprint must
 * NOT use `created_date`. Only `role` and `content` are stable, and
 * the 4000-char content cap matches the backend's `assistant_msg`
 * slice at agents.py:5330. First occurrence wins so callers can
 * control priority by ordering (e.g. server-confirmed state first).
 */
export function dedupeMessagesByFingerprint(msgs) {
  if (!Array.isArray(msgs) || msgs.length === 0) return [];
  const seenFingerprints = new Set();
  const result = [];
  for (const m of msgs) {
    if (!m) continue;
    const fp = `${m.role || ''}::${(m.content || '').slice(0, 4000)}`;
    if (seenFingerprints.has(fp)) continue;
    seenFingerprints.add(fp);
    result.push(m);
  }
  return result;
}
