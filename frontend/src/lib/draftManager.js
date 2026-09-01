/**
 * Lightweight localStorage draft manager with debounced writes.
 *
 * Key pattern:  draft:{key}  — caller supplies a short string key, e.g.
 * "agent_steps_builder_form" or "agent_steps_builder_config".
 */

const PREFIX = 'draft:';

function fullKey(k) {
  return PREFIX + k;
}

let timers = {};

/**
 * Save `data` to localStorage. When `debounceMs` > 0 the actual write is
 * delayed — repeated calls with the same key reset the timer, so only the
 * last value within the window is persisted.
 */
export function saveDraft(key, data, debounceMs = 300) {
  const k = fullKey(key);
  if (timers[k]) clearTimeout(timers[k]);
  if (debounceMs > 0) {
    timers[k] = setTimeout(() => {
      try { localStorage.setItem(k, JSON.stringify(data)); } catch (_) {}
      delete timers[k];
    }, debounceMs);
  } else {
    try { localStorage.setItem(k, JSON.stringify(data)); } catch (_) {}
  }
}

/** Flush any pending debounced saves immediately. */
export function flushDraft(key) {
  const k = fullKey(key);
  if (timers[k]) {
    clearTimeout(timers[k]);
    delete timers[k];
  }
}

/** Return the parsed draft or `null` when nothing is stored / JSON parse fails. */
export function loadDraft(key) {
  const k = fullKey(key);
  try {
    const raw = localStorage.getItem(k);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/** Remove a single draft entry. */
export function clearDraft(key) {
  const k = fullKey(key);
  if (timers[k]) {
    clearTimeout(timers[k]);
    delete timers[k];
  }
  try { localStorage.removeItem(k); } catch (_) {}
}

/**
 * True when a draft exists for `key` AND contains at least one non-empty
 * value. Used by the "Resume draft?" badge in MySpace so the user can see
 * the location of an unfinished creation even if the StepsAgentBuilder
 * hasn't been opened yet.
 */
export function hasDraft(key) {
  const data = loadDraft(key);
  if (!data || typeof data !== 'object') return false;
  // Consider the draft "real" when at least one string field has content.
  return Object.values(data).some((v) => {
    if (typeof v === 'string') return v.trim().length > 0;
    if (Array.isArray(v)) return v.length > 0;
    if (v && typeof v === 'object') return Object.keys(v).length > 0;
    return Boolean(v);
  });
}

/**
 * Lightweight summary of a draft for badge/UI display. Returns the first
 * non-empty string field, falling back to the key name. Returns `null` when
 * there is no usable draft content.
 */
export function getDraftSummary(key, fallbackLabel = '') {
  const data = loadDraft(key);
  if (!data || typeof data !== 'object') return null;
  for (const v of Object.values(data)) {
    if (typeof v === 'string' && v.trim().length > 0) return v.trim();
  }
  return fallbackLabel || null;
}

/** Batch-clear entries whose keys match `pattern` (prefix match). */
export function clearDraftsByPrefix(prefix) {
  const search = PREFIX + prefix;
  const keys = Object.keys(localStorage).filter((k) => k.startsWith(search));
  keys.forEach((k) => {
    try { localStorage.removeItem(k); } catch (_) {}
  });
}
