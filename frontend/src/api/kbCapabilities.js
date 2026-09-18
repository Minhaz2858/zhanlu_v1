/**
 * DataSource capability manifest — the UI's single source of truth for which
 * database dialects can be connected to / catalogued.
 *
 * The backend owns this list (`GET /apps/{app_id}/knowledge_bases/capabilities`,
 * derived from connector_factory._REGISTRY). Never hardcode a dialect array in
 * the frontend: adding a dialect on the backend must light up the UI with no
 * frontend change, and a dialect that the backend supports must never be
 * skipped by a stale frontend list.
 */
import { authFetch } from './authFetch';

let _cache = null;
let _inflight = null;

const capsUrl = (appId) =>
  `/api/apps/${appId || 'default-app'}/knowledge_bases/capabilities`;

/** Fetch (and memoize) the capability manifest. Returns null when unavailable. */
export async function getKbCapabilities(appId) {
  if (_cache) return _cache;
  if (_inflight) return _inflight;
  _inflight = (async () => {
    try {
      const res = await authFetch(capsUrl(appId));
      if (!res.ok) return null;
      const data = await res.json();
      if (data && Array.isArray(data.catalog_db_types)) _cache = data;
    } catch {
      /* fail-open — see isCatalogableDbType */
    } finally {
      _inflight = null;
    }
    return _cache;
  })();
  return _inflight;
}

/** Drop the memoized manifest (tests / after a backend capability change). */
export function resetKbCapabilitiesCache() {
  _cache = null;
  _inflight = null;
}

/**
 * Can this dialect be catalogued (semantic catalog indexed)?
 *
 * FAILS OPEN: when the manifest can't be fetched we return true so the request
 * still goes to the backend, which is the real authority — a stale or missing
 * frontend list must never be the thing that blocks a user's database from
 * being indexed.
 */
export async function isCatalogableDbType(dbType, appId) {
  const caps = await getKbCapabilities(appId);
  if (!caps) return true;
  if (caps.catalog_enabled === false) return false;
  if (!caps.catalog_db_types.length) return false;
  return caps.catalog_db_types.includes(String(dbType || '').toLowerCase());
}
