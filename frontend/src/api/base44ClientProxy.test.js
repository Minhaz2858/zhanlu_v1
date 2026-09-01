/**
 * Tests for the base44 SDK 401-recovery Proxy.
 *
 * Bug being fixed: the base44 SDK bakes the JWT into axios.defaults at
 * page-load and has no 401-retry. After 15 minutes every
 * `base44.entities.<X>.<method>(...)` call starts throwing 401 — pages
 * using the SDK with try/finally (no catch) silently render empty states,
 * and the chat input appears dead because the very first SDK call inside
 * `handleSend` (ChatSession.create / ChatMessage.create) rejects.
 *
 * Fix contract:
 *   1. The exported `base44.entities.<X>` and `base44.functions.<Y>` resolve
 *      to a Proxy whose methods (`list`, `filter`, `get`, `create`,
 *      `update`, `delete`, ...) automatically await a single-flight refresh
 *      on 401, then transparently retry the call once.
 *   2. The Proxy is read/write identical to the SDK API — a drop-in
 *      replacement. Existing call sites keep using
 *      `base44.entities.X.list()` unchanged.
 *   3. The Proxy does NOT retry non-401 errors (avoids masking real bugs).
 *   4. If refresh fails after a 401, the original 401 propagates so the
 *      UI can show "Session expired" and trigger logout.
 *
 * This file pins that contract; the implementation lives in
 * `api/base44Client.js`. We do NOT modify the SDK itself.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const { mockAppId, sdkState } = vi.hoisted(() => {
  const mockAppId = 'test-app-base44proxy';

  function makeStubEntity() {
    return {
      create: vi.fn(async () => ({ id: 'stub', ok: true })),
      list: vi.fn(async () => []),
      filter: vi.fn(async () => []),
      get: vi.fn(async () => ({})),
      update: vi.fn(async () => ({ id: 'stub' })),
      delete: vi.fn(async () => ({ ok: true })),
    };
  }

  const sdkState = {
    auth: {
      setToken: vi.fn(),
      removeToken: vi.fn(),
      me: vi.fn(async () => ({ id: 'u1' })),
    },
    entities: {
      ChatSession: makeStubEntity(),
      ChatMessage: makeStubEntity(),
      Project: makeStubEntity(),
    },
    functions: {
      someFunc: vi.fn(async () => ({ ok: true })),
    },
  };

  return { mockAppId, sdkState };
});

const mockSetToken = sdkState.auth.setToken;
const mockRemoveToken = sdkState.auth.removeToken;

vi.mock('@/lib/app-params', () => ({
  appParams: { appId: mockAppId, token: 'a_initial' },
}));

vi.mock('@base44/sdk', () => ({
  createClient: () => sdkState,
}));

// Mock the shared refresh util so we control its return value per test.
const mockRotate = vi.fn(async () => 'a_ROTATED');
vi.mock('@/api/tokenRefresh', () => ({
  rotateAccessToken: () => mockRotate(),
}));

function seedLocalStorage() {
  global.localStorage.clear();
  global.localStorage.setItem('base44_access_token', 'a_initial');
  global.localStorage.setItem('token', 'a_initial');
  global.localStorage.setItem('refresh_token', 'r_initial');
  global.localStorage.setItem('base44_app_id', mockAppId);
}

describe('base44 client — 401 retry proxy', () => {
  beforeEach(() => {
    mockSetToken.mockReset();
    mockRemoveToken.mockReset();
    mockRotate.mockReset();
    mockRotate.mockResolvedValue('a_ROTATED');
    seedLocalStorage();
    // Reset stub calls on the shared hoisted state.
    for (const e of Object.values(sdkState.entities)) {
      for (const fn of Object.values(e)) fn.mockClear();
    }
    for (const fn of Object.values(sdkState.functions)) fn.mockClear();
  });
  afterEach(() => {
    vi.resetModules();
  });

  it('returns the SDK result on first-success (no refresh, no retry)', async () => {
    const { base44 } = await import('@/api/base44Client');
    sdkState.entities.ChatSession.list.mockResolvedValueOnce([{ id: 's1' }]);

    const result = await base44.entities.ChatSession.list();

    expect(result).toEqual([{ id: 's1' }]);
    expect(sdkState.entities.ChatSession.list).toHaveBeenCalledTimes(1);
    expect(mockRotate).not.toHaveBeenCalled();
  });

  it('on 401 throws from the SDK, calls rotateAccessToken and retries once', async () => {
    const { base44 } = await import('@/api/base44Client');
    sdkState.entities.ChatSession.list
      .mockRejectedValueOnce(make401Error())
      .mockResolvedValueOnce([{ id: 's2' }]);

    const result = await base44.entities.ChatSession.list();

    expect(result).toEqual([{ id: 's2' }]);
    expect(sdkState.entities.ChatSession.list).toHaveBeenCalledTimes(2);
    expect(mockRotate).toHaveBeenCalledTimes(1);
  });

  it('on 401 + failed refresh, propagates the original 401 (no infinite loop)', async () => {
    const { base44 } = await import('@/api/base44Client');
    mockRotate.mockResolvedValueOnce(null); // refresh failed
    sdkState.entities.ChatSession.list.mockRejectedValue(make401Error());

    await expect(base44.entities.ChatSession.list()).rejects.toMatchObject({
      status: 401,
    });
    // Only tried once after the failed refresh — the proxy MUST NOT loop.
    expect(sdkState.entities.ChatSession.list).toHaveBeenCalledTimes(1);
    expect(mockRotate).toHaveBeenCalledTimes(1);
  });

  it('passes arguments through unchanged on retry', async () => {
    const { base44 } = await import('@/api/base44Client');
    sdkState.entities.ChatSession.create
      .mockRejectedValueOnce(make401Error())
      .mockResolvedValueOnce({ id: 's3' });

    const args = [{ title: 't', project: null }];
    const result = await base44.entities.ChatSession.create(...args);

    expect(result).toEqual({ id: 's3' });
    expect(sdkState.entities.ChatSession.create).toHaveBeenNthCalledWith(1, ...args);
    expect(sdkState.entities.ChatSession.create).toHaveBeenNthCalledWith(2, ...args);
  });

  it('does NOT trigger refresh on non-401 errors', async () => {
    const { base44 } = await import('@/api/base44Client');
    sdkState.entities.ChatSession.list.mockRejectedValueOnce(new Error('boom'));

    await expect(base44.entities.ChatSession.list()).rejects.toThrow('boom');
    expect(mockRotate).not.toHaveBeenCalled();
  });

  it('handles base44.functions.<X>(...) the same way', async () => {
    const { base44 } = await import('@/api/base44Client');
    sdkState.functions.someFunc
      .mockRejectedValueOnce(make401Error())
      .mockResolvedValueOnce({ ok: true });

    const result = await base44.functions.someFunc({ x: 1 });

    expect(result).toEqual({ ok: true });
    expect(sdkState.functions.someFunc).toHaveBeenCalledTimes(2);
    expect(mockRotate).toHaveBeenCalledTimes(1);
  });

  it('still allows reading a property (entity name) without triggering refresh', async () => {
    const { base44 } = await import('@/api/base44Client');
    expect(base44.entities.ChatSession).toBeDefined();
    expect(mockRotate).not.toHaveBeenCalled();
  });
});

/**
 * Build a base44-flavoured 401 error. The real SDK wraps axios errors with
 * `status` / `response.status` fields; we mirror that so the Proxy's
 * `err?.status === 401` check works.
 */
function make401Error() {
  const err = new Error('Request failed with status code 401');
  err.status = 401;
  err.response = { status: 401, data: { detail: 'Token expired' } };
  return err;
}
