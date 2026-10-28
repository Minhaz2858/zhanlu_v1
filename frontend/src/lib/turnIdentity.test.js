/**
 * Turn-identity transport — behavioural tests (Stage 2B Commit 4A).
 *
 * Scope: this module only transports `assistant_message_id` + `request_id`.
 * These tests assert the TRANSPORT contract, not the (unimplemented)
 * concurrency guarantee. Nothing here claims the race is fixed.
 *
 * Covered: normal send, regeneration, retry reuse, overlapping turns, and
 * distinct message identities.
 */
import { describe, it, expect } from 'vitest';
import {
  generateTurnId,
  isValidTurnId,
  buildTurnIdentityFields,
  newTurnIdentity,
  reuseTurnIdentity,
  TURN_ID_RE,
} from './turnIdentity';

const MSG_A = '3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6a7b';
const MSG_B = '9c8b7a65-4321-4fed-8cba-0123456789ab';

describe('generateTurnId', () => {
  it('produces an id the backend regex accepts', () => {
    const id = generateTurnId();
    expect(typeof id).toBe('string');
    expect(isValidTurnId(id)).toBe(true);
    expect(TURN_ID_RE.test(id)).toBe(true);
  });

  it('is not constant (independent turns must differ)', () => {
    const ids = new Set(Array.from({ length: 50 }, () => generateTurnId()));
    expect(ids.size).toBe(50);
  });
});

describe('isValidTurnId', () => {
  it('accepts UUIDs (the shape ChatMessage.create returns)', () => {
    expect(isValidTurnId(MSG_A)).toBe(true);
  });

  it('rejects junk that must never be sent as an identity', () => {
    for (const bad of ['', '   ', 'abc', null, undefined, 123, {}, 'has space!', 'x'.repeat(65)]) {
      expect(isValidTurnId(bad)).toBe(false);
    }
  });
});

describe('buildTurnIdentityFields', () => {
  it('includes both fields when valid', () => {
    const f = buildTurnIdentityFields({ assistantMessageId: MSG_A, requestId: 'req_abcdef01' });
    expect(f).toEqual({ assistant_message_id: MSG_A, request_id: 'req_abcdef01' });
  });

  it('omits (never sends) a malformed id, so the backend sees a legacy request', () => {
    const f = buildTurnIdentityFields({ assistantMessageId: 'bad id!', requestId: 'req_abcdef01' });
    expect(f).toEqual({ request_id: 'req_abcdef01' });
    expect('assistant_message_id' in f).toBe(false);
  });

  it('returns an empty object when nothing valid is supplied', () => {
    expect(buildTurnIdentityFields({})).toEqual({});
    expect(buildTurnIdentityFields()).toEqual({});
  });
});

describe('normal send', () => {
  it('carries the client-created assistant row id and a fresh request id', () => {
    const identity = newTurnIdentity({ assistantMessageId: MSG_A });
    expect(identity.assistant_message_id).toBe(MSG_A);
    expect(isValidTurnId(identity.request_id)).toBe(true);
  });

  it('two independent sends get DIFFERENT request ids but their own message ids', () => {
    const t1 = newTurnIdentity({ assistantMessageId: MSG_A });
    const t2 = newTurnIdentity({ assistantMessageId: MSG_B });
    expect(t1.request_id).not.toBe(t2.request_id);
    expect(t1.assistant_message_id).toBe(MSG_A);
    expect(t2.assistant_message_id).toBe(MSG_B);
  });
});

describe('regeneration', () => {
  it('KEEPS the same assistant message id (it rewrites that row)', () => {
    const original = newTurnIdentity({ assistantMessageId: MSG_A });
    const regen = newTurnIdentity({ assistantMessageId: MSG_A });
    expect(regen.assistant_message_id).toBe(original.assistant_message_id);
  });

  it('mints a NEW request id (regeneration is a new, newer turn)', () => {
    const original = newTurnIdentity({ assistantMessageId: MSG_A });
    const regen = newTurnIdentity({ assistantMessageId: MSG_A });
    expect(regen.request_id).not.toBe(original.request_id);
  });
});

describe('retry reuse', () => {
  it('reusing an identity yields the SAME request id and message id', () => {
    const original = newTurnIdentity({ assistantMessageId: MSG_A });
    const retry = reuseTurnIdentity(original);
    expect(retry.request_id).toBe(original.request_id);
    expect(retry.assistant_message_id).toBe(original.assistant_message_id);
  });

  it('a retry must NOT mint a new request id (models authFetch body replay)', () => {
    const original = newTurnIdentity({ assistantMessageId: MSG_A });
    // Two consecutive retries of the same logical request.
    const r1 = reuseTurnIdentity(original);
    const r2 = reuseTurnIdentity(r1);
    expect(r1.request_id).toBe(original.request_id);
    expect(r2.request_id).toBe(original.request_id);
  });

  it('reusing a null/absent identity produces no fields (never invents one)', () => {
    expect(reuseTurnIdentity(null)).toEqual({});
    expect(reuseTurnIdentity({})).toEqual({});
  });
});

describe('overlapping turns', () => {
  it('two in-flight turns in one conversation stay distinguishable', () => {
    const t1 = newTurnIdentity({ assistantMessageId: MSG_A });
    const t2 = newTurnIdentity({ assistantMessageId: MSG_B });
    // Identify by (message_id, request_id): both differ independently.
    expect(t1.assistant_message_id).not.toBe(t2.assistant_message_id);
    expect(t1.request_id).not.toBe(t2.request_id);
  });

  it('a retry of turn 1 cannot be confused with turn 2', () => {
    const t1 = newTurnIdentity({ assistantMessageId: MSG_A });
    const t2 = newTurnIdentity({ assistantMessageId: MSG_B });
    const t1Retry = reuseTurnIdentity(t1);
    expect(t1Retry.request_id).toBe(t1.request_id);
    expect(t1Retry.request_id).not.toBe(t2.request_id);
    expect(t1Retry.assistant_message_id).not.toBe(t2.assistant_message_id);
  });
});

describe('distinct message identities', () => {
  it('the transported id is exactly the one supplied (no rewriting)', () => {
    const identity = newTurnIdentity({ assistantMessageId: MSG_A });
    expect(identity.assistant_message_id === MSG_A).toBe(true);
  });

  it('a missing message id still transports a request id', () => {
    const identity = newTurnIdentity({});
    expect(identity.assistant_message_id).toBeUndefined();
    expect(isValidTurnId(identity.request_id)).toBe(true);
  });
});
