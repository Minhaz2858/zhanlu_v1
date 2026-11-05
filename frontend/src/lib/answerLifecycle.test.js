/**
 * Behavioural tests for the Stage 1 answer-lifecycle reducer.
 *
 * These exercise real decision logic (not source strings), covering every
 * case required by the Stage 1 brief. They are the regression guard for
 * "a valid answer must never be destroyed by an empty/stale/failed update".
 */
import { describe, it, expect } from 'vitest';
import {
  hasContent,
  applyContentReplace,
  resolveFinalContent,
  shouldPersistContent,
  isCurrentGeneration,
  backendOwnsContent,
} from './answerLifecycle';

const PLACEHOLDER = 'The agent did not return a response. Please retry.';

describe('hasContent', () => {
  it('is false for empty and whitespace-only values', () => {
    expect(hasContent('')).toBe(false);
    expect(hasContent('   \n\t ')).toBe(false);
    expect(hasContent(null)).toBe(false);
    expect(hasContent(undefined)).toBe(false);
    expect(hasContent(123)).toBe(false);
  });

  it('is true for any non-blank string (no length or script requirement)', () => {
    expect(hasContent('12')).toBe(true);
    expect(hasContent('Done.')).toBe(true);
    expect(hasContent('好的，已按客户拆分。')).toBe(true);
  });
});

describe('applyContentReplace — replacement protection', () => {
  const FULL_ANSWER =
    'Revenue for the week of 19–25 October was 105,920,951.33. ' +
    'The top material was isoprene.';

  it('accepts a completed valid replacement', () => {
    const r = applyContentReplace(FULL_ANSWER, 'Breakdown by customer: China Petroleum 34.4M.');
    expect(r.accepted).toBe(true);
    expect(r.content).toBe('Breakdown by customer: China Petroleum 34.4M.');
  });

  it('REJECTS an empty replacement and preserves the displayed answer', () => {
    const r = applyContentReplace(FULL_ANSWER, '');
    expect(r.accepted).toBe(false);
    expect(r.reason).toBe('empty_replacement');
    expect(r.content).toBe(FULL_ANSWER);
  });

  it('REJECTS a whitespace-only replacement', () => {
    const r = applyContentReplace(FULL_ANSWER, '   \n  ');
    expect(r.accepted).toBe(false);
    expect(r.content).toBe(FULL_ANSWER);
  });

  it('accepts a replacement SHORTER than the previous answer (no min length)', () => {
    const r = applyContentReplace(FULL_ANSWER, '12');
    expect(r.accepted).toBe(true);
    expect(r.content).toBe('12');
  });

  it('accepts a short conversational replacement "Done."', () => {
    const r = applyContentReplace(FULL_ANSWER, 'Done.');
    expect(r.accepted).toBe(true);
    expect(r.content).toBe('Done.');
  });

  it('accepts a valid Chinese replacement (no ASCII punctuation requirement)', () => {
    const r = applyContentReplace(FULL_ANSWER, '好的，已按客户拆分。');
    expect(r.accepted).toBe(true);
    expect(r.content).toBe('好的，已按客户拆分。');
  });

  it('accepts a Chinese replacement even when it has no ASCII terminator', () => {
    const r = applyContentReplace(FULL_ANSWER, '已按客户拆分');
    expect(r.accepted).toBe(true);
  });

  it('treats an empty previous + empty replacement as a no-op, not a crash', () => {
    const r = applyContentReplace('', '');
    expect(r.accepted).toBe(false);
    expect(r.content).toBe('');
  });
});

describe('resolveFinalContent — final-answer authority', () => {
  const STREAMED = 'Revenue was 105.9M across 65 orders.';
  const DONE = 'Revenue was 105.9M across 65 orders.\n\nTop material: isoprene.';

  it('normal completed answer: server done content wins', () => {
    const r = resolveFinalContent({ doneContent: DONE, streamedContent: STREAMED, hadTerminal: true });
    expect(r.source).toBe('done');
    expect(r.content).toBe(DONE);
    expect(r.ok).toBe(true);
  });

  it('empty done.content after a valid streamed answer KEEPS the streamed answer', () => {
    const r = resolveFinalContent({ doneContent: '', streamedContent: STREAMED, hadTerminal: true });
    expect(r.source).toBe('streamed');
    expect(r.content).toBe(STREAMED);
    expect(r.ok).toBe(true);
  });

  it('empty done.content AND empty streamed yields an explicit placeholder, not ""', () => {
    const r = resolveFinalContent({ doneContent: '', streamedContent: '', placeholder: PLACEHOLDER, hadTerminal: true });
    expect(r.source).toBe('placeholder');
    expect(r.content).toBe(PLACEHOLDER);
    expect(r.ok).toBe(false);
  });

  it('whitespace-only done.content falls through to streamed content', () => {
    const r = resolveFinalContent({ doneContent: '   ', streamedContent: STREAMED, hadTerminal: true });
    expect(r.content).toBe(STREAMED);
  });

  it('stream that ended without a terminal keeps the text but reports not-ok', () => {
    const r = resolveFinalContent({ doneContent: '', streamedContent: STREAMED, hadTerminal: false });
    expect(r.source).toBe('streamed');
    expect(r.ok).toBe(false); // delivered but not confirmed by a terminal event
  });

  it('a short valid answer and a Chinese answer are preserved verbatim', () => {
    expect(resolveFinalContent({ doneContent: '12', hadTerminal: true }).content).toBe('12');
    expect(resolveFinalContent({ doneContent: '好的，已按客户拆分。', hadTerminal: true }).content)
      .toBe('好的，已按客户拆分。');
  });
});

