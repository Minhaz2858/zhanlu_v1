import { useEffect, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  PolarAngleAxis,
  PolarGrid,
  Radar,
  RadarChart,
  RadialBar,
  RadialBarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { getChartPalette } from '../hooks/useDesignTokens.js';

/** Active chart palette — design-system driven; falls back to slate defaults. */
function palette() {
  return getChartPalette();
}

/**
 * Convert backend rows into objects keyed by column name.
 *
 * The backend returns DICT rows (`[{"revenue": 123}]`) — pass them through
 * as-is. Legacy/columnar backends return ARRAY rows (`[[1,2],[3,4]]`) — map
 * them onto the columns list. Accepting only array rows was the "No data on
 * every widget" bug: dict rows were filtered out and every card rendered empty
 * even though the SQL returned real figures.
 */
export function rowsToObjects(columns, rows) {
  if (!Array.isArray(columns) || !Array.isArray(rows)) return [];
  if (rows.length && typeof rows[0] === 'object' && !Array.isArray(rows[0])) {
    return rows; // dict rows — already keyed by column name
  }
  return rows
    .filter((r) => Array.isArray(r))
    .map((r) => {
      const obj = {};
      columns.forEach((c, i) => {
        obj[c] = r[i];
      });
      return obj;
    });
}

function numericKeys(objects, xKey) {
  if (!objects.length) return [];
  const first = objects[0];
  return Object.keys(first).filter(
    (k) => k !== xKey && typeof Number(first[k]) === 'number' && first[k] !== null && first[k] !== '',
  );
}

function fmtNumber(value, digits = 2) {
  const num = Number(value);
  return Number.isFinite(num)
    ? num.toLocaleString(undefined, { maximumFractionDigits: digits })
    : String(value ?? '');
}

function fmtCompact(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value ?? '');
  return new Intl.NumberFormat(undefined, {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(num);
}

/** Count-up animation for the Chinese BI (大屏) style. Animates 0 → value
 *  over ~900ms with an ease-out curve. Falls back to the plain formatted
 *  value when the browser prefers reduced motion. */
function useCountUp(target, active) {
  const [display, setDisplay] = useState(0);
  useEffect(() => {
    if (!active) { setDisplay(Number(target) || 0); return; }
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setDisplay(Number(target) || 0);
      return;
    }
    const end = Number(target) || 0;
    const duration = 900;
    const start = performance.now();
    let raf;
    const tick = (now) => {
      const p = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(end * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, active]);
  return display;
}

function tooltipStyle() {
  return {
    contentStyle: {
      backgroundColor: 'var(--ds-background)',
      border: '1px solid var(--ds-border)',
      borderRadius: 'var(--ds-card-radius)',
      boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
      color: 'var(--ds-foreground)',
      fontSize: '12px',
      padding: '8px 10px',
    },
    labelStyle: { color: 'var(--ds-foreground)', fontWeight: 600, marginBottom: 4 },
    itemStyle: { color: 'var(--ds-foreground)' },
    cursor: { fill: 'var(--ds-muted)', opacity: 0.6 },
  };
}

/** Formatter for chart tooltips — renders values with thousands separators. */
function valueFormatter(value, name) {
  const num = Number(value);
  const label = name ? `${name}: ` : '';
  return [Number.isFinite(num) ? fmtNumber(num) : String(value ?? ''), label];
}

/**
 * Interactive legend: click a series name to toggle its visibility.
 * Mirrors Recharts' default legend look (dot + label) but adds the
 * toggle affordance and a dimmed style for hidden series.
 */
function InteractiveLegend({ payload, hidden, onToggle }) {
  if (!payload || !payload.length) return null;
  return (
    <ul className="mt-2 flex flex-wrap items-center justify-center gap-x-3 gap-y-1">
      {payload.map((entry) => {
        const isHidden = Boolean(hidden[entry.dataKey || entry.value]);
        return (
          <li key={entry.dataKey || entry.value}>
            <button
              type="button"
              onClick={() => onToggle(entry.dataKey || entry.value)}
              className={`inline-flex items-center gap-1.5 text-xs transition-opacity ${
                isHidden ? 'opacity-35 hover:opacity-60' : 'opacity-90 hover:opacity-100'
              }`}
              title={isHidden ? 'Show series' : 'Hide series'}
            >
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: entry.color }}
              />
              <span className="text-foreground/80">{entry.value}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** Shared chart animation timing — 300ms, ease-out, respects reduced motion. */
const CHART_ANIMATION = { animationDuration: 300, animationEasing: 'ease-out' };

function Card({ title, children, className = '', status, hint, accent }) {
  // Design-token driven card chrome. The accent bar uses the design
  // system's primary color (falls back to a neutral slate when no design
  // system is attached) — the same professional chrome pattern the EDIA
  // dashboards use (gradient header + status badge + subtle hover lift).
  return (
    <section
      className={`dash-card relative flex flex-col overflow-hidden rounded-card border border-border bg-background ${className || ''}`}
      aria-label={title}
    >
      <div
        className="h-0.5 w-full shrink-0"
        style={{ background: accent || 'linear-gradient(90deg, var(--ds-primary), var(--ds-accent))' }}
      />
      <div className="flex flex-1 flex-col p-4">
        <div className="mb-3 flex items-start justify-between gap-2">
          <h2 className="font-body min-w-0 truncate text-sm font-semibold text-foreground">{title}</h2>
          <div className="flex shrink-0 items-center gap-1.5">
            {status === 'live' ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:text-emerald-400">
                <span className="h-1 w-1 rounded-full bg-emerald-500" />
                live
              </span>
            ) : null}
            {status === 'loading' ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-foreground/60">
                <span className="h-1 w-1 animate-pulse rounded-full bg-foreground/40" />
                loading
              </span>
            ) : null}
            {hint ? (
              <span className="max-w-[16ch] truncate text-[10px] text-foreground/40" title={hint}>
                {hint}
              </span>
            ) : null}
          </div>
        </div>
        <div className="min-h-0 flex-1">{children}</div>
      </div>
    </section>
  );
}

function Skeleton() {
  // Shimmer skeleton (locked polish, Tier 1): three pulsing bars with a
  // moving highlight sweep — reads as "data is loading" instead of a flat
  // gray box. Respects prefers-reduced-motion via CSS.
  return (
    <div className="flex h-40 flex-col items-center justify-center gap-2" role="status" aria-label="Loading data">
      <div className="dash-shimmer h-3 w-24 rounded-full" />
      <div className="dash-shimmer h-2.5 w-16 rounded-full" />
      <div className="dash-shimmer h-2.5 w-28 rounded-full" />
    </div>
  );
}

/** Locked-analytics callout: "Top: X · 62.5%" footer for breakdown widgets.
 *  Rendered from options.topItem which the backend computed server-side at
 *  build time (analytics.py) — never fabricated by the LLM. */
function TopItemCallout({ topItem, label }) {
  if (!topItem || topItem.label == null) return null;
  return (
    <div className="mt-2 flex items-center gap-1.5 rounded-md bg-primary/5 px-2 py-1 text-[11px] text-foreground/70">
      <span className="font-medium text-foreground/50">{label || 'Top'}:</span>
      <span className="truncate font-semibold text-primary">{topItem.label}</span>
      <span className="ml-auto shrink-0 tabular-nums text-foreground/50">
        {fmtCompact(topItem.value)} · {topItem.share_pct != null ? `${topItem.share_pct}%` : ''}
      </span>
    </div>
  );
}

function Empty({ error, compact }) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-1.5 text-center text-xs text-foreground/45 ${compact ? 'h-24' : 'h-40'}`}
      role="status"
    >
      <svg className="h-5 w-5 opacity-40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.5 6 10l4 4 5-7 4 4" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 20h18M4 20V8m16 12V5" />
      </svg>
      <span>{error || 'No data yet — waiting for the first refresh'}</span>
    </div>
  );
}

function KpiWidget({ objects, options }) {
  if (!objects.length) return <Empty compact />;
  const first = objects[0];
  const keys = numericKeys(objects, '');
  if (!keys.length) return <Empty compact />;
  const key = keys[keys.length - 1];
  const value = first[key];
  const num = Number(value);
  const unit = options?.unit || '';
  const color = options?.color || palette()[0];
  const delta = options?.delta != null ? Number(options.delta) : null;
  const deltaLabel = options?.deltaLabel || 'vs prev. period';
  // Decision-center KPI: severity accent (colored top border), delta tone
  // override (up/down/neutral/warn — e.g. inventory rising = bad = "down"),
  // and a context sub-line ("38 订单 · 12 产品" / "目标 16% · 底线 11%").
  const accent = options?.accent || null;
  const deltaTone = options?.delta_tone || (delta == null ? null : delta >= 0 ? 'up' : 'down');
  const deltaCls = deltaTone === 'up' ? 'tone-up' : deltaTone === 'down' ? 'tone-down' : deltaTone === 'warn' ? 'tone-warn' : 'tone-neutral';
  // Chinese BI style: animate the number counting up (大屏 data-wall effect).
  // Detected from the <html> class set by App.jsx — no prop drilling needed.
  const chineseBi = typeof document !== 'undefined'
    && document.documentElement.classList.contains('chinese-bi');
  const displayNum = useCountUp(num, chineseBi);
  // Optional inline sparkline: the metric returns series rows (label + value)
  // and options.sparkline is truthy. Renders a compact trend under the number.
  const sparkRows = options?.sparkline ? objects.slice(0, 30) : [];
  const sparkValues = sparkRows.map((o) => Number(o[Object.keys(o)[Object.keys(o).length - 1]]) || 0);
  const hasSpark = sparkRows.length >= 2 && sparkValues.some((v) => v !== sparkValues[0]);
  const sparkWidth = 120;
  const sparkHeight = 28;
  const sparkPoints = (() => {
    if (!hasSpark) return '';
    const min = Math.min(...sparkValues);
    const max = Math.max(...sparkValues);
    const range = max - min || 1;
    return sparkValues
      .map((v, i) => {
        const x = (i / (sparkValues.length - 1)) * sparkWidth;
        const y = sparkHeight - 2 - ((v - min) / range) * (sparkHeight - 4);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  })();
  const sparkPath = hasSpark
    ? sparkPoints
        .split(' ')
        .map((p, i) => (i === 0 ? `M ${p}` : `L ${p}`))
        .join(' ')
    : '';
  const lastSpark = hasSpark ? sparkValues[sparkValues.length - 1] : null;
  const firstSpark = hasSpark ? sparkValues[0] : null;
  const sparkTrend = hasSpark && firstSpark !== null ? lastSpark - firstSpark : null;
  return (
    <div
      className="flex h-40 flex-col justify-center"
      style={accent ? { borderTop: `2px solid ${accent}` } : undefined}
    >
      <div className="text-[11px] font-medium uppercase tracking-wide text-foreground/45">{key}</div>
      <div
        className="mt-1 font-heading text-[length:var(--text-4xl,2.25rem)] font-bold leading-tight tracking-tight tabular-nums"
        style={{ color }}
      >
        {fmtNumber(displayNum)}
        {unit ? <span className="ml-1.5 text-sm font-medium text-foreground/50">{unit}</span> : null}
      </div>
      {delta != null ? (
        <div className="mt-2 inline-flex items-center gap-1 text-xs">
          <span className={`inline-flex items-center gap-0.5 font-medium ${deltaCls}`}>
            <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {deltaTone === 'up' ? (
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
              ) : deltaTone === 'down' ? (
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14" />
              )}
            </svg>
            {Math.abs(delta).toLocaleString(undefined, { maximumFractionDigits: 1 })}%
          </span>
          <span className="text-foreground/45">{deltaLabel}</span>
        </div>
      ) : null}
      {options?.sub ? (
        <div className="mt-1.5 text-[10px] text-foreground/45">{options.sub}</div>
      ) : null}
      {hasSpark ? (
        <div className="mt-2 flex items-end gap-2">
          <svg width={sparkWidth} height={sparkHeight} className="shrink-0" aria-hidden="true">
            <path d={sparkPath} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx={sparkWidth} cy={sparkHeight - 2 - ((lastSpark - Math.min(...sparkValues)) / (Math.max(...sparkValues) - Math.min(...sparkValues) || 1)) * (sparkHeight - 4)} r="2" fill={color} />
          </svg>
          {sparkTrend != null ? (
            <span
              className={`mb-0.5 inline-flex items-center gap-0.5 text-[11px] font-medium ${
                sparkTrend >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500 dark:text-red-400'
              }`}
            >
              <svg className="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                {sparkTrend >= 0 ? (
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
                ) : (
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                )}
              </svg>
              {fmtCompact(Math.abs(sparkTrend))}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function LineWidget({ objects, type, options }) {
  const [hidden, setHidden] = useState({});
  if (!objects.length) return <Empty />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty />;
  const Chart = type === 'area' ? AreaChart : LineChart;
  const visibleKeys = keys.filter((k) => !hidden[k]);
  function toggleSeries(k) {
    setHidden((h) => ({ ...h, [k]: !h[k] }));
  }
  return (
    <div>
      <ResponsiveContainer width="100%" height={220}>
        <Chart data={objects} margin={{ top: 8, right: 12, left: -8, bottom: 0 }} {...CHART_ANIMATION}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--ds-border)" vertical={false} />
          <XAxis
            dataKey={xKey}
            tick={{ fontSize: 11, fill: 'var(--ds-foreground)' }}
            tickLine={false}
            tickMargin={8}
            axisLine={{ stroke: 'var(--ds-border)' }}
          />
          <YAxis
            tick={{ fontSize: 11, fill: 'var(--ds-foreground)' }}
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(v) => fmtCompact(v)}
          />
          <Tooltip {...tooltipStyle()} formatter={valueFormatter} />
          {visibleKeys.map((k, i) =>
            type === 'area' ? (
              <Area
                key={k}
                type="monotone"
                dataKey={k}
                name={k}
                stroke={options?.colors?.[i] || palette()[i % palette().length]}
                fill={options?.colors?.[i] || palette()[i % palette().length]}
                fillOpacity={0.12}
                strokeWidth={2}
                activeDot={{ r: 4 }}
              />
            ) : (
              <Line
                key={k}
                type="monotone"
                dataKey={k}
                name={k}
                stroke={options?.colors?.[i] || palette()[i % palette().length]}
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            ),
          )}
        </Chart>
      </ResponsiveContainer>
      {options?.delta != null ? (
        <div className="mt-2 inline-flex items-center gap-1 text-xs">
          <span
            className={`inline-flex items-center gap-0.5 font-medium ${
              options.delta >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500 dark:text-red-400'
            }`}
          >
            <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {options.delta >= 0 ? (
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
              ) : (
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
              )}
            </svg>
            {Math.abs(options.delta).toLocaleString(undefined, { maximumFractionDigits: 1 })}%
          </span>
          <span className="text-foreground/45">{options.deltaLabel || 'vs prev. period'}</span>
        </div>
      ) : null}
      {keys.length > 1 && (
        <InteractiveLegend
          payload={keys.map((k, i) => ({
            value: k,
            dataKey: k,
            color: options?.colors?.[i] || palette()[i % palette().length],
          }))}
          hidden={hidden}
          onToggle={toggleSeries}
        />
      )}
    </div>
  );
}

function BarWidget({ objects, options }) {
  const [hidden, setHidden] = useState({});
  if (!objects.length) return <Empty />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty />;
  const visibleKeys = keys.filter((k) => !hidden[k]);
  function toggleSeries(k) {
    setHidden((h) => ({ ...h, [k]: !h[k] }));
  }
  return (
    <div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={objects} margin={{ top: 8, right: 12, left: -8, bottom: 0 }} {...CHART_ANIMATION}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--ds-border)" vertical={false} />
          <XAxis
            dataKey={xKey}
            tick={{ fontSize: 11, fill: 'var(--ds-foreground)' }}
            tickLine={false}
            tickMargin={8}
            axisLine={{ stroke: 'var(--ds-border)' }}
            interval={0}
          />
          <YAxis
            tick={{ fontSize: 11, fill: 'var(--ds-foreground)' }}
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(v) => fmtCompact(v)}
          />
          <Tooltip {...tooltipStyle()} formatter={valueFormatter} cursor={{ fill: 'var(--ds-muted)', opacity: 0.5 }} />
          {visibleKeys.map((k, i) => (
            <Bar
              key={k}
              dataKey={k}
              name={k}
              stackId={options?.stacked ? 'stack' : undefined}
              fill={options?.colors?.[i] || palette()[i % palette().length]}
              radius={options?.stacked ? (i === visibleKeys.length - 1 ? [4, 4, 0, 0] : 0) : [4, 4, 0, 0]}
              maxBarSize={48}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
      {keys.length > 1 && (
        <InteractiveLegend
          payload={keys.map((k, i) => ({
            value: k,
            dataKey: k,
            color: options?.colors?.[i] || palette()[i % palette().length],
          }))}
          hidden={hidden}
          onToggle={toggleSeries}
        />
      )}
      <TopItemCallout topItem={options?.topItem} label="Top" />
    </div>
  );
}

function PieWidget({ objects, options }) {
  if (!objects.length) return <Empty />;
  const labelKey = Object.keys(objects[0])[0];
  const valueKey = numericKeys(objects, labelKey)[0] || Object.keys(objects[0])[1];
  if (!valueKey) return <Empty />;
  const data = objects
    .map((o, i) => ({
      name: String(o[labelKey] ?? `#${i + 1}`),
      value: Number(o[valueKey]) || 0,
    }))
    .slice(0, 8); // cap segments — beyond 8 a bar chart reads better
  const total = data.reduce((s, d) => s + d.value, 0);
  return (
    <div className="relative">
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie data={data} dataKey="value" nameKey="name" innerRadius={48} outerRadius={82} paddingAngle={2} strokeWidth={0} {...CHART_ANIMATION}>
            {data.map((_, i) => (
              <Cell key={i} fill={palette()[i % palette().length]} />
            ))}
          </Pie>
          <Tooltip {...tooltipStyle()} formatter={(v) => fmtNumber(v)} />
          <Legend wrapperStyle={{ fontSize: 11, color: 'var(--ds-foreground)' }} />
        </PieChart>
      </ResponsiveContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="font-heading text-lg font-bold tabular-nums text-foreground">{fmtCompact(total)}</span>
        <span className="text-[10px] uppercase tracking-wide text-foreground/45">total</span>
      </div>
      <TopItemCallout topItem={options?.topItem} label="Top" />
    </div>
  );
}

/**
 * ComboWidget — the classic BI chart: bars on the left axis + a line on the
 * right axis sharing one x-axis (e.g. Volume bars + Revenue trend, or
 * Quantity bars + Price line). The spec declares which keys are bars and
 * which are lines via options:
 *   { bars: ['qty'], lines: ['amount'], barColor: ..., lineColor: ... }
 * Defaults: first key = bar, second key = line.
 */
function ComboWidget({ objects, options }) {
  const [hidden, setHidden] = useState({});
  if (!objects.length) return <Empty />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty />;
  const barKeys = (options?.bars || []).filter((k) => keys.includes(k));
  const lineKeys = (options?.lines || []).filter((k) => keys.includes(k));
  // Fallback when the spec omits bars/lines: first numeric = bar, second = line.
  const effectiveBars = barKeys.length ? barKeys : [keys[0]].filter(Boolean);
  const effectiveLines = lineKeys.length ? lineKeys : [keys[1]].filter(Boolean);
  const visibleBars = effectiveBars.filter((k) => !hidden[k]);
  const visibleLines = effectiveLines.filter((k) => !hidden[k]);
  function toggleSeries(k) {
    setHidden((h) => ({ ...h, [k]: !h[k] }));
  }
  const legendPayload = [...effectiveBars, ...effectiveLines].map((k, i) => ({
    value: k,
    dataKey: k,
    color: options?.colors?.[i] || palette()[i % palette().length],
  }));
  return (
    <div>
      <ResponsiveContainer width="100%" height={220}>
        <ComposedChart data={objects} margin={{ top: 8, right: 12, left: -8, bottom: 0 }} {...CHART_ANIMATION}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--ds-border)" vertical={false} />
          <XAxis
            dataKey={xKey}
            tick={{ fontSize: 11, fill: 'var(--ds-foreground)' }}
            tickLine={false}
            tickMargin={8}
            axisLine={{ stroke: 'var(--ds-border)' }}
            interval={0}
          />
          <YAxis
            yAxisId="left"
            tick={{ fontSize: 11, fill: 'var(--ds-foreground)' }}
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(v) => fmtCompact(v)}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={{ fontSize: 11, fill: 'var(--ds-foreground)' }}
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(v) => fmtCompact(v)}
          />
          <Tooltip {...tooltipStyle()} formatter={valueFormatter} cursor={{ fill: 'var(--ds-muted)', opacity: 0.5 }} />
          {visibleBars.map((k, i) => (
            <Bar
              key={k}
              yAxisId="left"
              dataKey={k}
              name={k}
              fill={options?.barColor || options?.colors?.[i] || palette()[i % palette().length]}
              radius={[4, 4, 0, 0]}
              maxBarSize={36}
            />
          ))}
          {visibleLines.map((k, i) => (
            <Line
              key={k}
              yAxisId="right"
              type="monotone"
              dataKey={k}
              name={k}
              stroke={options?.lineColor || options?.colors?.[effectiveBars.length + i] || palette()[(effectiveBars.length + i) % palette().length]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
            />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
      {legendPayload.length > 1 && (
        <InteractiveLegend payload={legendPayload} hidden={hidden} onToggle={toggleSeries} />
      )}
    </div>
  );
}

function TableWidget({ objects, columns, options }) {
  const [sortKey, setSortKey] = useState(null);
  const [sortDir, setSortDir] = useState('asc');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 10;

  if (!objects || !objects.length) return <Empty />;

  const filtered = query.trim()
    ? objects.filter((r) =>
        columns.some((c) => String(r[c] ?? '').toLowerCase().includes(query.trim().toLowerCase())),
      )
    : objects;

  const sorted = [...filtered].sort((a, b) => {
    if (!sortKey) return 0;
    const av = a[sortKey];
    const bv = b[sortKey];
    const an = Number(av);
    const bn = Number(bv);
    if (Number.isFinite(an) && Number.isFinite(bn)) {
      return sortDir === 'asc' ? an - bn : bn - an;
    }
    return sortDir === 'asc'
      ? String(av ?? '').localeCompare(String(bv ?? ''))
      : String(bv ?? '').localeCompare(String(av ?? ''));
  });

  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = sorted.slice(safePage * PAGE_SIZE, safePage * PAGE_SIZE + PAGE_SIZE);

  function toggleSort(c) {
    if (sortKey === c) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(c);
      setSortDir('asc');
    }
  }

  function exportCsv() {
    if (!sorted.length) return;
    const esc = (v) => {
      const s = String(v ?? '');
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const lines = [
      columns.map(esc).join(','),
      ...sorted.map((r) => columns.map((c) => esc(r[c])).join(',')),
    ];
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'dashboard-export.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <div className="relative min-w-0 flex-1">
          <svg
            className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-foreground/40"
            viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
          >
            <circle cx="11" cy="11" r="7" />
            <path strokeLinecap="round" d="m20 20-3.5-3.5" />
          </svg>
          <input
            value={query}
            onChange={(e) => { setQuery(e.target.value); setPage(0); }}
            placeholder="Filter…"
            className="w-full rounded-lg border border-border bg-muted/40 py-1.5 pl-8 pr-2 text-xs text-foreground outline-none transition-colors placeholder:text-foreground/40 focus:border-ring"
          />
        </div>
        <div className="flex shrink-0 items-center gap-1.5 text-[11px] text-foreground/50">
          <span className="tabular-nums">{sorted.length}</span>
          <span>rows</span>
        </div>
        <button
          type="button"
          onClick={exportCsv}
          title="Export CSV"
          aria-label="Export CSV"
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-border text-foreground/60 transition-colors hover:bg-muted hover:text-foreground"
        >
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
          </svg>
        </button>
      </div>
      <div className="max-h-56 flex-1 overflow-auto rounded-lg border border-border">
        <table className="w-full text-left text-xs">
          <thead className="sticky top-0 z-10 bg-muted">
            <tr>
              {columns.map((c) => (
                <th
                  key={c}
                  onClick={() => toggleSort(c)}
                  className="cursor-pointer select-none whitespace-nowrap px-3 py-2 font-semibold uppercase tracking-wide text-foreground/60 transition-colors hover:text-foreground"
                  aria-sort={sortKey === c ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  <span className="inline-flex items-center gap-1">
                    {c}
                    <svg
                      className={`h-3 w-3 transition-opacity ${sortKey === c ? 'opacity-70' : 'opacity-25'}`}
                      viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
                    >
                      {sortKey === c && sortDir === 'asc' ? (
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
                      ) : (
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                      )}
                    </svg>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pageRows.length ? (
              pageRows.map((r, i) => {
                // Decision-center signal table: optional row tinting from a
                // dedicated column (values good|bad|warn|none), action pills
                // (options.pills), per-column tone coloring (options.tone_columns)
                // and direction signal chips (options.signal_column).
                const rowTone = options?.row_tone_column ? r[options.row_tone_column] : null;
                const rowCls = rowTone === 'good' || rowTone === 'bad' || rowTone === 'warn'
                  ? ` row-${rowTone}` : '';
                const pillColumn = options?.pills?.column;
                const pillMap = options?.pills?.map || {};
                const toneCols = options?.tone_columns || {};
                const signalCol = options?.signal_column;
                return (
                  <tr key={i} className={`border-t border-border transition-colors hover:bg-muted/50${rowCls}`}>
                    {columns.map((c) => {
                      const raw = r[c];
                      if (c === pillColumn && raw != null && pillMap[String(raw)]) {
                        return (
                          <td key={c} className="whitespace-nowrap px-3 py-1.5">
                            <span className={`dc-pill dc-pill-${pillMap[String(raw)]}`}>{raw}</span>
                          </td>
                        );
                      }
                      if (c === signalCol && raw != null) {
                        const sig = String(raw).toLowerCase();
                        const up = sig === 'up' || sig === 'rise' || sig === '升' || sig === '涨';
                        const down = sig === 'down' || sig === 'fall' || sig === '跌' || sig === '弱';
                        return (
                          <td key={c} className="whitespace-nowrap px-3 py-1.5">
                            <span className={`dc-fsig ${up ? 'tone-up' : down ? 'tone-down' : 'tone-neutral'}`}>
                              {up ? '↗ ' : down ? '↘ ' : '→ '}{raw}
                            </span>
                          </td>
                        );
                      }
                      const tone = toneCols[c];
                      return (
                        <td
                          key={c}
                          className={`whitespace-nowrap px-3 py-1.5 tabular-nums${tone ? ` ${tone === 'up' ? 'tone-up' : tone === 'down' ? 'tone-down' : tone === 'warn' ? 'tone-warn' : 'tone-neutral'}` : ''} text-foreground/80`}
                        >
                          {raw == null ? '' : String(raw)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={columns.length} className="px-3 py-6 text-center text-foreground/45">
                  No rows match “{query}”
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {pageCount > 1 && (
        <div className="flex items-center justify-between text-[11px] text-foreground/50">
          <span className="tabular-nums">
            {safePage * PAGE_SIZE + 1}–{Math.min((safePage + 1) * PAGE_SIZE, sorted.length)} of {sorted.length}
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              disabled={safePage === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="rounded-md border border-border px-2 py-1 transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-40"
            >
              Prev
            </button>
            <span className="tabular-nums px-1">{safePage + 1} / {pageCount}</span>
            <button
              type="button"
              disabled={safePage >= pageCount - 1}
              onClick={() => setPage((p) => Math.min(pageCount - 1, p + 1))}
              className="rounded-md border border-border px-2 py-1 transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}
      <TopItemCallout topItem={options?.topItem} label="Top" />
    </div>
  );
}

function GaugeWidget({ objects, options }) {
  if (!objects.length) return <Empty />;
  const keys = numericKeys(objects, '');
  if (!keys.length) return <Empty />;
  const value = Number(objects[0][keys[0]]) || 0;
  const max = Number(options?.max) || 100;
  const color = options?.color || palette()[0];
  const data = [{ name: keys[0], value: Math.max(0, Math.min(value, max)) }];
  return (
    <div className="flex h-[220px] flex-col items-center">
      <ResponsiveContainer width="100%" height={170}>
        <RadialBarChart data={data} innerRadius="70%" outerRadius="100%" startAngle={210} endAngle={-30}>
          <PolarAngleAxis type="number" domain={[0, max]} angleAxisId={0} tick={false} />
          <RadialBar dataKey="value" fill={color} background={{ fill: 'var(--ds-border)' }} cornerRadius={8} {...CHART_ANIMATION} />
        </RadialBarChart>
      </ResponsiveContainer>
      <div className="-mt-10 font-heading text-xl font-bold tabular-nums" style={{ color }}>
        {fmtNumber(value)}
        <span className="ml-1 text-xs font-medium text-foreground/50">/ {fmtNumber(max, 0)}</span>
      </div>
    </div>
  );
}

function RadarWidget({ objects }) {
  if (!objects.length) return <Empty />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty />;
  return (
    <ResponsiveContainer width="100%" height={220}>
      <RadarChart data={objects} {...CHART_ANIMATION}>
        <PolarGrid stroke="var(--ds-border)" />
        <PolarAngleAxis dataKey={xKey} tick={{ fontSize: 10, fill: 'var(--ds-foreground)' }} />
        {keys.map((k, i) => (
          <Radar
            key={k}
            name={k}
            dataKey={k}
            stroke={palette()[i % palette().length]}
            fill={palette()[i % palette().length]}
            fillOpacity={0.18}
          />
        ))}
        <Legend wrapperStyle={{ fontSize: 12, color: 'var(--ds-foreground)' }} />
        <Tooltip {...tooltipStyle()} />
      </RadarChart>
    </ResponsiveContainer>
  );
}

/**
 * Sparkline — compact trend card (the Ecisco CEO "product signal card" look):
 * a small no-axis line chart (with optional area fill), plus an optional
 * action pill (options.pill + options.pill_tone: up|down|warn|neutral) and
 * an optional confidence badge (options.confidence). Data = label + value cols.
 */
function SparklineWidget({ objects, options }) {
  if (!objects.length) return <Empty compact />;
  const xKey = Object.keys(objects[0])[0];
  const keys = numericKeys(objects, xKey);
  if (!keys.length) return <Empty compact />;
  const key = keys[keys.length - 1];
  const vals = objects.map((o) => Number(o[key]) || 0);
  const color = options?.color || palette()[0];
  const pillTone = options?.pill_tone || 'neutral';
  const pillCls = `dc-pill dc-pill-${pillTone}`;
  const conf = options?.confidence != null ? Number(options.confidence) : null;
  const confCls = conf >= 75 ? 'tone-up' : conf >= 60 ? 'tone-warn' : 'tone-down';
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 text-xs text-foreground/70">
          <div className="truncate font-medium text-foreground">{options?.label || key}</div>
          {options?.sub ? <div className="mt-0.5 text-[10px] text-foreground/45">{options.sub}</div> : null}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {options?.pill ? <span className={pillCls}>{options.pill}</span> : null}
          {conf != null ? (
            <span className={`dc-fsig ${confCls}`}>conf {conf}%</span>
          ) : null}
        </div>
      </div>
      <div className="h-16">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={objects} margin={{ top: 4, right: 2, left: 2, bottom: 0 }}>
            <defs>
              <linearGradient id={`spk${String(key).replace(/\W/g, '')}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.22} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <Area
              type="monotone"
              dataKey={key}
              stroke={color}
              strokeWidth={1.6}
              fill={options?.area === false ? 'transparent' : `url(#spk${String(key).replace(/\W/g, '')})`}
              dot={false}
              isAnimationActive={false}
            />
            <Tooltip {...tooltipStyle()} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

/**
 * Renders one metric (any widget type) inside a Card.
 *
 * - `data` may be undefined (still loading) → skeleton
 * - `data.error` → meaningful empty state with the error message
 * - dict OR array rows are accepted (see rowsToObjects)
 */
export function Widget({ metric, data, className = '' }) {
  const error = data?.error;
  const loading = !data;
  const columns = data?.columns || [];
  const rows = data?.rows || [];
  const objects = rowsToObjects(columns, rows);
  const options = metric.options || {};

  let body;
  switch (metric.type) {
    case 'kpi':
      body = <KpiWidget objects={objects} options={options} />;
      break;
    case 'line':
    case 'area':
      body = <LineWidget objects={objects} type={metric.type} options={options} />;
      break;
    case 'bar':
      body = <BarWidget objects={objects} options={options} />;
      break;
    case 'combo':
      body = <ComboWidget objects={objects} options={options} />;
      break;
    case 'pie':
      body = <PieWidget objects={objects} options={options} />;
      break;
    case 'table':
      body = <TableWidget objects={objects} columns={columns} options={options} />;
      break;
    case 'gauge':
      body = <GaugeWidget objects={objects} options={options} />;
      break;
    case 'radar':
      body = <RadarWidget objects={objects} />;
      break;
    case 'sparkline':
      body = <SparklineWidget objects={objects} options={options} />;
      break;
    default:
      body = <Empty error={`Unknown widget type: ${metric.type}`} />;
  }

  return (
    <Card
      title={metric.title}
      className={className}
      status={error ? null : loading ? 'loading' : objects.length > 0 ? 'live' : null}
      hint={error || undefined}
    >
      {error ? <Empty error={error} /> : loading ? <Skeleton /> : body}
    </Card>
  );
}
