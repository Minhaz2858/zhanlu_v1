/**
 * Observed-execution aggregation for the process flow canvas (Phase 6A).
 *
 * Pure functions — no fetching, no React. Aggregates the process-level
 * observation summary (already embedded in the process detail response as
 * `observed_executions`) and the payload-free references from the
 * ONE-per-canvas observation endpoint into per-activity views.
 *
 * HONESTY CONTRACT:
 * - observed ≠ documented ≠ configured — this module NEVER merges the
 *   dimensions; it only reports what executions matched.
 * - missing confidence stays "unknown" — never fabricated, never inferred
 *   from documentation.
 * - references carry NO execution payloads (no SQL, no results, no
 *   prompts) — the backend contract already guarantees this.
 */

export function activityObservation(summary, references, seq) {
  const key = String(seq);
  const refs = (Array.isArray(references) ? references : [])
    .filter((r) => String(r?.activity_seq) === key);
  const count = refs.length ||
    Number((summary?.activity_counts || {})[key] || 0);
  if (!count) {
    return { observed: false, count: 0, max_confidence: null,
             band: 'unknown', latest_observed_at: null, methods: [],
             record_types: [] };
  }
  const confs = refs.map((r) => r?.match_confidence)
    .filter((c) => typeof c === 'number');
  const max_confidence = confs.length ? Math.max(...confs) : null;
  const band = max_confidence === null
    ? 'unknown'
    : (max_confidence >= 0.8 ? 'high'
        : (max_confidence >= 0.5 ? 'medium' : 'low'));
  const times = refs.map((r) => r?.observed_at).filter(Boolean).sort();
  const methods = [...new Set(refs.map((r) => r?.match_method).filter(Boolean))];
  const record_types = [...new Set(refs.map((r) => r?.record_type).filter(Boolean))];
  return {
    observed: true,
    count: refs.length || count,
    max_confidence,
    band,
    latest_observed_at: times.length ? times[times.length - 1] : null,
    methods,
    record_types,
  };
}

export function confidenceLabel(band) {
  switch (band) {
    case 'high': return 'high confidence';
    case 'medium': return 'medium confidence';
    case 'low': return 'low confidence';
    default: return 'confidence unknown';
  }
}