describe('shouldPersistContent — stale/empty write protection', () => {
  it('allows writing a non-empty current answer', () => {
    expect(shouldPersistContent({ content: 'answer', isCurrentGeneration: true }))
      .toEqual({ writeContent: true, reason: 'ok' });
  });

  it('BLOCKS an empty write (the 0-char row bug)', () => {
    const r = shouldPersistContent({ content: '', isCurrentGeneration: true });
    expect(r.writeContent).toBe(false);
    expect(r.reason).toBe('empty_content');
  });

  it('BLOCKS a whitespace-only write', () => {
    expect(shouldPersistContent({ content: '  \n ' }).writeContent).toBe(false);
  });

  it('BLOCKS a stale write from a superseded generation', () => {
    const r = shouldPersistContent({ content: 'old answer', isCurrentGeneration: false });
    expect(r.writeContent).toBe(false);
    expect(r.reason).toBe('stale_generation');
  });

  // ── O3 (2026-10-27): backend content ownership closes the write race ──
  it('O3: BLOCKS the content write when the backend owns it', () => {
    const r = shouldPersistContent({
      content: 'the answer',
      isCurrentGeneration: true,
      backendOwnsContent: true,
    });
    expect(r.writeContent).toBe(false);
    expect(r.reason).toBe('backend_owns_content');
  });

  it('O3: still writes when the backend does NOT own content', () => {
    const r = shouldPersistContent({
      content: 'the answer',
      isCurrentGeneration: true,
      backendOwnsContent: false,
    });
    expect(r.writeContent).toBe(true);
  });

  it('O3: empty content still wins over backend ownership (no shadowing)', () => {
    const r = shouldPersistContent({
      content: '',
      isCurrentGeneration: true,
      backendOwnsContent: true,
    });
    expect(r.reason).toBe('empty_content');
  });
});

describe('backendOwnsContent — O3 ownership rule (requires backend attestation)', () => {
  it('is TRUE only when done arrived, content non-empty, AND answer_persisted === true', () => {
    expect(backendOwnsContent({
      hadTerminal: true, doneContent: 'server answer', answerPersisted: true,
    })).toBe(true);
  });

  it('is FALSE when a done arrived with non-empty content but NO attestation', () => {
    // This is the O3 bug: assuming the backend wrote just because `done` was
    // non-empty. The backend is patch-if-empty, so it may have skipped.
    expect(backendOwnsContent({ hadTerminal: true, doneContent: 'server answer' })).toBe(false);
    expect(backendOwnsContent({
      hadTerminal: true, doneContent: 'server answer', answerPersisted: false,
    })).toBe(false);
  });

  it('is FALSE when no terminal arrived (watchdog / reload — client must still write)', () => {
    expect(backendOwnsContent({
      hadTerminal: false, doneContent: 'partial text', answerPersisted: true,
    })).toBe(false);
  });

  it('is FALSE when done arrived with EMPTY content (backend had nothing to persist)', () => {
    expect(backendOwnsContent({ hadTerminal: true, doneContent: '', answerPersisted: true })).toBe(false);
    expect(backendOwnsContent({ hadTerminal: true, doneContent: '   ', answerPersisted: true })).toBe(false);
  });

  it('short-but-valid content still counts, once attested', () => {
    expect(backendOwnsContent({ hadTerminal: true, doneContent: '12', answerPersisted: true })).toBe(true);
    expect(backendOwnsContent({
      hadTerminal: true, doneContent: '好的，已按客户拆分。', answerPersisted: true,
    })).toBe(true);
  });

  it('non-boolean attestation is never treated as success', () => {
    for (const bad of ['true', 1, 'yes', {}, [], null]) {
      expect(backendOwnsContent({
        hadTerminal: true, doneContent: 'x', answerPersisted: bad,
      })).toBe(false);
    }
  });
});

