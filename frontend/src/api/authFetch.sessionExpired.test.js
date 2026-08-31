/**
 * Tests for authFetch's session-expired surfacing.
 *
 * Bug: when the user's stored refresh token is consumed (replay protection),
 * refreshAccessToken() returns null, authFetch still returns the original 401
 * to the caller, and the caller's UI can't distinguish "expired token must
 * log in" from "stream failed for some other reason" — so the chat shows the
 * generic "Sorry, the connection was interrupted" message and persists it.
 *
 * Fix contract:
 *   1. `authFetch` exposes a helper `authFetchOrThrow(url, opts)` that, when
 *      a 401 comes back even after a refresh attempt, throws a typed
 *      `SessionExpiredError`. The streaming endpoint uses this helper.
 *   2. `SessionExpiredError.sessionExpired === true` so the chat UI can
 *      detect it and render the auth-required message instead of the
 *      generic network-failure message (and NOT persist the error to
 *      ChatMessage).
 *
 * This file pins that contract; the implementation lives in
 * `api/authFetch.js` + `api/agentEnhanced.js`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const { mockAppId } = vi.hoisted(() => ({ mockAppId: 'test-app-123' }));

vi.mock('@/lib/app-params', () => ({
  appParams: { appId: mockAppId },
}));

import {
  authFetch,
  SessionExpiredError,
  authFetchOrThrow,
} from '@/api/authFetch';

function mockLocalStorage() {
  // jsdom provides localStorage but we want explicit state for each test
  global.localStorage.clear();
  global.localStorage.setItem('refresh_token', 'r_old');
  global.localStorage.setItem('base44_app_id', mockAppId);
}

describe('authFetch session-expired surfacing', () => {
  let originalFetch;

  beforeEach(() => {
    originalFetch = global.fetch;
    mockLocalStorage();
  });
  afterEach(() => {
    global.fetch = originalFetch;
  });

  it('throws SessionExpiredError when the request gets 401 and refresh returns null', async () => {
    // First call = original request → 401.
    // Second call = refresh attempt → 401 (refresh_token already revoked).
    // Expected: helper throws SessionExpiredError rather than returning a
    // response whose status === 401 (which the caller would misinterpret
    // as a generic network failure).
    let i = 0;
    global.fetch = vi.fn(async (url, _opts) => {
      if (url.endsWith('/auth/refresh')) {
        return { ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}), body: null };
      }
      i++;
      if (i === 1) {
        return { ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}), body: null };
      }
      return { ok: true, status: 200, statusText: 'OK', json: async () => ({ ok: true }), body: null };
    });

    await expect(
      authFetchOrThrow('/api/apps/test-app-123/agents/conversations/v3/c1/messages/stream', {
        method: 'POST',
        body: JSON.stringify({ content: 'hi' }),
      })
    ).rejects.toBeInstanceOf(SessionExpiredError);
  });

  it('does NOT throw SessionExpiredError when refresh succeeds and retry returns 200', async () => {
    let i = 0;
    global.fetch = vi.fn(async (url, _opts) => {
      if (url.endsWith('/auth/refresh')) {
        return {
          ok: true, status: 200, statusText: 'OK',
          json: async () => ({ access_token: 'a_new', refresh_token: 'r_new' }),
          body: null,
        };
      }
      i++;
      if (i === 1) {
        return { ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}), body: null };
      }
      return { ok: true, status: 200, statusText: 'OK', json: async () => ({ id: 'c1' }), body: null };
    });

    const r = await authFetchOrThrow('/api/apps/test-app-123/agents/conversations/c1', { method: 'GET' });
    expect(r.status).toBe(200);
  });

  it('does NOT throw SessionExpiredError when status is 401 on a non-/api/ route', async () => {
    // Only /api/* triggers the refresh-and-retry helper. Other 401s should
    // surface normally to the caller.
    global.fetch = vi.fn(async (_url, _opts) => ({
      ok: false, status: 401, statusText: 'Unauthorized',
      json: async () => ({}), body: null,
    }));

    // Non-/api/ URL → no refresh attempt, no SessionExpiredError.
    const r = await authFetchOrThrow('https://example.com/x', { method: 'GET' });
    expect(r.status).toBe(401);
  });

  it('authFetch (legacy) still returns the response so non-throwing callers keep working', async () => {
    // Existing call sites (e.g. polling loops) call `authFetch`
    // directly. They expect a Response, not a throw. Confirm the legacy
    // helper hasn't been broken.
    global.fetch = vi.fn(async (_url, _opts) => ({
      ok: true, status: 200, statusText: 'OK',
      json: async () => ({ data: 'ok' }), body: null,
    }));

    const r = await authFetch('/api/apps/test-app-123/anything', { method: 'GET' });
    expect(r.status).toBe(200);
  });
});
