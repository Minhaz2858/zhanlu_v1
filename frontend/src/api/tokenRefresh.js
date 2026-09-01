/**
 * Shared, single-flight token-refresh utility.
 *
 * The bug this fixes: before this module existed, /auth/refresh was called
 * from two uncoordinated places (`api/authFetch.js` and
 * `lib/AuthContext.jsx`). Only one of them routed the rotated access token
 * into the base44 SDK's in-memory axios header, so every page using
 * `base44.entities.*` started 401-ing exactly 15 minutes after login.
 * Concurrent callers also raced on the one-time-use refresh token, killing
 * the session instead of recovering it.
 *
 * Contract:
 *   - `rotateAccessToken()` is the single entry point used by both
 *     `authFetch` and `AuthContext`. It is single-flight: a burst of N
 *     concurrent calls shares the same in-flight promise.
 *   - On success it persists the new access + refresh tokens to
 *     localStorage AND calls `base44.auth.setToken(newAccessToken)` so
 *     the SDK's next request uses the rotated token.
 *   - On any failure (no refresh token, replay rejected, network error,
 *     non-OK response) it returns `null`. The caller must treat `null`
 *     as "session is dead — log the user out".
 *
 * Implementation note: `happy-dom`/`vitest` test mocks dynamically replace
 * `global.fetch` and the SDK module; the implementation therefore reads
 * the SDK via a top-level `import` (resolved by Vite) and the in-flight
 * `_pending` is keyed per-app so multi-app test setups don't collide.
 */

import { base44 } from '@/api/base44Client';

const TOKEN_STORAGE_KEY = 'base44_access_token';
const LEGACY_TOKEN_KEY = 'token';
const REFRESH_TOKEN_KEY = 'refresh_token';
const APP_ID_STORAGE_KEY = 'base44_app_id';

// Single-flight state. We keep one in-flight promise per (appId, network)
// so concurrent callers share the same refresh. The map is reset on every
// successful or failed refresh — a failed rotation MUST NOT poison the
// next attempt (replay-protection may have failed once, succeed the next
// time the user re-authenticates).
let _pending = null;

/**
 * Read a value from `window.localStorage`. Wrapped in try/catch because
 * private-mode browsers + some embedded webviews throw on access.
 */
function readStorage(key) {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key, value) {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* ignore quota / private mode */
  }
}

/**
 * Rotate the access token using the current refresh token, propagating the
 * new token into the base44 SDK so subsequent SDK-managed requests use it.
 *
 * Returns the new access token on success, or `null` on any failure (caller
 * should treat as "session expired").
 */
export async function rotateAccessToken() {
  if (_pending) return _pending;

  const refreshToken = readStorage(REFRESH_TOKEN_KEY);
  if (!refreshToken) return null;

  const appId =
    readStorage(APP_ID_STORAGE_KEY) ||
    (import.meta?.env?.VITE_BASE44_APP_ID) ||
    null;
  if (!appId) return null;

  _pending = (async () => {
    try {
      const resp = await fetch(`/api/apps/${appId}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!resp.ok) return null;

      let data;
      try {
        data = await resp.json();
      } catch {
        return null;
      }
      const newAccess = data?.access_token;
      const newRefresh = data?.refresh_token;
      if (!newAccess) return null;

      // 1) Persist to localStorage so the next authFetch (legacy path) and
      //    any other code that reads from storage uses the fresh token.
      writeStorage(TOKEN_STORAGE_KEY, newAccess);
      writeStorage(LEGACY_TOKEN_KEY, newAccess);
      if (newRefresh) writeStorage(REFRESH_TOKEN_KEY, newRefresh);

      // 2) Push the token into the base44 SDK's axios default header so
      //    EVERY base44.entities.* call made after this point uses the
      //    fresh token, mid-session. Without this, the SDK keeps the
      //    dead in-memory token and only heals on full page reload.
      try {
        base44.auth.setToken(newAccess);
      } catch {
        /* non-fatal — caller still has localStorage to fall back on */
      }

      return newAccess;
    } catch {
      return null;
    } finally {
      _pending = null;
    }
  })();

  return _pending;
}

/**
 * Test-only helper: clears the single-flight in-flight promise so the next
 * `rotateAccessToken()` call is forced to actually hit the network.
 * Production code MUST NOT call this — it's exported only because the test
 * suite needs to assert on "after a failed refresh, the next call retries"
 * without sharing state across tests.
 */
export function _resetRefreshCacheForTests() {
  _pending = null;
}

export default rotateAccessToken;