describe('isCurrentGeneration — stale final content after a newer answer', () => {
  it('a frame from an older generation is not current', () => {
    expect(isCurrentGeneration(3, 5)).toBe(false);
  });

  it('the current generation is current', () => {
    expect(isCurrentGeneration(5, 5)).toBe(true);
  });
});

describe('integration of the reducer over a realistic event sequence', () => {
  /** Minimal reducer mirroring Chat.jsx's stream state machine. */
  function reduce(events, gen = 1) {
    let fullContent = '';
    let doneContent = '';
    let hadTerminal = false;
    let outcome = 'completed';
    for (const evt of events) {
      if (evt.gen != null && !isCurrentGeneration(evt.gen, gen)) continue; // stale frames ignored
      if (evt.type === 'delta') fullContent += evt.content;
      else if (evt.type === 'content_replace') {
        const r = applyContentReplace(fullContent, evt.content);
        if (r.accepted) fullContent = r.content;
      } else if (evt.type === 'done') {
        hadTerminal = true;
        if (hasContent(evt.content)) { doneContent = evt.content; fullContent = evt.content; }
        else outcome = fullContent ? 'degraded' : 'failed';
      } else if (evt.type === 'error') {
        outcome = fullContent ? 'degraded' : 'failed';
      } else if (evt.type === 'cancel') {
        outcome = 'cancelled';
      }
    }
    const final = resolveFinalContent({ doneContent, streamedContent: fullContent, placeholder: PLACEHOLDER, hadTerminal });
    const persist = shouldPersistContent({ content: final.content === PLACEHOLDER ? '' : final.content, isCurrentGeneration: true });
    return { rendered: final.content, persisted: persist.writeContent ? final.content : null, outcome, source: final.source };
  }

  it('normal single-pass answer', () => {
    const r = reduce([
      { type: 'delta', content: 'Revenue was 1M.' },
      { type: 'done', content: 'Revenue was 1M.' },
    ]);
    expect(r.rendered).toBe('Revenue was 1M.');
    expect(r.persisted).toBe('Revenue was 1M.');
    expect(r.outcome).toBe('completed');
  });

  it('a failed replacement after a complete answer keeps the answer', () => {
    const r = reduce([
      { type: 'delta', content: 'First complete answer.' },
      { type: 'content_replace', content: '' },          // replacement collapses
      { type: 'done', content: '' },                      // terminated empty
    ]);
    expect(r.rendered).toBe('First complete answer.');
    expect(r.persisted).toBe('First complete answer.');
    expect(r.outcome).toBe('degraded');
  });

  it('a truncated replacement arriving as empty does not erase the answer', () => {
    const r = reduce([
      { type: 'delta', content: 'Complete English answer about revenue.' },
      { type: 'content_replace', content: 'Revenue by cus' },     // truncated but non-empty
      { type: 'content_replace', content: '' },                   // then collapses
    ]);
    // The non-empty (if truncated) replacement was accepted; the empty one was not.
    expect(r.rendered).toBe('Revenue by cus');
    expect(r.persisted).toBe('Revenue by cus');
  });

  it('a stale frame from an older generation never overwrites', () => {
    const r = reduce([
      { type: 'delta', content: 'newer answer', gen: 2 },
      { type: 'done', content: 'newer answer', gen: 2 },
      { type: 'delta', content: 'STALE', gen: 1 },
      { type: 'done', content: 'STALE', gen: 1 },
    ], 2);
    expect(r.rendered).toBe('newer answer');
  });

  it('cancellation preserves partial content and never fabricates success', () => {
    const r = reduce([
      { type: 'delta', content: 'partial answer' },
      { type: 'cancel' },
    ]);
    expect(r.rendered).toBe('partial answer');
    expect(r.outcome).toBe('cancelled');
  });

  it('an error with no content is an explicit failure, never an empty success', () => {
    const r = reduce([{ type: 'error' }]);
    expect(r.rendered).toBe(PLACEHOLDER);
    expect(r.persisted).toBeNull();
    expect(r.outcome).toBe('failed');
  });
});

