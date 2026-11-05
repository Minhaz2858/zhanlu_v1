/**
 * Answer-lifecycle reducer — Stage 1 (2026-10-27).
 *
 * WHY THIS EXISTS
 * ---------------
 * `Chat.jsx`'s v3 stream handler used to overwrite the visible answer in two
 * places with no guard:
 *
 *   1. `content_replace` assigned `fullContent = evt.content` unconditionally,
 *      so an empty or partial replacement erased a complete answer that had
 *      already streamed.
 *   2. the final `ChatMessage.update` wrote `content: fullContent`
 *      unconditionally, so a stream that ended without a `done` frame (client
 *      watchdog, reload, server close) persisted empty/stale text over the
 *      row that held the real answer.
 *
 * This module holds ONLY the decision logic so it can be unit-tested as
 * behaviour. It does not touch React, the network, or the DOM.
 *
 * INVARIANTS ENFORCED
 * -------------------
 * I1  Empty / whitespace-only content NEVER replaces non-empty content
 *     (neither in the bubble nor in the persisted row).
 * I2  A replacement is accepted when it is a *completed, non-empty* value —
 *     regardless of length or script. There is deliberately NO minimum length
 *     and NO punctuation/English requirement: `Done.`, `12` and
 *     `好的，已按客户拆分。` are all valid answers.
 * I3  `done.content` is authoritative when non-empty (server truth wins),
 *     preserving the 2026-10-19 duplicate-answer contract.
 * I4  When nothing valid was ever produced, the outcome is an explicit
 *     placeholder/failure — never a silent empty "success".
 * I5  Content from a superseded generation is ignored (stale-write guard).
 * I6  (O3, 2026-10-27; corrected same day) The BACKEND owns the persisted
 *     content — and the client does not re-write it — ONLY when all three
 *     hold: a `done` arrived, its content is non-empty, and it reported
 *     `answer_persisted === true`. That closes the race where a stale client
 *     PUT overwrites the backend's committed answer WITHOUT reintroducing the
 *     inverse bug (backend skipped its patch-if-empty write, so nobody
 *     persisted). In every other case the client REMAINS the writer.
 *
 * NOT IMPLEMENTED HERE (by scope): backend persistence, assistant_message_id,
 * turn_id payloads, recovery floor. Those belong to later approved stages.
 */

