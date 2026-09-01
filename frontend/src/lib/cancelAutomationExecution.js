// Cancel API for automation executions.
//
// ``POST /api/automations/executions/{id}/cancel`` is idempotent: cancelling
// an already-cancelled or already-terminal run is a no-op (the backend
// returns 200 with ``already_terminal=true``). The frontend uses this as
// the Stop button handler — wiring it through authFetch so the standard
// 401-refresh + X-Org-Id headers apply.
//
// Returning a small result object instead of throwing keeps the
// ChatInput's stop click handler one-liner clean and lets the UI log a
// soft error if the call fails (e.g. network down) without locking the
// user out of a retry.

/**
 * @param {string} executionId - The AutomationExecution.id to cancel.
 * @param {{ authFetch?: Function, base44?: any }} opts
 * @returns {Promise<
 *   | { ok: true, status: string, alreadyTerminal: boolean, delivered: boolean }
 *   | { ok: false, error: string }
 * >}
 */
export async function cancelAutomationExecution(executionId, opts = {}) {
  if (!executionId) {
    return { ok: false, error: 'missing execution id' };
  }
  const { authFetch } = opts;
  if (typeof authFetch !== 'function') {
    return { ok: false, error: 'authFetch not available' };
  }
  try {
    const res = await authFetch(
      `/api/automations/executions/${encodeURIComponent(executionId)}/cancel`,
      { method: 'POST' },
    );
    // The backend returns the JSON body directly on 2xx. authFetch may
    // throw on non-2xx — but the cancel endpoint returns 200 even for
    // already-terminal / not-found cases (404 only for truly unknown
    // ids). Treat HTTP errors as soft failures.
    const body = res && typeof res === 'object' ? res : {};
    return {
      ok: true,
      status: body.status || 'cancelled',
      alreadyTerminal: Boolean(body.already_terminal),
      delivered: Boolean(body.delivered),
    };
  } catch (e) {
    return {
      ok: false,
      error: (e && (e.message || String(e))) || 'cancel request failed',
    };
  }
}