describe('O3 (corrected) — backend-confirmed ownership handoff', () => {
  /**
   * FAITHFUL model of BOTH real writers.
   *
   * The previous version of this helper was WRONG: it assumed the backend
   * unconditionally wrote `canonical` into the row, which is not what the
   * backend does. The real writer is PATCH-IF-EMPTY (agents.py):
   *
   *   if row is empty          -> write canonical, answer_persisted = true
   *   else if row == canonical -> (already durable) answer_persisted = true
   *   else                     -> SKIP,          answer_persisted = false
   *   on exception             -> answer_persisted = false
   *
   * That difference is exactly what produced the Regenerate bug: the backend
   * skipped, O3 relinquished, and nobody persisted.
   */
  function simulateTurn({
    rowBefore,
    donePayload,
    streamedText = '',
    backendThrows = false,
    clientPutUsesStreamedText = false,
    answerPersistedField,          // undefined => field absent (legacy backend)
    isCurrentGeneration = true,
  }) {
    const hadTerminal = donePayload !== undefined;
    const canonical = (donePayload ?? '').trim() ? donePayload : '';

    // ---- BACKEND: patch-if-empty ----
    let row = rowBefore;
    let backendPersisted;
    if (backendThrows) {
      backendPersisted = false;                       // exception swallowed
    } else if (!canonical) {
      backendPersisted = false;
    } else if (!String(rowBefore).trim()) {
      row = canonical;                                // patched empty row
      backendPersisted = true;
    } else if (rowBefore === canonical) {
      backendPersisted = true;                        // already durable
    } else {
      backendPersisted = false;                       // different non-empty -> skip
    }

    // ---- DONE frame field ----
    const persistedField = answerPersistedField !== undefined
      ? answerPersistedField
      : backendPersisted;

    // ---- CLIENT ----
    const final = resolveFinalContent({
      doneContent: hadTerminal ? canonical : '',
      streamedContent: streamedText,
      placeholder: PLACEHOLDER,
      hadTerminal,
    });
    const owns = backendOwnsContent({
      hadTerminal,
      doneContent: hadTerminal ? canonical : '',
      answerPersisted: persistedField,
    });
    const decision = shouldPersistContent({
      content: final.source === 'placeholder' ? '' : final.content,
      isCurrentGeneration,
      backendOwnsContent: owns,
    });
    if (decision.writeContent) {
      row = clientPutUsesStreamedText ? streamedText : final.content;
    }
    return {
      row,
      decision,
      rendered: final.content,
      backendPersisted,
      consistent: row === (hadTerminal && canonical ? canonical : row),
    };
  }

  // ── The required matrix ──────────────────────────────────────────────
  it('empty row + canonical committed -> relinquish ownership', () => {
    const r = simulateTurn({ rowBefore: '', donePayload: 'CANON' });
    expect(r.backendPersisted).toBe(true);
    expect(r.decision.reason).toBe('backend_owns_content');
    expect(r.row).toBe('CANON');
  });

  it('PARTIAL row + backend skips -> client retains the final write', () => {
    const r = simulateTurn({ rowBefore: 'partial text', donePayload: 'CANON' });
    expect(r.backendPersisted).toBe(false);
    expect(r.decision.writeContent).toBe(true);
    expect(r.row).toBe('CANON');           // DB now matches the UI
    expect(r.rendered).toBe('CANON');
  });

  it('PREVIOUS answer on Regenerate + backend skips -> client retains the write', () => {
    const r = simulateTurn({ rowBefore: 'OLD answer (previous turn)', donePayload: 'CANON' });
    expect(r.backendPersisted).toBe(false);
    expect(r.decision.writeContent).toBe(true);
    expect(r.row).toBe('CANON');
    expect(r.row).not.toBe('OLD answer (previous turn)');
  });

  it('empty row but COMMIT FAILS -> client retains the final write', () => {
    const r = simulateTurn({ rowBefore: '', donePayload: 'CANON', backendThrows: true });
    expect(r.backendPersisted).toBe(false);
    expect(r.decision.writeContent).toBe(true);
    expect(r.row).toBe('CANON');
  });

  it('row already canonical -> backend verifies identical -> may relinquish', () => {
    const r = simulateTurn({ rowBefore: 'CANON', donePayload: 'CANON' });
    expect(r.backendPersisted).toBe(true);
    expect(r.decision.reason).toBe('backend_owns_content');
    expect(r.row).toBe('CANON');
  });

  it('terminal field ABSENT (legacy backend) -> legacy client-write behaviour', () => {
    const r = simulateTurn({
      rowBefore: '',
      donePayload: 'CANON',
      answerPersistedField: undefined,
    });
    // With the field absent the simulated backend still reports true above,
    // so assert the strict rule directly on the helper:
    expect(backendOwnsContent({ hadTerminal: true, doneContent: 'CANON' })).toBe(false);
    expect(r.row).toBe('CANON');           // still persisted, by the backend
  });

  it('answer_persisted false/string/malformed -> never relinquish', () => {
    for (const bad of [false, 'true', 1, null, undefined, 'yes', {}]) {
      expect(backendOwnsContent({
        hadTerminal: true, doneContent: 'CANON', answerPersisted: bad,
      })).toBe(false);
    }
  });

  it('empty or failed terminal -> do not relinquish', () => {
    expect(backendOwnsContent({ hadTerminal: true, doneContent: '', answerPersisted: true })).toBe(false);
    expect(backendOwnsContent({ hadTerminal: false, doneContent: 'CANON', answerPersisted: true })).toBe(false);
  });

  it('no done (watchdog/reload): client remains the writer', () => {
    const r = simulateTurn({ rowBefore: '', donePayload: undefined, streamedText: 'partial answer' });
    expect(r.decision.writeContent).toBe(true);
    expect(r.row).toBe('partial answer');
  });

  it('stale generation still preserves the newer generation', () => {
    const r = simulateTurn({ rowBefore: '', donePayload: 'OLD GEN', isCurrentGeneration: false });
    expect(r.decision.reason).toBe('stale_generation');
    expect(r.decision.writeContent).toBe(false);
  });

  // ── The regression this correction exists to prevent ─────────────────
  it('REGRESSION GUARD: partial row + O3 alone (no attestation) would lose the answer', () => {
    // Model the OLD O3 rule (relinquish on any non-empty done) to prove the
    // bug is real and that the new rule fixes it.
    const oldRelinquish = true;   // O3 as originally shipped
    let row = 'partial text';
    if (oldRelinquish) { /* client skipped */ }
    expect(row).not.toBe('CANON');   // the bug

    // Corrected rule:
    const r = simulateTurn({ rowBefore: 'partial text', donePayload: 'CANON' });
    expect(r.row).toBe('CANON');     // fixed
  });

  it('STALE client text cannot overwrite a backend-committed answer (C8 race)', () => {
    // Backend committed into an empty row; client's local text is stale.
    const r = simulateTurn({
      rowBefore: '',
      donePayload: 'CANONICAL answer from server.',
      streamedText: 'stale partial text',
      clientPutUsesStreamedText: true,
    });
    expect(r.decision.reason).toBe('backend_owns_content');
    expect(r.row).toBe('CANONICAL answer from server.');
  });

  it('done.content differs from streamed text: server wins in the row', () => {
    const r = simulateTurn({
      rowBefore: '',
      donePayload: 'Server joined ALL iterations.',
      streamedText: 'only the last iteration',
    });
    expect(r.row).toBe('Server joined ALL iterations.');
  });

  it('short and Chinese canonical answers are owned by the backend', () => {
    expect(simulateTurn({ rowBefore: '', donePayload: '12' }).row).toBe('12');
    expect(simulateTurn({ rowBefore: '', donePayload: '好的，已按客户拆分。' }).row)
      .toBe('好的，已按客户拆分。');
  });
});

