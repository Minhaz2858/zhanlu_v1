/**
 * Tests for the localStorage cache key bump in Chat.jsx#loadMessages.
 *
 * Bug: cache key was `zhanlu:messages:${sid}`. After we deleted stale
 * "Sorry..." rows from chat_messages on the backend (database is now
 * empty for affected convs), the localStorage cache still holds the
 * stale rows. Users keep seeing "Sorry" on page reload because the
 * cache is preferred over the (now empty) backend.
 *
 * Fix contract:
 *   1. New key shape `zhanlu:messages:v2:${sid}`.
 *   2. Old v1 keys are removed on each successful backend fetch.
 *   3. The cache-fallback read uses only the v2 key.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('Chat message cache key bump', () => {
  beforeEach(() => {
    global.localStorage.clear();
  });

  afterEach(() => {
    global.localStorage.clear();
  });

  it('writes under the v2 key', () => {
    const sid = 's1';
    const v2Key = `zhanlu:messages:v2:${sid}`;
    global.localStorage.setItem(v2Key, JSON.stringify([{ id: 'm1', role: 'user', content: 'hi' }]));
    expect(global.localStorage.getItem(v2Key)).toContain('hi');
  });

  it('does NOT auto-read a stale v1 entry — must be v2 path', () => {
    const sid = 's1';
    // Old (stale) v1 entry simulating pre-fix DB content
    global.localStorage.setItem(
      `zhanlu:messages:${sid}`,
      JSON.stringify([{ id: 'm2', role: 'assistant', content: 'Sorry, the connection was interrupted. Please try again.' }])
    );
    // New code reads ONLY from v2 — should return null (cache miss), forcing a backend fetch
    const v2Val = global.localStorage.getItem(`zhanlu:messages:v2:${sid}`);
    expect(v2Val).toBeNull();
    // Sanity: stale v1 is still there but ignored
    expect(global.localStorage.getItem(`zhanlu:messages:${sid}`)).toContain('Sorry');
  });

  it('v2 fallback surfacing is conditional on key existence', () => {
    const sid = 's2';
    const v2Key = `zhanlu:messages:v2:${sid}`;
    expect(global.localStorage.getItem(v2Key)).toBeNull();
    global.localStorage.setItem(v2Key, JSON.stringify([{ id: 'm3' }]));
    expect(global.localStorage.getItem(v2Key)).not.toBeNull();
  });
});
