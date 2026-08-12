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