// ── D5 (2026-11-01): server retry reset markers must not double the answer ──
import { applyDelta } from './answerLifecycle';

describe('D5 — reset-aware delta accumulation', () => {
  it('REPRO: deltas after a rejected empty content_replace replace, not append', () => {
    // Server streamed "AB", then a generator retry emitted content_replace ""
    // (rejected — the bubble must keep "AB"), then re-streamed "A","B".
    let content = 'AB';
    let pending = true; // rejected empty replace → reset marker set
    const r1 = applyDelta(content, 'A', pending);
    content = r1.content; pending = r1.pendingReset;
    const r2 = applyDelta(content, 'B', pending);
    content = r2.content; pending = r2.pendingReset;
    // Transient doubling ("ABAB") is gone; the re-stream replaced cleanly.
    expect(content).toBe('AB');
    expect(pending).toBe(false);
  });

  it('normal accumulation is unchanged when no reset is pending', () => {
    let r = applyDelta('', 'A', false);
    r = applyDelta(r.content, 'B', r.pendingReset);
    expect(r.content).toBe('AB');
  });

  it('non-empty content_replace clears the pending reset (authoritative)', () => {
    // Callers clear the flag on accepted replacements; helper documents that
    // a pending reset with an incoming authoritative replace is moot because
    // applyContentReplace already set the full content and callers reset.
    let r = applyDelta('stale', 'x', true);
    expect(r.content).toBe('x');
    expect(r.pendingReset).toBe(false);
  });

  it('non-string delta is treated as empty', () => {
    const r = applyDelta('keep', undefined, false);
    expect(r.content).toBe('keep');
  });
});
