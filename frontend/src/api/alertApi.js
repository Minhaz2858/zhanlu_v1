/** API client for the Ecisco BI proactive market alert system.

Uses the canonical authFetch pattern (auto token + one-time 401 refresh).
*/

import { authFetch } from '@/api/authFetch';

/**
 * Fetch active (non-dismissed) alerts and unread count.
 * @returns {{ alerts: Array, unread_count: number }}
 */
export async function getAlerts() {
  const resp = await authFetch('/api/alerts');
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`getAlerts failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Mark all unread alerts as read (per org).
 * @returns {{ marked_read: number }}
 */
export async function markAlertsRead() {
  const resp = await authFetch('/api/alerts/read', { method: 'POST' });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`markAlertsRead failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Dismiss a single alert by ID.
 * @param {string} alertId
 * @returns {{ dismissed: string }}
 */
export async function dismissAlert(alertId) {
  const resp = await authFetch(`/api/alerts/dismiss/${alertId}`, { method: 'POST' });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`dismissAlert failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Trigger a manual alert scan cycle.
 * @returns {{ scanned_at, new_alerts, total_scanned, error }}
 */
export async function scanAlerts() {
  const resp = await authFetch('/api/alerts/scan', { method: 'POST' });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`scanAlerts failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Fetch pipeline diagnostics (flag state, warehouse reachability, per-product
 * probe, would-trigger evaluation). Gated by auth only — works even when the
 * alerts flag is off so the UI can explain the off state.
 * @returns {{
 *   flags: {ecisco_bi_alerts_enabled: boolean, alert_digest_enabled: boolean},
 *   warehouse: {configured: boolean, reachable: boolean},
 *   thresholds: {price_spike: number, price_drop: number, high_severity: number},
 *   effective_org_id: string,
 *   products: Array,
 *   triggering_products: Array<string>,
 * }}
 */
export async function getAlertDiagnostics() {
  const resp = await authFetch('/api/alerts/diagnostics');
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`getAlertDiagnostics failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Manually trigger a CEO morning digest build.
 * @returns {Object} The created digest AlertLog
 */
export async function triggerDigest() {
  const resp = await authFetch('/api/alerts/digest', { method: 'POST' });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`triggerDigest failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Fetch the current user's alert preferences.
 * @returns {Promise<{
 *   min_severity: 'low'|'medium'|'high',
 *   muted_products: string[],
 *   channels: string[],
 *   threshold_multiplier: number,
 *   quiet_hours: {enabled: boolean, start: string, end: string}|null,
 *   recommendations_enabled: boolean,
 *   webhook_url: string|null,
 *   webhook_secret: string|null,
 * }>}
 */
export async function getAlertPreferences() {
  const resp = await authFetch('/api/users/me/alert-preferences');
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`getAlertPreferences failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Partially update the current user's alert preferences.
 * @param {Object} patch — subset of the preference fields
 * @returns {Promise<Object>} The full updated snapshot
 */
export async function updateAlertPreferences(patch) {
  const resp = await authFetch('/api/users/me/alert-preferences', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`updateAlertPreferences failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

/**
 * Mute a product so its alerts are suppressed.
 * @param {string} productId
 * @param {string[]} currentMuted — the current muted list (to append, not replace)
 * @returns {Promise<Object>} The full updated snapshot
 */
export async function muteProduct(productId, currentMuted = []) {
  return updateAlertPreferences({
    muted_products: [...currentMuted, productId],
  });
}
