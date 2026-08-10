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
 * erroring every 15 minutes. The refresh is delegated to the shared
 * `rotateAccessToken` util in `api/tokenRefresh.js`, which is also the
 * single entry point used by `AuthContext` — so concurrent 401 bursts in
 * either code path share the same in-flight promise and we never race on
 * the one-time-use refresh token.
 */

import { rotateAccessToken } from '@/api/tokenRefresh';

const TOKEN_STORAGE_KEY = 'base44_access_token';
const REFRESH_TOKEN_KEY = 'refresh_token';

export function getAuthToken() {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY) || null;
  } catch {
    return null;
  }
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
    const newToken = await rotateAccessToken();
    if (newToken) {
      const retryHeaders = new Headers(options.headers || {});
      retryHeaders.set('Authorization', `Bearer ${newToken}`);
      return fetch(url, { ...options, headers: retryHeaders });
    }
  }

  return response;
}

export default authFetch;

/**
 * Thrown by `authFetchOrThrow` when a request comes back 401 and the silent
 * refresh attempt also fails. Callers (e.g. the streaming chat) catch this
 * to surface "Session expired, please log in again" instead of the generic
 * "Sorry, the connection was interrupted" message that's only appropriate
 * for true network/stream failures.
 *
 * The base44 SDK throws opaque 401s from `axios` too — but for streaming
 * responses we use raw `fetch` and need a typed signal.
 */
export class SessionExpiredError extends Error {
  constructor(message = 'Session expired', originalError = null) {
    super(message);
    this.name = 'SessionExpiredError';
    // Preserve a stack + the underlying error for the browser console so
    // debugging "why am I seeing this" is one click away.
    if (originalError) this.cause = originalError;
    this.sessionExpired = true;
  }
}

/**
 * Same as `authFetch` but throws a typed `SessionExpiredError` when the
 * silent refresh-and-retry doesn't recover the caller's auth. Use this
 * helper for streaming endpoints where a 401-with-no-recovery needs to be
 * surfaced distinctly from a transient network failure (the chat UI's
 * catch block treats the two cases very differently: one persists
 * "Sorry, the connection was interrupted", the other shows a "Please log
 * in again" banner and triggers logout).
 *
 * Existing non-streaming call sites can keep using `authFetch` (they
 * normally 401-handle themselves and we don't want to change behaviour).
 */
export async function authFetchOrThrow(url, options = {}) {
  const response = await authFetch(url, options);
  // Non-401: return as-is. Stream body readers need the raw Response.
  if (response.status !== 401) return response;
  // 401 on /api/ routes that have a non-readable body (string/none) — authFetch
  // already attempted one silent refresh. If we still have 401, the refresh
  // returned null (refresh_token consumed / missing / revoked). Surface it.
  if (
    typeof url === 'string' &&
    url.startsWith('/api/') &&
    (!options.body || typeof options.body === 'string')
  ) {
    // One more chance: maybe a parallel call in flight already refreshed.
    // Check localStorage — if a newer refresh_token exists, the refresh did
    // succeed for another in-flight caller; the original 401 was just a
    // race. Return the response unchanged and let the caller decide.
    try {
      if (window.localStorage.getItem(REFRESH_TOKEN_KEY)) {
        // The refresh module already cleared _refreshInFlight by now; if the
        // global state has a refresh_token, retry once more with the new one.
        const freshToken = window.localStorage.getItem(TOKEN_STORAGE_KEY);
        if (freshToken) {
          const retryHeaders = new Headers(options.headers || {});
          retryHeaders.set('Authorization', `Bearer ${freshToken}`);
          const retryResp = await fetch(url, { ...options, headers: retryHeaders });
          if (retryResp.status !== 401) return retryResp;
        }
      }
    } catch {
      /* ignore — fall through to throw */
    }
    throw new SessionExpiredError();
  }
  return response;
}
