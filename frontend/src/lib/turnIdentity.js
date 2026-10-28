/**
 * Turn-identity transport — Stage 2B Commit 4A (2026-10-27).
 *
 * WHAT THIS DOES
 * --------------
 * Carries two correlation fields on the v3 stream request so the backend can
 * eventually address the EXACT assistant row a turn owns:
 *
 *   assistant_message_id : the client-created chat_messages row id
 *                          (returned by ChatMessage.create) — the only id that
 *                          corresponds to a real row. Backend-generated urls in
 *                          the stream generator do NOT identify that row.
 *   request_id           : a stable id for ONE logical send, reused across
 *                          transport/auth retries, and freshly minted for a new
 *                          turn or an explicit regeneration.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO (scope of Commit 4A)
 * -------------------------------------------------------
 *   * no version echo, no 409 handling, no entity-route enforcement
 *   * no change to persistence ownership, finalization or concurrency
 *   * no schema change
 *
 * So this commit establishes TRANSPORT ONLY. It does not fix — and must not be
 * read as fixing — the answer-persistence race.
 *
 * WHY request_id IS STABLE ACROSS RETRIES
 * ---------------------------------------
 * `streamAgentResponse` serializes the body exactly once (`JSON.stringify`)
 * and `authFetch` replays that same serialized string on its single 401
 * retry. Because both fields live INSIDE the body, a retry necessarily
 * carries the identical request_id + assistant_message_id. No retry bookkeeping
 * is needed at the call site.
 */

/** Regex the backend is expected to accept for both transport fields. */
export const TURN_ID_RE = /^[0-9A-Za-z_-]{8,64}$/;

/**
 * Generate a random id (hex, 32 chars).
 *
 * Uses crypto.randomUUID when available (browsers, happy-dom, node >= 19) and
 * falls back to a manual hex generator so tests and older runtimes still work.
 * Never throws.
 */
export function generateTurnId() {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID().replace(/-/g, '');
    }
  } catch {
    /* fall through */
  }
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
      const buf = new Uint8Array(16);
      crypto.getRandomValues(buf);
      return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
    }
  } catch {
    /* fall through */
  }
  // Last resort (deterministic environments): time + random.
  let out = '';
  for (let i = 0; i < 4; i++) {
    out += Math.floor(Math.random() * 0xffffffff).toString(16).padStart(8, '0');
  }
  return out;
}

/**
 * True when `value` is a plausible transport id for the backend.
 * Deliberately permissive about length (>= 8) and character set; the backend
 * performs its own validation.
 */
export function isValidTurnId(value) {
  return typeof value === 'string' && TURN_ID_RE.test(value);
}

/**
 * Build the two transport fields for one logical stream request.
 *
 * @param {{assistantMessageId?: string, requestId?: string}} opts
 * @returns {{assistant_message_id?: string, request_id?: string}}
 *   Only validated values are included — an absent/malformed id is OMITTED
 *   rather than sent as junk, so the backend treats it exactly as a legacy
 *   request (no identity), never as a wrong identity.
 */
export function buildTurnIdentityFields({ assistantMessageId, requestId } = {}) {
  const fields = {};
  if (isValidTurnId(assistantMessageId)) {
    fields.assistant_message_id = assistantMessageId;
  }
  if (isValidTurnId(requestId)) {
    fields.request_id = requestId;
  }
  return fields;
}

/**
 * Mint the identity for a NEW logical turn.
 *
 * A new user message and an explicit regeneration are both new logical turns:
 * each gets a fresh request_id. Regeneration additionally KEEPS the target
 * assistant message id, because it rewrites that same row.
 *
 * @param {{assistantMessageId?: string}} opts
 * @returns {{assistant_message_id?: string, request_id: string}}
 */
export function newTurnIdentity({ assistantMessageId } = {}) {
  return buildTurnIdentityFields({
    assistantMessageId,
    requestId: generateTurnId(),
  });
}

/**
 * Reuse the SAME identity for a retry of an in-flight logical request.
 *
 * This is the contract Commit 4A must preserve: retrying must not mint a new
 * request_id, or the backend would see a second, newer turn.
 *
 * @param {{assistant_message_id?: string, request_id?: string} | null} previous
 * @returns {{assistant_message_id?: string, request_id?: string}}
 */
export function reuseTurnIdentity(previous) {
  return buildTurnIdentityFields({
    assistantMessageId: previous?.assistant_message_id,
    requestId: previous?.request_id,
  });
}
