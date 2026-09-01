/**
 * LiveActivityStream.legacyAdapter
 * --------------------------------
 * Pure conversion from the legacy `activity_steps` shape to the new
 * `live_events` shape so every assistant message — past or future — renders
 * through the same `LiveActivityStream` component.
 *
 * Legacy step shape (see `ActivitySteps.jsx` docs):
 *   { number, description, status: "running"|"done"|"failed",
 *     tool_name?, command?, output_preview?, artifact_id?,
 *     row_count?, duration?, ts?? }
 *
 * New live-event shape:
 *   { type, label_key, params, ts, _legacy?: true }
 *
 * Mapping rules
 *   - Each legacy step becomes ONE typed event:
 *       status "done"      → tool_call_finished
 *       status "failed"    → tool_call_failed
 *       status "running"   → tool_call_started  (orphan; never matched by finish)
 *       anything else      → tool_call_started  (treated as in-progress)
 *   - Optional numeric `row_count` and `duration` pass through into the new
 *     structured params so the renderer can surface them in the row meta.
 *   - We DO NOT synthesize phase_enter events from the legacy `phase` prop —
 *     the consumer can derive the headline from `phase` directly. Synthesizing
 *     phantom phase transitions would over-promise activity that never ran.
 *
 * The adapter is pure (no I/O) and exports a single function. Tests for it
 * live in `LiveActivityStream.test.jsx` (`describe('legacy adapter')`).
 */

/**
 * @param {Array<{number?: number, description?: string, status?: string,
 *   tool_name?: string, row_count?: number, duration?: number, ts?: string}>} steps
 * @returns {Array<{type: string, label_key: string, params: object, _legacy: boolean}>}
 */
export function synthesizeLegacySteps(steps) {
  if (!Array.isArray(steps) || steps.length === 0) return [];

  const out = [];
  for (let i = 0; i < steps.length; i++) {
    const step = steps[i];
    if (!step) continue;
    const status = String(step.status || 'running');
    const isDone = status === 'done';
    const isFailed = status === 'failed';
    const type = isDone
      ? 'tool_call_finished'
      : isFailed
        ? 'tool_call_failed'
        : 'tool_call_started';
    const labelKey = isDone
      ? 'tool_call_finished'
      : isFailed
        ? 'tool_call_failed'
        : 'tool_call_started';
    const params = {
      tool_label:
        step.description || step.tool_name || (step.number != null ? `step ${step.number}` : `step ${i + 1}`),
    };
    if (typeof step.row_count === 'number' && step.row_count >= 0) {
      params.row_count = step.row_count;
    }
    if (typeof step.duration === 'number' && step.duration >= 0) {
      params.duration = step.duration;
    }
    out.push({
      type,
      label_key: labelKey,
      params,
      ts: step.ts || step.started_at || new Date().toISOString(),
      _legacy: true,
    });
  }
  return out;
}

/**
 * Convenience selector: pick the better of `live_events` vs `legacySteps`.
 * `live_events` wins (the modern source). We only fall back to the
 * synthesizer when `live_events` is missing or empty.
 *
 * @param {Array|undefined|null} liveEvents
 * @param {Array|undefined|null} legacySteps
 * @returns {Array}
 */
export function pickEvents(liveEvents, legacySteps) {
  if (Array.isArray(liveEvents) && liveEvents.length > 0) return liveEvents;
  return synthesizeLegacySteps(legacySteps);
}