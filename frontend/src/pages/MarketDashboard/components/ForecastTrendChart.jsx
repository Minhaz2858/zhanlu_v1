// ForecastTrendChart — EDIA overlay-chart style with step-function AI review.
// Past zone: actual (royal-blue) + step-function previous-AI (orange dashed) +
//   past-range band + executed-in/out scatter dots.
// Forecast zone: bull / base / bear dotted trio fanning from today; no green band.
// Header: zone pill bars, legend with live executed counts.
import { useEffect, useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer, ComposedChart, Line, Area, XAxis, YAxis,
  Tooltip, ReferenceLine, ReferenceDot, CartesianGrid,
} from 'recharts';
import { RefreshCw, Activity, Zap, Layers } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import { getChartTheme } from '../lib/chartTheme';
import { computeYAxis, normalizeForecastPoints } from '../lib/forecastChartMath';

function useDarkMode() {
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const update = () => setIsDark(document.documentElement.classList.contains('dark'));
    update();
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  }, []);
  return isDark;
}

const RANGES = [7, 15, 30];

// EDIA overlay-chart palette
const C = {
  actual: '#2563eb',        // royal blue — actual price line
  previousAi: '#f59e0b',    // amber — previous AI dashed
  pastBand: '#bfdbfe',      // past-zone AI range band fill
  bull: '#10b981',          // emerald — bull (upper)
  base: '#60a5fa',          // blue — base (middle)
  bear: '#f87171',          // red — bear (lower)
  execIn: '#16a34a',        // green — executed in range
  execOut: '#dc2626',       // red — executed outside range
  todayDot: '#16a34a',      // green today marker
  forecastBand: '#bbf7d0',  // forecast wash fill (very subtle)
};

