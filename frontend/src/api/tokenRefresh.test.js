/**
 * Tests for the shared single-flight token-refresh utility.
 *
 * Bug being fixed: previously the refresh-token rotation lived in two
 * uncoordinated places (`authFetch.js` and `AuthContext.jsx`). Only one of
 * them called `base44.auth.setToken(...)` after a successful rotation, so
 * the SDK's axios client kept the dead bearer header in memory until the
 * page was reloaded. Meanwhile both callers raced on the /auth/refresh
 * endpoint — refresh tokens are one-time-use, so the second concurrent
 * call would replay a consumed token and kill the session.
 *
 * Fix contract:
 *   1. `rotateAccessToken({ appId, fetchImpl })` is the single entry point
 *      that both `authFetch` and `AuthContext` call. It is single-flight:
 *      a burst of N concurrent calls shares the same in-flight promise.
 *   2. On success it persists the new tokens to localStorage AND calls
 *      `base44.auth.setToken(newAccessToken)` so the base44 SDK's axios
 *      client uses the new token on its very next request.
 *   3. On failure (no refresh token, replay rejected, network error) it
 *      returns null so the caller can surface `SessionExpiredError`.
 *
 * This file pins that contract; the implementation lives in
 * `api/tokenRefresh.js` and is consumed by `api/authFetch.js` and
 * `lib/AuthContext.jsx`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const { mockAppId } = vi.hoisted(() => ({ mockAppId: 'test-app-tokenrefresh' }));

const mockSetToken = vi.fn();
const mockRemoveToken = vi.fn();
const mockSdkAuth = { setToken: mockSetToken, removeToken: mockRemoveToken };

vi.mock('@base44/sdk', () => ({
  createClient: () => ({
    auth: mockSdkAuth,
  }),
}));

vi.mock('@/lib/app-params', () => ({
  appParams: { appId: mockAppId },
}));

const TOK_KEY = 'base44_access_token';
const LEGACY_KEY = 'token';
const REFRESH_KEY = 'refresh_token';
const APP_ID_KEY = 'base44_app_id';

function seedLocalStorage(refresh = 'r_initial', access = 'a_initial', appId = mockAppId) {
  global.localStorage.clear();
  global.localStorage.setItem(REFRESH_KEY, refresh);
  global.localStorage.setItem(TOK_KEY, access);
  global.localStorage.setItem(LEGACY_KEY, access);
  global.localStorage.setItem(APP_ID_KEY, appId);
}

describe('api/tokenRefresh — rotateAccessToken', () => {
  let originalFetch;
  let originalFetchImpl;

  beforeEach(() => {
    originalFetch = global.fetch;
    mockSetToken.mockReset();
    mockRemoveToken.mockReset();
    seedLocalStorage();
  });
  afterEach(() => {
    global.fetch = originalFetch;
    vi.resetModules();
  });

  it('calls /auth/refresh with the current refresh token and returns the new access token', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    global.fetch = vi.fn(async (url, opts) => {
      expect(url).toBe(`/api/apps/${mockAppId}/auth/refresh`);
      expect(opts.method).toBe('POST');
      const body = JSON.parse(opts.body);
      expect(body.refresh_token).toBe('r_initial');
      return {
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'a_rotated', refresh_token: 'r_rotated' }),
      };
    });

    const token = await rotateAccessToken();
    expect(token).toBe('a_rotated');
  });

  it('persists new access + refresh tokens to localStorage on success', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    global.fetch = vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ access_token: 'a_NEW', refresh_token: 'r_NEW' }),
    }));

    await rotateAccessToken();

    expect(global.localStorage.getItem(TOK_KEY)).toBe('a_NEW');
    expect(global.localStorage.getItem(LEGACY_KEY)).toBe('a_NEW');
    expect(global.localStorage.getItem(REFRESH_KEY)).toBe('r_NEW');
  });

  it('calls base44.auth.setToken with the new access token on success (the bug fix)', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    global.fetch = vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ access_token: 'a_FRESH', refresh_token: 'r_FRESH' }),
    }));

    await rotateAccessToken();

    expect(mockSetToken).toHaveBeenCalledWith('a_FRESH');
  });

  it('dedupes concurrent calls — one fetch to /auth/refresh for N callers', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    const refreshSpy = vi.fn(async () => {
      // Add a tiny delay so concurrent callers pile up
      await new Promise((r) => setTimeout(r, 10));
      return { ok: true, status: 200, json: async () => ({ access_token: 'a_ONE', refresh_token: 'r_ONE' }) };
    });
    global.fetch = refreshSpy;

    const results = await Promise.all([
      rotateAccessToken(),
      rotateAccessToken(),
      rotateAccessToken(),
      rotateAccessToken(),
    ]);

    expect(refreshSpy).toHaveBeenCalledTimes(1);
    expect(results).toEqual(['a_ONE', 'a_ONE', 'a_ONE', 'a_ONE']);
    expect(mockSetToken).toHaveBeenCalledTimes(1);
  });

  it('returns null and does not call setToken when no refresh token is stored', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    global.localStorage.removeItem(REFRESH_KEY);
    const refreshSpy = vi.fn();
    global.fetch = refreshSpy;

    const token = await rotateAccessToken();
    expect(token).toBeNull();
    expect(refreshSpy).not.toHaveBeenCalled();
    expect(mockSetToken).not.toHaveBeenCalled();
  });

  it('returns null and does not call setToken when refresh endpoint returns non-OK', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    global.fetch = vi.fn(async () => ({
      ok: false, status: 401,
      json: async () => ({ detail: 'Token has been revoked' }),
    }));

    const token = await rotateAccessToken();
    expect(token).toBeNull();
    expect(mockSetToken).not.toHaveBeenCalled();
  });

  it('returns null and does not call setToken when refresh endpoint throws', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    global.fetch = vi.fn(async () => {
      throw new Error('network down');
    });

    const token = await rotateAccessToken();
    expect(token).toBeNull();
    expect(mockSetToken).not.toHaveBeenCalled();
  });

  it('after a failed refresh, the next call retries (does not poison the single-flight cache)', async () => {
    const { rotateAccessToken } = await import('@/api/tokenRefresh');
    let attempt = 0;
    global.fetch = vi.fn(async () => {
      attempt++;
      if (attempt === 1) {
        return { ok: false, status: 401, json: async () => ({}) };
      }
      return { ok: true, status: 200, json: async () => ({ access_token: 'a_NEXT', refresh_token: 'r_NEXT' }) };
    });

    const first = await rotateAccessToken();
    const second = await rotateAccessToken();

    expect(first).toBeNull();
    expect(second).toBe('a_NEXT');
    expect(attempt).toBe(2);
  });
});
