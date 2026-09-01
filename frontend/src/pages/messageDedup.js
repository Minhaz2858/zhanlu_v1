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
 *
 * COLLAPSE IS CONSECUTIVE-ONLY. Only messages that are ADJACENT in the
 * input (same fingerprint as the immediately preceding KEPT message)
 * are dropped. Cross-store copies of the same logical message are
 * written in the same turn (timestamps ms apart), so after the caller
 * sorts by `created_date` they sit side by side and still collapse.
 * A genuine repeat — the user re-asking the SAME question after a
 * failed turn — is separated by the assistant response, so both user
 * bubbles must survive. A global seen-set would wrongly collapse the
 * second identical prompt (plain user messages carry no
 * `phase.execution_id`, so their fingerprints are identical), which is
 * exactly the reported "user input missing from the agent chat" bug.
 *
 * Execution-scoped messages: when a message carries
 * `phase.execution_id` (set by automation_executor for the synthetic
 * "Run Automation Task" user card + the empty assistant bubble that
 * mirrors activity steps), include it in the fingerprint. Two runs of
 * the same automation task produce structurally identical
 * user-card/assistant-bubble content, so without this they would be
 * collapsed into a single row and the second Run Now would appear to
 * silently drop its prompt. The same fingerprint still matches a
 * chat_messages row and an agent_conversations mirror of the same
 * execution (both carry the same execution_id), so the cross-store
 * dedup contract is preserved.
 */
export function dedupeMessagesByFingerprint(msgs) {
  if (!Array.isArray(msgs) || msgs.length === 0) return [];
  const result = [];
  let lastKeptFp = null;
  for (const m of msgs) {
    if (!m) continue;
    const phase = m.phase && typeof m.phase === 'object' ? m.phase : null;
    const execId = phase && phase.execution_id ? String(phase.execution_id) : '';
    const fp = `${m.role || ''}::${(m.content || '').slice(0, 4000)}::${execId}`;
    // Drop ONLY consecutive identical fingerprints (cross-store copies).
    // Repeated questions are separated by other messages → kept.
    if (fp === lastKeptFp) continue;
    lastKeptFp = fp;
    result.push(m);
  }
  return result;
}
