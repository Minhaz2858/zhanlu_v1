// forecastChartMath — pure helpers for ForecastTrendChart.
//
// Two responsibilities:
//  1. Y-axis: compute a tight domain with "nice" round-step ticks, ignoring
//     null / non-finite / non-positive sentinel values (bad backdated runs
//     sometimes emit bear ≈ 0, which used to stretch the axis to $0–$115).
//  2. Boundary anchoring (old-EDIA behaviour): when the backend emits no
//     "today" bridge point (last actual lags calendar today), the forecast
//     fan would start floating several ticks after the last actual. We
//     re-anchor the fan at the LAST ACTUAL point — seeding its
//     base/bull/bear from the actual — so bull/base/bear lines physically
//     connect to the tip of the actual line with no gap.

const VALUE_KEYS = [
  'actual', 'base', 'bull', 'bear', 'previous_ai',
  'prev_ai_low', 'prev_ai_high', 'exec_in', 'exec_out',
];

function niceNum(range, round) {
  if (!(range > 0)) return 1;
  const exponent = Math.floor(Math.log10(range));
  const fraction = range / 10 ** exponent;
  let niceFraction;
  if (round) {
    if (fraction < 1.5) niceFraction = 1;
    else if (fraction < 3) niceFraction = 2;
    else if (fraction < 7) niceFraction = 5;
    else niceFraction = 10;
  } else {
    if (fraction <= 1) niceFraction = 1;
    else if (fraction <= 2) niceFraction = 2;
    else if (fraction <= 5) niceFraction = 5;
    else niceFraction = 10;
  }
  return niceFraction * 10 ** exponent;
}

/**
 * Tight axis bracket with evenly spaced, human-round ticks.
 * Returns { domain: [lo, hi], ticks: number[] }.
 */
export function niceTicks(lo, hi, maxTicks = 6) {
  let low = Number(lo);
  let high = Number(hi);
  if (!Number.isFinite(low) || !Number.isFinite(high)) {
    return { domain: ['auto', 'auto'], ticks: undefined };
  }
  if (low > high) [low, high] = [high, low];
  if (low === high) {
    const spread = Math.max(Math.abs(low) * 0.02, 1);
    low -= spread;
    high += spread;
  }
  const range = niceNum(high - low, false);
  const step = niceNum(range / Math.max(2, maxTicks - 1), true);
  const tickLo = Math.floor(low / step) * step;
  const tickHi = Math.ceil(high / step) * step;
  const ticks = [];
  for (let v = tickLo; v <= tickHi + step / 2; v += step) {
    ticks.push(Number(v.toFixed(10)));
  }
  return { domain: [ticks[0], ticks[ticks.length - 1]], ticks };
}

/**
 * Domain + ticks across all rendered series, dropping sentinel values
 * (null, NaN, <= 0). Falls back to recharts 'auto' when nothing usable.
 */
export function computeYAxis(points, maxTicks = 6) {
  const vals = [];
  for (const p of points || []) {
    for (const k of VALUE_KEYS) {
      const v = Number(p?.[k]);
      if (p?.[k] != null && Number.isFinite(v) && v > 0) vals.push(v);
    }
  }
  if (!vals.length) return { domain: ['auto', 'auto'], ticks: undefined };
  return niceTicks(Math.min(...vals), Math.max(...vals), maxTicks);
}

/**
 * Re-anchor the forecast fan at the last actual point (old-EDIA style).
 *
 * - Finds the last point with an actual price before the first forecast
 *   point and promotes it to zone 'today', seeding base/bull/bear from the
 *   actual so the fan emanates from the actual line's tip.
 * - A backend-emitted 'today' bridge point is left untouched.
 * - Past-only products (no forecast yet) pass through unchanged.
 *
 * Returns { points, hasForecast }. Input is not mutated.
 */
export function normalizeForecastPoints(rawPoints) {
  const points = (rawPoints || []).map((p) => ({ ...p }));
  const firstForecastIdx = points.findIndex((p) => p.zone === 'forecast');
  const hasForecast = firstForecastIdx >= 0;
  if (!hasForecast) return { points, hasForecast };

  const hasTodayBridge = points.some((p) => p.zone === 'today');
  if (hasTodayBridge) return { points, hasForecast };

  // Last actual at or before the forecast start becomes the anchor.
  let anchorIdx = -1;
  for (let i = 0; i < firstForecastIdx; i++) {
    if (points[i].actual != null) anchorIdx = i;
  }
  if (anchorIdx < 0) return { points, hasForecast };

  const a = points[anchorIdx];
  const seeded = { ...a, zone: 'today' };
  if (seeded.base == null) seeded.base = a.actual;
  if (seeded.bull == null) seeded.bull = a.actual;
  if (seeded.bear == null) seeded.bear = a.actual;
  points[anchorIdx] = seeded;
  return { points, hasForecast };
}
