/**
 * Centralised fetch helper that attaches the user's JWT (if any) to every
 * request, mirroring the base44 SDK's behaviour for `axios`. Use this instead
 * of raw `fetch(...)` whenever the target endpoint is on the local app's
 * `/api/...` routes — those routes require `Authorization: Bearer <token>`
 * once `Authentication required` mode is on (plan 2026-07-27).
 *
 * Drop-in replacement for `fetch`:
 *
 *   const res = await authFetch(`/api/automations/${id}/run`, { method: 'POST' });
 *
 * For streaming endpoints, prefer `authStreamingFetch` so the body stays a
 * raw `ReadableStream` (auth headers are still attached).
 *
 * Refresh-on-401: the access token is short-lived (ACCESS_TOKEN_EXPIRE_MINUTES,
 * default 15 min). When a request comes back 401 mid-session, authFetch
 * transparently performs ONE silent refresh-token rotation and retries the
 * original call — so long-lived sessions (chat, dashboards) don't start
 * erroring every 15 minutes. If the refresh fails (no refresh token / expired),
 * the original 401 is returned and the caller/AuthContext will redirect to
 * login. A module-level promise dedupes concurrent 401 bursts so only one
 * refresh fires at a time.
 */

const TOKEN_STORAGE_KEY = 'base44_access_token';
const LEGACY_TOKEN_KEY = 'token';
const REFRESH_TOKEN_KEY = 'refresh_token';
const APP_ID_STORAGE_KEY = 'base44_app_id';

export function getAuthToken() {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY) || null;
  } catch {
    return null;
  }
}

// Module-level guard: a burst of simultaneous 401s should trigger exactly one
// refresh, not N parallel rotations (which would burn through refresh tokens
// and likely fail on replay-protection).
let _refreshInFlight = null;

async function refreshAccessToken() {
  if (_refreshInFlight) return _refreshInFlight;
  let refreshToken = null;
  try {
    refreshToken = window.localStorage.getItem(REFRESH_TOKEN_KEY);
  } catch {
    return null;
  }
  if (!refreshToken) return null;

  _refreshInFlight = (async () => {
    try {
      // appParams stores the active app id under base44_app_id; fall back to
      // the build-time env var if it isn't in storage yet.
      let appId = null;
      try {
        appId = window.localStorage.getItem(APP_ID_STORAGE_KEY);
      } catch {
        /* ignore */
      }
      if (!appId) {
        appId = import.meta.env.VITE_BASE44_APP_ID;
      }
      if (!appId) return null;

      const resp = await fetch(`/api/apps/${appId}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!resp.ok) return null;
      const data = await resp.json();
      const access = data.access_token;
      if (!access) return null;
      try {
        window.localStorage.setItem(TOKEN_STORAGE_KEY, access);
        // Keep the legacy key in sync so the base44 SDK (which may read
        // 'token') sees the rotated token too.
        window.localStorage.setItem(LEGACY_TOKEN_KEY, access);
        window.localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
      } catch {
        /* ignore storage failures */
      }
      return access;
    } catch {
      return null;
    } finally {
      _refreshInFlight = null;
    }
  })();
  return _refreshInFlight;
}

/**
 * Same as `authFetch` but keeps the response body as a `ReadableStream`
 * (the default `Response` in `fetch` is already a stream — we just promise
 * not to consume it via `.json()` first).
 *
 * Useful for `InvokeLLMStream` and other SSE endpoints.
 */
export async function authStreamingFetch(url, options = {}) {
  return authFetch(url, options);
}

/**
 * Attach the Authorization header (if a token is available) to the request
 * and forward to `fetch`. On a 401 from our own API, transparently refresh
 * the access token once and retry. We never strip headers the caller already
 * set — we just add ours on top.
 *
 * @param {string} url
 * @param {RequestInit} [options]
 * @returns {Promise<Response>}
 */
export async function authFetch(url, options = {}) {
  const token = getAuthToken();
  const headers = new Headers(options.headers || {});

  // Only attach the header when we actually have a token. Endpoints that
  // need to work while logged-out (login, public settings, OAuth start)
  // simply omit the header — that matches the SDK's behaviour.
  if (token && !headers.has('Authorization') && !headers.has('authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  const response = await fetch(url, { ...options, headers });

  // Mid-session token expiry. Only retry for same-origin /api routes, and
  // only when the request body can be safely re-sent (string/undefined — a
  // ReadableStream body can't be replayed). One retry max prevents loops.
  if (
    response.status === 401 &&
    typeof url === 'string' &&
    url.startsWith('/api/') &&
    (!options.body || typeof options.body === 'string')
  ) {
    const newToken = await refreshAccessToken();
    if (newToken) {
      const retryHeaders = new Headers(options.headers || {});
      retryHeaders.set('Authorization', `Bearer ${newToken}`);
      return fetch(url, { ...options, headers: retryHeaders });
    }
  }

  return response;
}

export default authFetch;
