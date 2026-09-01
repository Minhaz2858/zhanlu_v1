// Shared number formatting for dashboard widgets. Intl instances are created
// once at module scope — formatting is O(1) per call.

const compactFmt = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const fullFmt = new Intl.NumberFormat('en', { maximumFractionDigits: 2 });

const COMPACT_THRESHOLD = 10000;

export function formatMetric(value, { unit, compact = true } = {}) {
  if (value == null || value === '') return '—';
  const num = Number(value);
  const base = Number.isFinite(num)
    ? compact && Math.abs(num) >= COMPACT_THRESHOLD
      ? compactFmt.format(num)
      : fullFmt.format(num)
    : String(value);
  return unit ? `${base} ${unit}` : base;
}

export function formatAxisTick(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value ?? '');
  return Math.abs(num) >= COMPACT_THRESHOLD ? compactFmt.format(num) : fullFmt.format(num);
}

// Returns { pct, direction } for a KPI delta chip, or null when the inputs
// are not comparable. pct is absolute, rounded to 1 decimal.
export function formatDelta(current, compare) {
  const cur = Number(current);
  const cmp = Number(compare);
  if (!Number.isFinite(cur) || !Number.isFinite(cmp) || cmp === 0) return null;
  const pct = ((cur - cmp) / Math.abs(cmp)) * 100;
  const direction = Math.abs(pct) < 0.05 ? 'flat' : pct > 0 ? 'up' : 'down';
  return { pct: Math.round(Math.abs(pct) * 10) / 10, direction };
}
