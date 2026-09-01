import { createClient } from '@base44/sdk';
import { appParams } from '@/lib/app-params';
import { rotateAccessToken } from '@/api/tokenRefresh';

const { appId, token, functionsVersion, appBaseUrl } = appParams;

// Underlying SDK client. We keep a reference so the 401-retry Proxy can
// call straight back into it.
const rawClient = createClient({
  appId,
  token,
  functionsVersion,
  serverUrl: '',
  requiresAuth: false,
  appBaseUrl,
});

/**
 * Return true for any thrown value that the base44 SDK historically uses
 * to signal "your bearer token is no good". The SDK wraps axios errors
 * with `.status` and `.response.status`; some older paths throw a raw
 * `Base44Error`. Be permissive.
 */
function is401(err) {
  if (!err) return false;
  if (err.status === 401) return true;
  if (err.response && err.response.status === 401) return true;
  if (err.code === 'unauthorized') return true;
  return false;
}

/**
 * Wrap a function/SDK method so that, on a 401 from the underlying call,
 * it awaits a single in-flight refresh and retries the call once. Any
 * other error (or the post-refresh 401) is propagated untouched so the
 * caller can react.
 */
function wrapWith401Retry(fn) {
  return function wrapped(...args) {
    const original = fn.apply(this, args);
    // Promise.resolve guards against sync throws from the SDK.
    return Promise.resolve(original).catch(async (err) => {
      if (!is401(err)) throw err;

      const newToken = await rotateAccessToken();
      if (!newToken) {
        // Refresh failed (no refresh token / replay rejected / network).
        // Re-throw the original 401 so the caller's downstream code can
        // decide what to do (show "Session expired", redirect to login, …).
        throw err;
      }

      // Retry once. If this also returns 401, the caller sees it — we
      // don't loop forever, otherwise a permanent token revocation would
      // pin the browser at 100% CPU.
      const retry = fn.apply(this, args);
      return await retry;
    });
  };
}

/**
 * Build a Proxy that wraps every property-access on `target` such that
 * callable methods get the 401-retry treatment, while plain values
 * (entity names, function names, …) pass through untouched. Nested
 * objects (e.g. entities.ChatSession, functions.someFunc) are wrapped
 * lazily so the drop-in shape is preserved.
 */
function create401RetryProxy(target) {
  // Cache proxy-by-target to avoid infinite recursion when the SDK's
  // own getters return the same nested object on every property access.
  const cache = new WeakMap();
  if (cache.has(target)) return cache.get(target);
  // Don't try to proxy primitives or DOM nodes that snuck in.
  if (target === null || typeof target !== 'object') return target;

  const proxy = new Proxy(target, {
    get(obj, prop, receiver) {
      const value = Reflect.get(obj, prop, receiver);
      if (typeof value === 'function') {
        return wrapWith401Retry(value.bind(obj));
      }
      if (value && typeof value === 'object') {
        return create401RetryProxy(value);
      }
      return value;
    },
  });
  cache.set(target, proxy);
  return proxy;
}

export const base44 = create401RetryProxy(rawClient);

export default base44;