function fmtPrice(v) {
  if (v == null) return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

// MM-DD format: "2026-08-03" → "08-03"
function fmtDateMMDD(iso) {
  if (!iso) return '';
  try {
    const parts = iso.split('-');
    if (parts.length === 3) return `${parts[1]}-${parts[2]}`;
    const d = new Date(iso + 'T00:00:00');
    const mo = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${mo}-${day}`;
  } catch { return iso; }
}

function fmtDateShort(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  } catch { return iso; }
}

export default function ForecastTrendChart({ productId, productName }) {
  const [days, setDays] = useState(30);
  const isDark = useDarkMode();
  const chartTheme = getChartTheme(isDark);

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['market-dashboard', 'forecast-chart', productId, days],
    queryFn: () => marketDashboardApi.forecastChart(productId, days),
    staleTime: 60_000,
    enabled: !!productId,
  });

  const [showSkuPopover, setShowSkuPopover] = useState(false);

  // Fetch SKU list for ERP products (c5_resin, raffinate_c5)
  const isErpProduct = productId === 'c5_resin' || productId === 'raffinate_c5';
  const { data: skuData } = useQuery({
    queryKey: ['market-dashboard', 'forecast-skus', productId],
    queryFn: () => marketDashboardApi.forecastListSkus(productId),
    staleTime: 300_000,  // 5 min — SKU list doesn't change often
    enabled: !!productId && isErpProduct,
  });

  const skuList = skuData?.skus || [];
  const primarySku = skuList.find(s => s.is_primary) || skuList[0];
  const skuCount = skuList.length;

  const rawPoints = useMemo(() => data?.points || [], [data]);

  // Old-EDIA boundary anchoring: when the backend emits no "today" bridge
  // point, the last actual is promoted to the zone boundary and seeds the
  // bull/base/bear fan — so the forecast lines connect to the actual line's
  // tip with no gap. Past-only products pass through unchanged.
  const { points: normalizedPoints, hasForecast } = useMemo(
    () => normalizeForecastPoints(rawPoints),
    [rawPoints],
  );

  // Enrich points for stacked-area band rendering.
  // past zone: prevAiLow (transparent) + prevAiRange (visible fill)
  // forecast zone: forecastBear (transparent) + forecastRange (subtle fill)
  const points = useMemo(
    () =>
      normalizedPoints.map((p) => {
        const inPast = p.prev_ai_low != null || p.prev_ai_high != null;
        const inForecast = p.zone === 'forecast' || p.zone === 'today';
        return {
          ...p,
          // Past AI range band (stacked area)
          prevAiLow: inPast ? p.prev_ai_low : null,
          prevAiRange:
            p.prev_ai_low != null && p.prev_ai_high != null
              ? Math.max(0, p.prev_ai_high - p.prev_ai_low)
              : null,
          // Forecast range band (stacked area, subtle) — starts at the
          // boundary anchor so the wash band also emanates from the tip.
          forecastBear:
            inForecast && p.bear != null ? p.bear : null,
          forecastRange:
            inForecast && p.bull != null && p.bear != null
              ? Math.max(0, p.bull - p.bear)
              : null,
          // Executed dots — sparse, only where value exists
          execInDot: p.exec_in,
          execOutDot: p.exec_out,
        };
      }),
    [normalizedPoints],
  );

  const hasPastAi = useMemo(
    () => points.some((p) => p.previous_ai != null || p.prevAiRange != null),
    [points],
  );

  const todayIdx = useMemo(
    () => points.findIndex((p) => p.zone === 'today' || p.zone === 'forecast'),
    [points],
  );
  const todayPoint = todayIdx >= 0 ? points[todayIdx] : null;
  const todayValue = todayPoint?.actual ?? todayPoint?.base ?? null;

  const firstForecastIdx = useMemo(
    () => points.findIndex((p) => p.zone === 'forecast'),
    [points],
  );

  // Y-axis: tight domain + evenly spaced round-step ticks; null/0/negative
  // sentinel values from bad runs are ignored so the axis hugs real prices.
  const yAxis = useMemo(() => computeYAxis(points), [points]);

  // Tooltip
  const CustomTooltip = ({ active, payload, label }) => {
    if (!active || !payload?.length) return null;
    const p = rawPoints.find((pt) => pt.date === label) || {};
    return (
      <div
        className="min-w-[240px] rounded-md border px-3 py-2 text-xs shadow-lg"
        style={{
          backgroundColor: isDark ? '#1e293b' : '#fff',
          borderColor: chartTheme.axis,
        }}
      >
        <div className="mb-1.5 font-semibold text-slate-700 dark:text-slate-200">
          {fmtDateMMDD(label)}
        </div>
        {p.actual != null && (
          <div className="flex justify-between gap-4 leading-relaxed">
            <span style={{ color: C.actual }}>{productName || productId}</span>
            <span className="tabular-nums font-mono">${fmtPrice(p.actual)}</span>
          </div>
        )}
        {p.previous_ai != null && (
          <div className="flex justify-between gap-4 leading-relaxed">
            <span style={{ color: C.previousAi }} className="flex-1 pr-2">
              Previous AI (made {fmtDateShort(data?.previous_ai_date)})
            </span>
            <span className="tabular-nums font-mono whitespace-nowrap">${fmtPrice(p.previous_ai)}</span>
          </div>
        )}
        {p.exec_in != null && (
          <div className="flex justify-between gap-4 leading-relaxed">
            <span style={{ color: C.execIn }}>✓ Executed (AI: {fmtPrice(p.prev_ai_low)}–{fmtPrice(p.prev_ai_high)})</span>
            <span className="tabular-nums font-mono">${fmtPrice(p.exec_in)}</span>
          </div>
        )}
        {p.exec_out != null && (
          <div className="flex justify-between gap-4 leading-relaxed">
            <span style={{ color: C.execOut }}>✗ Executed outside range</span>
            <span className="tabular-nums font-mono">${fmtPrice(p.exec_out)}</span>
          </div>
        )}
        {p.base != null && (
          <div className="flex justify-between gap-4 leading-relaxed">
            <span style={{ color: C.base }}>Base</span>
            <span className="tabular-nums font-mono">${fmtPrice(p.base)}</span>
          </div>
        )}
        {p.bull != null && (
          <div className="flex justify-between gap-4 leading-relaxed">
            <span style={{ color: C.bull }}>Bull</span>
            <span className="tabular-nums font-mono">${fmtPrice(p.bull)}</span>
          </div>
        )}
        {p.bear != null && (
          <div className="flex justify-between gap-4 leading-relaxed">
            <span style={{ color: C.bear }}>Bear</span>
            <span className="tabular-nums font-mono">${fmtPrice(p.bear)}</span>
          </div>
        )}
      </div>
    );
  };

  const execInCount = data?.executed_in_range_count ?? 0;
  const execOutCount = data?.executed_outside_range_count ?? 0;

  // Shared dot renderer for sparse executed-dot series
  const execDotProps = (color) => ({
    r: 5,
    fill: color,
    stroke: '#fff',
    strokeWidth: 2,
  });

  return (
    <div className="border-b border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
      {/* Header */}
      <div className="flex items-center justify-between px-6 pt-5">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className={TYPO.cardTitle + ' text-slate-800 dark:text-slate-100'}>
            {hasForecast
              ? `Forecast for next ${days} days — ${productName || productId}`
              : `Price history — ${productName || productId}`}
          </h3>
          <span className="inline-flex items-center gap-1.5 rounded-md bg-slate-100 px-2 py-0.5
            text-[11px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            <RefreshCw size={11} />
            Last refresh {data?.as_of_date ? fmtDateShort(data.as_of_date) : '—'}
          </span>
          {/* Phase 6: Overlay badges */}
          {data?.overlay?.policy?.volatility_regime && (
            <span className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-medium ${
              data.overlay.policy.volatility_regime.toLowerCase() === 'high'
                ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                : data.overlay.policy.volatility_regime.toLowerCase() === 'moderate'
                ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                : 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400'
            }`}>
              <Activity size={11} />
              {data.overlay.policy.volatility_regime}
              {data.overlay.policy.vol_multiplier != null && ` ×${data.overlay.policy.vol_multiplier.toFixed(1)}`}
            </span>
          )}
          {data?.overlay?.intelligence?.active_event_count > 0 && (
            <span className="inline-flex items-center gap-1 rounded-md bg-purple-100 px-2 py-0.5 text-[11px] font-medium text-purple-700 dark:bg-purple-900/30 dark:text-purple-400">
              <Zap size={11} />
              {data.overlay.intelligence.bias_direction === 'up' ? '↑' : data.overlay.intelligence.bias_direction === 'down' ? '↓' : '—'}{' '}
              {data.overlay.intelligence.active_event_count} intel
            </span>
          )}
          {/* Phase B: Trust tier badge */}
          {data?.overlay?.trust_tier && (
            <div
              className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] font-medium"
              style={{
                backgroundColor:
                  data.overlay.trust_tier.badge_color === 'green' ? '#dcfce7' :
                  data.overlay.trust_tier.badge_color === 'yellow' ? '#fef9c3' :
                  data.overlay.trust_tier.badge_color === 'orange' ? '#ffedd5' :
                  data.overlay.trust_tier.badge_color === 'red' ? '#fee2e2' : '#f1f5f9',
                color:
                  data.overlay.trust_tier.badge_color === 'green' ? '#166534' :
                  data.overlay.trust_tier.badge_color === 'yellow' ? '#854d0e' :
                  data.overlay.trust_tier.badge_color === 'orange' ? '#9a3412' :
                  data.overlay.trust_tier.badge_color === 'red' ? '#991b1b' : '#475569',
              }}
              title={data.overlay.trust_tier.reason_en || data.overlay.trust_tier.reason_zh}
            >
              <span
                className="inline-block h-1.5 w-1.5 rounded-full"
                style={{
                  backgroundColor:
                    data.overlay.trust_tier.badge_color === 'green' ? '#22c55e' :
                    data.overlay.trust_tier.badge_color === 'yellow' ? '#eab308' :
                    data.overlay.trust_tier.badge_color === 'orange' ? '#f97316' :
                    data.overlay.trust_tier.badge_color === 'red' ? '#ef4444' : '#94a3b8',
                }}
              />
              {data.overlay.trust_tier.badge_label_en}
            </div>
          )}
          {/* Phase D: Price-change probability badge (primary 7d horizon) */}
          {data?.overlay?.probability && data.overlay.probability.length > 0 && (() => {
            const p7 = data.overlay.probability.find(p => p.horizon === 7)
                       || data.overlay.probability[0]
            if (!p7) return null
            const prob = p7.p_rise
            const tintBg = prob > 0.6 ? '#dcfce7' : prob < 0.4 ? '#fee2e2' : '#f1f5f9'
            const tintFg = prob > 0.6 ? '#166534' : prob < 0.4 ? '#991b1b' : '#475569'
            const dot = prob > 0.6 ? '#22c55e' : prob < 0.4 ? '#ef4444' : '#94a3b8'
            const sign = (p7.expected_change_pct ?? 0) >= 0 ? '↑' : '↓'
            return (
              <div
                className="relative inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] font-medium"
                style={{ backgroundColor: tintBg, color: tintFg }}
                title={`P(rise) at ${p7.horizon}d: ${(prob * 100).toFixed(0)}%  · expected ${sign} ${(Math.abs(p7.expected_change_pct ?? 0) * 100).toFixed(1)}%`}
              >
                <span
                  className="inline-block h-2 w-2 rounded-full"
                  style={{ backgroundColor: dot }}
                />
                {p7.horizon}d · {(prob * 100).toFixed(0)}% rise
              </div>
            )
          })()}
          {/* Phase 7: SKU badge for ERP products */}
          {isErpProduct && skuCount > 0 && (
            <div className="relative">
              <button
                onClick={() => setShowSkuPopover(v => !v)}
                className="inline-flex items-center gap-1.5 rounded-md bg-indigo-100 px-2 py-0.5
                  text-[11px] font-medium text-indigo-700 hover:bg-indigo-200
                  dark:bg-indigo-900/30 dark:text-indigo-400 dark:hover:bg-indigo-900/50
                  transition-colors"
                title={`${skuCount} SKUs (material_code) for this product family`}
              >
                <Layers size={11} />
                {skuCount} SKU{skuCount > 1 ? 's' : ''}
                {primarySku && (
                  <span className="ml-1 opacity-70">· {primarySku.material_code}</span>
                )}
              </button>
              {showSkuPopover && (
                <div className="absolute right-0 top-full z-50 mt-1 min-w-[280px] rounded-md
                  border border-slate-200 bg-white p-3 shadow-lg dark:border-slate-700 dark:bg-slate-900">
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-semibold text-slate-700 dark:text-slate-200">
                      SKU Forecast Breakdown
                    </span>
                    <button
                      onClick={() => setShowSkuPopover(false)}
                      className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200"
                    >
                      ✕
                    </button>
                  </div>
                  <div className="max-h-[240px] overflow-y-auto">
                    <table className="w-full text-[11px]">
                      <thead>
                        <tr className="border-b border-slate-200 dark:border-slate-700">
                          <th className="pb-1 text-left font-medium text-slate-500 dark:text-slate-400">Code</th>
                          <th className="pb-1 text-right font-medium text-slate-500 dark:text-slate-400">Forecast</th>
                          <th className="pb-1 text-right font-medium text-slate-500 dark:text-slate-400">Conf.</th>
                        </tr>
                      </thead>
                      <tbody>
                        {skuList.map((sku) => (
                          <tr
                            key={sku.target_id}
                            className={`border-b border-slate-100 dark:border-slate-800 ${
                              sku.is_primary ? 'bg-indigo-50 dark:bg-indigo-900/20' : ''
                            }`}
                          >
                            <td className="py-1.5 font-mono text-slate-700 dark:text-slate-300">
                              {sku.is_primary && (
                                <span className="mr-1 text-indigo-600 dark:text-indigo-400" title="Primary SKU">★</span>
                              )}
                              {sku.material_code}
                            </td>
                            <td className="py-1.5 text-right tabular-nums font-mono text-slate-700 dark:text-slate-300">
                              {sku.forecast_base != null ? fmtPrice(sku.forecast_base) : '—'}
                            </td>
                            <td className="py-1.5 text-right tabular-nums text-slate-500 dark:text-slate-400">
                              {sku.forecast_confidence != null
                                ? `${(sku.forecast_confidence * 100).toFixed(0)}%`
                                : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="mt-2 text-[10px] text-slate-400 dark:text-slate-500">
                    ★ = Primary SKU (highest volume). Forecast shown is {days}-day base.
                  </p>
                </div>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className={TYPO.micro + ' text-slate-500 dark:text-slate-400'}>
            {productName || productId}
          </span>
          <button
            onClick={() => refetch()}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600
              dark:hover:bg-slate-800 dark:hover:text-slate-300"
            title="Refresh"
          >
            <RefreshCw size={14} className={isFetching ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      {/* Range pills (right-aligned) */}
      <div className="flex items-center justify-end gap-1 px-6 pt-2">
        {RANGES.map((d) => (
          <button
            key={d}
            onClick={() => setDays(d)}
            className={`px-2 py-0.5 text-[11px] rounded transition-colors ${
              days === d
                ? 'bg-slate-200 font-semibold text-slate-800 dark:bg-slate-700 dark:text-slate-100'
                : 'text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
            }`}
          >
            {d}d
          </button>
        ))}
      </div>

      {/* Zone pill bars — single past-only pill until forecast data exists */}
      {hasForecast ? (
        <div className="grid grid-cols-2 gap-0 mx-6 mt-3">
          <div className="flex items-center justify-center rounded-t-md bg-stone-200 py-1.5
            text-[11px] font-medium text-stone-600 dark:bg-stone-700 dark:text-stone-300">
            Past {days} days · AI accuracy review
          </div>
          <div className="flex items-center justify-center rounded-t-md bg-blue-100 py-1.5
            text-[11px] font-medium text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
            Next {days} days · Forecast
          </div>
        </div>
      ) : (
        <div className="mx-6 mt-3">
          <div className="flex items-center justify-center rounded-t-md bg-stone-200 py-1.5
            text-[11px] font-medium text-stone-600 dark:bg-stone-700 dark:text-stone-300">
            Past {days} days · Price history
          </div>
        </div>
      )}

      {/* Chart body */}
      {isLoading && (
        <div className="h-[360px] flex items-center justify-center">
          <p className={TYPO.listText + ' text-slate-500'}>Loading forecast chart…</p>
        </div>
      )}
      {isError && (
        <div className="h-[360px] flex items-center justify-center">
          <p className={TYPO.listText + ' text-red-500'}>Failed to load forecast chart.</p>
        </div>
      )}
      {!isLoading && !isError && points.length === 0 && (
        <div className="h-[360px] flex items-center justify-center">
          <p className={TYPO.listText + ' text-slate-500'}>No forecast data available.</p>
        </div>
      )}
      {!isLoading && !isError && points.length > 0 && (
        <>
          <ResponsiveContainer width="100%" height={360}>
            <ComposedChart data={points} margin={{ top: 12, right: 24, left: 8, bottom: 4 }}>
              <CartesianGrid stroke={isDark ? 'rgba(148,163,184,0.12)' : 'rgba(226,232,240,0.8)'} strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10, fill: chartTheme.tick }}
                tickLine={false}
                axisLine={{ stroke: chartTheme.axis }}
                minTickGap={35}
                tickFormatter={fmtDateMMDD}
              />
              <YAxis
                domain={yAxis.domain}
                ticks={yAxis.ticks}
                tick={{ fontSize: 10, fill: chartTheme.tick }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v) => `$${Math.round(v)}`}
                width={55}
              />
              <Tooltip content={<CustomTooltip />} />

              {/* ── Past AI range band (stacked area) ── */}
              <Area
                type="monotone" dataKey="prevAiLow"
                stroke="none" fill="transparent"
                stackId="pastBand" connectNulls={false}
                isAnimationActive={false} legendType="none" tooltipType="none"
              />
              <Area
                type="monotone" dataKey="prevAiRange"
                stroke="none" fill={C.pastBand} fillOpacity={isDark ? 0.3 : 0.5}
                stackId="pastBand" connectNulls={false}
                isAnimationActive={false} legendType="none" tooltipType="none"
              />

              {/* ── Forecast range band (stacked area, subtle) ── */}
              <Area
                type="monotone" dataKey="forecastBear"
                stroke="none" fill="transparent"
                stackId="forecastBand" connectNulls={false}
                isAnimationActive={false} legendType="none" tooltipType="none"
              />
              <Area
                type="monotone" dataKey="forecastRange"
                stroke="none" fill={C.forecastBand} fillOpacity={isDark ? 0.12 : 0.2}
                stackId="forecastBand" connectNulls={false}
                isAnimationActive={false} legendType="none" tooltipType="none"
              />

              {/* ── Vertical divider at the past/forecast boundary ── */}
              {hasForecast && todayIdx >= 0 && (
                <ReferenceLine
                  x={points[todayIdx]?.date}
                  stroke={isDark ? '#94a3b8' : '#475569'}
                  strokeDasharray="5 4"
                  strokeWidth={1.5}
                  label={{
                    value: 'Today',
                    position: 'insideTop',
                    offset: 8,
                    style: { fontSize: 10, fill: chartTheme.tick, fontWeight: 600 },
                  }}
                />
              )}

              {/* ── Main series ── */}

              {/* Actual price — royal blue, prominent */}
              <Line
                type="monotone" dataKey="actual" stroke={C.actual}
                strokeWidth={3} dot={false} name="Actual price"
                connectNulls={false} isAnimationActive={false}
              />

              {/* Previous AI recommended — orange dashed */}
              <Line
                type="monotone" dataKey="previous_ai" stroke={C.previousAi}
                strokeWidth={2.5} strokeDasharray="5 3" dot={false}
                name="Previous AI recommended price" connectNulls
                isAnimationActive={false}
              />

              {/* Bull — green dotted, fanning from today */}
              <Line
                type="monotone" dataKey="bull" stroke={C.bull}
                strokeWidth={3} strokeDasharray="4 2" dot={false}
                name="Bull" connectNulls={false} isAnimationActive={false}
              />

              {/* Base — blue dotted */}
              <Line
                type="monotone" dataKey="base" stroke={C.base}
                strokeWidth={3} strokeDasharray="4 2" dot={false}
                name="Base" connectNulls={false} isAnimationActive={false}
              />

              {/* Bear — red dotted */}
              <Line
                type="monotone" dataKey="bear" stroke={C.bear}
                strokeWidth={3} strokeDasharray="4 2" dot={false}
                name="Bear" connectNulls={false} isAnimationActive={false}
              />

              {/* ── Executed scatter dots (rendered as sparse Line with custom dots) ── */}
              <Line
                type="monotone" dataKey="execInDot"
                stroke="transparent" strokeWidth={0}
                dot={{ r: 5, fill: C.execIn, stroke: '#fff', strokeWidth: 2 }}
                name="Executed in range"
                connectNulls={false} isAnimationActive={false}
                legendType="line"
              />
              <Line
                type="monotone" dataKey="execOutDot"
                stroke="transparent" strokeWidth={0}
                dot={{ r: 5, fill: C.execOut, stroke: '#fff', strokeWidth: 2 }}
                name="Executed outside range"
                connectNulls={false} isAnimationActive={false}
                legendType="line"
              />

              {/* ── Green boundary marker (large dot) ── */}
              {hasForecast && todayPoint && todayValue != null && (
                <ReferenceDot
                  x={todayPoint.date}
                  y={todayValue}
                  r={6}
                  fill={C.todayDot}
                  stroke="#fff"
                  strokeWidth={3}
                />
              )}
            </ComposedChart>
          </ResponsiveContainer>

          {/* ── Legend ── */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-slate-100
            px-6 py-2.5 text-[11px] text-slate-500 dark:border-slate-800 dark:text-slate-400">
            <LegendLine color={C.actual} label="Actual price" width={16} />
            {hasPastAi && (
              <>
                <LegendBand color={C.pastBand} label="AI recommended range (past)" opacity={0.5} />
                <LegendLine color={C.previousAi} label="Previous AI recommended price" dashed width={16} />
                <LegendDot color={C.execIn} label={`Executed in range (${execInCount})`} />
                <LegendDot color={C.execOut} label={`Executed outside range (${execOutCount})`} />
              </>
            )}
            {hasForecast && (
              <>
                <LegendLine color={C.bull} label="Bull" dotted width={14} />
                <LegendLine color={C.base} label="Base" dotted width={14} />
                <LegendLine color={C.bear} label="Bear" dotted width={14} />
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/* ── Legend atoms ────────────────────────────────────────────── */
function LegendLine({ color, label, dashed, dotted, width = 14 }) {
  const dash = dashed ? '4 3' : dotted ? '3 3' : '0';
  return (
    <span className="inline-flex items-center gap-1.5">
      <svg width={width} height={6}>
        <line
          x1="0" y1="3" x2={width} y2="3"
          stroke={color} strokeWidth={dashed || dotted ? 2 : 2.5} strokeDasharray={dash}
        />
      </svg>
      {label}
    </span>
  );
}

function LegendBand({ color, label, opacity = 0.2 }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block h-3 w-4 rounded-sm"
        style={{ backgroundColor: color, opacity }}
      />
      {label}
    </span>
  );
}

function LegendDot({ color, label }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <svg width={10} height={10}>
        <circle cx={5} cy={5} r={4} fill={color} stroke="#fff" strokeWidth={1.5} />
      </svg>
      {label}
    </span>
  );
}