/** True when `value` is a string with non-whitespace content. */
export function hasContent(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

/**
 * Decide the bubble/persisted content after a `content_replace` event.
 *
 * A replacement may legitimately be SHORTER than the previous answer, and may
 * be in any language — it is accepted purely on "completed and non-empty".
 * An empty/whitespace replacement is rejected so a pending regeneration can
 * never blank a valid answer while it is still being produced.
 *
 * @param {string} previous - currently displayed/accumulated content
 * @param {string} next     - the replacement payload
 * @returns {{content: string, accepted: boolean, reason: string}}
 */
export function applyContentReplace(previous, next) {
  if (!hasContent(next)) {
    return {
      content: previous,
      accepted: false,
      reason: 'empty_replacement',
    };
  }
  return { content: next, accepted: true, reason: 'replacement' };
}

/**
 * Decide the content to persist for a finished turn.
 *
 * Order of authority:
 *   1. `doneContent` (server-authoritative) when non-empty;
 *   2. otherwise the locally accumulated `streamedContent` when non-empty;
 *   3. otherwise an explicit failure placeholder (never "").
 *
 * @param {{doneContent?: string, streamedContent?: string,
 *          placeholder?: string, hadTerminal?: boolean}} opts
 * @returns {{content: string, source: 'done'|'streamed'|'placeholder',
 *            ok: boolean}}
 */
export function resolveFinalContent({
  doneContent = '',
  streamedContent = '',
  placeholder = '',
  hadTerminal = false,
} = {}) {
  if (hasContent(doneContent)) {
    return { content: doneContent, source: 'done', ok: true };
  }
  if (hasContent(streamedContent)) {
    // A stream that ended without a terminal `done` still delivered real
    // text — keep it rather than discarding the user's answer.
    return { content: streamedContent, source: 'streamed', ok: hadTerminal };
  }
  return { content: placeholder, source: 'placeholder', ok: false };
}

/**
 * Decide whether the final `ChatMessage.update` may write `content`.
 *
 * Guards the destructive cases:
 *   * nothing valid to write (empty/whitespace) → do NOT write content;
 *   * the turn was superseded by a newer generation → do NOT write content;
 *   * (O3, 2026-10-27) the BACKEND owns the content for this turn → do NOT
 *     write content. See `backendOwnsContent` below.
 *
 * Metadata (tool_calls / artifacts / sources) may still be written when the
 * turn owns the row — this returns a separate flag so callers keep that
 * behaviour unchanged.
 *
 * @param {{content?: string, isCurrentGeneration?: boolean,
 *          backendOwnsContent?: boolean}} opts
 * @returns {{writeContent: boolean, reason: string}}
 */
export function shouldPersistContent({
  content = '',
  isCurrentGeneration = true,
  backendOwnsContent = false,
} = {}) {
  if (!hasContent(content)) {
    return { writeContent: false, reason: 'empty_content' };
  }
  if (!isCurrentGeneration) {
    return { writeContent: false, reason: 'stale_generation' };
  }
  // O3: a non-empty `done` means the backend finalised and persisted this
  // answer server-side. A second client-side write of `content` is redundant
  // at best and destructive at worst: when the client's accumulated text is
  // STALE (stream ended without `done`, watchdog, reload) its blind PUT lands
  // after the backend's commit and overwrites the server's answer with an
  // older string. Relinquishing content ownership closes that race.
  if (backendOwnsContent) {
    return { writeContent: false, reason: 'backend_owns_content' };
  }
  return { writeContent: true, reason: 'ok' };
}

/**
 * O3 — does the backend own this turn's persisted `content`?
 *
 * TRUE only when ALL THREE hold:
 *   1. a terminal `done` frame arrived;
 *   2. that frame carried non-empty content;
 *   3. the frame reported `answer_persisted === true` — i.e. the backend
 *      CONFIRMED it committed this turn's canonical answer to the row.
 *
 * Condition 3 is essential (O3 correction, 2026-10-27). The backend's writer
 * is *patch-if-empty*: when the row already holds a different non-empty
 * answer — the Regenerate case, or leftover partial/previous text — the
 * backend SKIPS the write. Relinquishing on conditions 1+2 alone therefore
 * left the UI showing the new answer while the database kept the old one.
 *
 * Anything other than an explicit `true` (absent, false, malformed, string
 * "true", non-boolean) keeps the legacy client-write behaviour, which is the
 * safe default: the client writes the canonical answer itself. Likewise, when
 * no `done` arrived (watchdog, tab close, server close) the backend may never
 * have persisted anything, so the client MUST remain the writer or the
 * answer would be lost on reload.
 *
 * @param {{hadTerminal?: boolean, doneContent?: string,
 *          answerPersisted?: unknown}} opts
 * @returns {boolean}
 */
export function backendOwnsContent({
  hadTerminal = false,
  doneContent = '',
  answerPersisted = false,
} = {}) {
  return (
    Boolean(hadTerminal) &&
    hasContent(doneContent) &&
    answerPersisted === true
  );
}

/**
 * True when a newer answer should be considered over an older one for the
 * same bubble (used to reject late frames from a superseded stream).
 *
 * @param {number} frameGen - generation that produced the frame
 * @param {number} currentGen - the current generation counter
 */
export function isCurrentGeneration(frameGen, currentGen) {
  return frameGen === currentGen;
}

/**
 * D5 (2026-11-01) — apply a streamed delta, honouring a pending server reset.
 *
 * On mid-stream generator retries the server emits `content_reset` /
 * `content_replace ""` to signal "I am re-streaming this answer from
 * scratch". Invariant I1 (never blank a valid answer) makes the reducer
 * REJECT the empty replacement — correct for the bubble, but the accumulator
 * must also NOT keep appending the fresh re-stream onto the stale pre-reset
 * text (that produced the transient doubled answer until `done` reconciled).
 *
 * This helper carries the reset as STATE: the visible content is preserved,
 * and the FIRST delta after the reset REPLACES the accumulator instead of
 * appending. A non-empty `content_replace` always clears the flag (it is an
 * authoritative full value).
 *
 * @param {string} previous - accumulated content so far
 * @param {string} delta    - the incoming delta text
 * @param {boolean} pendingReset - true right after a rejected empty replace
 *                                  or a `content_reset` frame
 * @returns {{content: string, pendingReset: boolean}}
 */
export function applyDelta(previous, delta, pendingReset = false) {
  const d = typeof delta === 'string' ? delta : '';
  if (pendingReset) {
    return { content: d, pendingReset: false };
  }
  return { content: previous + d, pendingReset: false };
}
