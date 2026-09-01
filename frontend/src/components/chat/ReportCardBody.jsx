/**
 * ReportCardBody — shared report body rendered by both the inline
 * ReportCard and the side ReportSidePanel.  Extracted from ReportCard.jsx
 * so both surfaces stay visually identical.
 *
 * Props:
 *   payload   — ReportCardPayload { kpis, chart, insights, warnings, summary }
 *
 * (Next-step question + suggested-action chips are intentionally not rendered
 * here — the inline ReportCard is the source of truth; the side panel re-uses
 * the same body so removing the noise from one removes it from both.)
 */

import { useEffect, useState } from 'react';
import {
  ResponsiveContainer, BarChart, Bar, LineChart, Line, PieChart, Pie, Cell,
  XAxis, YAxis, Tooltip, CartesianGrid, Legend,
} from 'recharts';
import {
  Lightbulb, AlertTriangle, TrendingUp, TrendingDown, Sparkles, Target,
  Star, ShieldAlert, CheckCircle2, Loader2,
} from 'lucide-react';
import { motion } from 'framer-motion';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Icon map — string → lucide component
// ---------------------------------------------------------------------------

const ICON_MAP = {
  lightbulb: Lightbulb,
  trending_up: TrendingUp,
  trending_down: TrendingDown,
  sparkles: Sparkles,
  target: Target,
  star: Star,
  shield_alert: ShieldAlert,
  check: CheckCircle2,
  alert: AlertTriangle,
};

function InsightIcon({ name, className }) {
  const Icon = ICON_MAP[name] || Lightbulb;
  return <Icon className={className} />;
}

// ---------------------------------------------------------------------------
// Tiny number formatter (1,234 / 1.2k / 1.5M)
// ---------------------------------------------------------------------------

export function formatNumber(n) {
  if (n == null || n === '') return '\u2014';
  const num = typeof n === 'number' ? n : Number(String(n).replace(/[^0-9.\-]/g, ''));
  if (!isFinite(num)) return String(n);
  if (Math.abs(num) >= 1e9) return `${(num / 1e9).toFixed(2)}B`;
  if (Math.abs(num) >= 1e6) return `${(num / 1e6).toFixed(2)}M`;
  if (Math.abs(num) >= 1e3) return `${(num / 1e3).toFixed(1)}k`;
  return num.toLocaleString();
}

// Compact axis formatter — trims useless trailing zeros so tick labels stay
// short ("600M" / "1.5B" instead of "600.00M" / "1.50B"). Long labels were
// being clipped by the default YAxis width ("600.00M" rendered as "00.00M").
export function formatAxisNumber(n) {
  if (n == null || n === '') return '';
  const num = typeof n === 'number' ? n : Number(String(n).replace(/[^0-9.\-]/g, ''));
  if (!isFinite(num)) return String(n);
  const trim = (v) => String(parseFloat(v.toFixed(2)));
  if (Math.abs(num) >= 1e9) return `${trim(num / 1e9)}B`;
  if (Math.abs(num) >= 1e6) return `${trim(num / 1e6)}M`;
  if (Math.abs(num) >= 1e3) return `${trim(num / 1e3)}k`;
  return trim(num);
}

// ---------------------------------------------------------------------------
// Animated KPI counter (counts up on mount)
// ---------------------------------------------------------------------------

function AnimatedKpiValue({ value }) {
  const [display, setDisplay] = useState(typeof value === 'string' ? value : '0');

  useEffect(() => {
    if (typeof value === 'string') {
      setDisplay(value);
      return;
    }
    const target = Number(value);
    if (!isFinite(target)) {
      setDisplay(String(value));
      return;
    }
    const duration = 600;
    const start = performance.now();
    const initial = 0;
    let raf;
    const tick = (t) => {
      const p = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      const current = initial + (target - initial) * eased;
      setDisplay(formatNumber(current));
      if (p < 1) raf = requestAnimationFrame(tick);
      else setDisplay(formatNumber(target));
    };
    raf = requestAnimationFrame(tick);
    return () => raf && cancelAnimationFrame(raf);
  }, [value]);

  return <span>{display}</span>;
}

// ---------------------------------------------------------------------------
// Chart skeleton (200ms lazy-load)
// ---------------------------------------------------------------------------

function ChartSkeleton({ height = 260 }) {
  return (
    <div
      className="flex w-full items-center justify-center rounded-lg bg-gradient-to-r from-secondary/30 via-secondary/60 to-secondary/30"
      style={{ height }}
    >
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Rendering chart\u2026
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recharts Custom Tooltip
// ---------------------------------------------------------------------------

function ChartTooltip({ active, payload, label, unit }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div className="rounded-lg border border-border/60 bg-card/95 px-3 py-2 text-xs shadow-lg backdrop-blur-md">
      {label != null && <div className="mb-1 font-medium text-foreground">{label}</div>}
      {payload.map((p, i) => (
        <div key={i} className="flex items-center gap-2 text-muted-foreground">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: p.color || p.fill || '#2563EB' }}
          />
          <span className="font-medium text-foreground">
            {p.name}: {formatNumber(p.value)}{unit ? ` ${unit}` : ''}
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Chart renderer
// ---------------------------------------------------------------------------

const CHART_COLORS = ['#2563EB', '#10B981', '#F59E0B', '#EF4444', '#0EA5E9', '#8B5CF6'];

function ReportChart({ chart }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setReady(true), 200);
    return () => clearTimeout(t);
  }, []);

  const data = Array.isArray(chart.data) ? chart.data : [];
  const type = (chart.type || 'bar').toLowerCase();
  const unit = chart.unit || '';
  const yKeys = Array.isArray(chart.y_keys) && chart.y_keys.length > 0
    ? chart.y_keys
    : Object.keys(data[0] || {}).filter((k) => k !== chart.x_key);

  if (!ready) return <ChartSkeleton />;
  if (data.length === 0) {
    return (
      <div className="flex h-[200px] w-full items-center justify-center rounded-lg border border-dashed border-border bg-secondary/20 text-xs text-muted-foreground">
        No chart data
      </div>
    );
  }

  if (type === 'pie') {
    const valueKey = yKeys[0] || 'value';
    return (
      <ResponsiveContainer width="100%" height={260}>
        <PieChart>
          <Tooltip content={<ChartTooltip unit={unit} />} />
          <Legend wrapperStyle={{ fontSize: 11, color: '#94A3B8' }} iconType="circle" />
          <Pie data={data} dataKey={valueKey} nameKey={chart.x_key} cx="50%" cy="50%"
               innerRadius={45} outerRadius={90} paddingAngle={2}
               stroke="#0F172A" strokeWidth={1} isAnimationActive animationDuration={700}>
            {data.map((_, i) => (
              <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
    );
  }

  if (type === 'line') {
    return (
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            {yKeys.map((k, i) => (
              <linearGradient id={`line-grad-${k}`} key={k} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0.4} />
                <stop offset="100%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.18)" />
          <XAxis dataKey={chart.x_key} tick={{ fontSize: 11, fill: '#94A3B8' }}
                 axisLine={{ stroke: 'rgba(148, 163, 184, 0.3)' }} tickLine={false} />
          <YAxis width={52} tick={{ fontSize: 11, fill: '#94A3B8' }}
                 axisLine={{ stroke: 'rgba(148, 163, 184, 0.3)' }} tickLine={false}
                 tickFormatter={(v) => formatAxisNumber(v)} />
          <Tooltip content={<ChartTooltip unit={unit} />}
                   cursor={{ stroke: '#2563EB', strokeWidth: 1, strokeDasharray: '3 3' }} />
          {yKeys.map((k, i) => (
            <Line key={k} type="monotone" dataKey={k} name={k}
                  stroke={CHART_COLORS[i % CHART_COLORS.length]} strokeWidth={2.5}
                  dot={{ r: 3, fill: CHART_COLORS[i % CHART_COLORS.length], strokeWidth: 0 }}
                  activeDot={{ r: 5 }} isAnimationActive animationDuration={700} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    );
  }

  // default: bar
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <defs>
          {yKeys.map((k, i) => (
            <linearGradient id={`bar-grad-${k}`} key={k} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0.95} />
              <stop offset="100%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0.55} />
            </linearGradient>
          ))}
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.18)" vertical={false} />
        <XAxis dataKey={chart.x_key} tick={{ fontSize: 11, fill: '#94A3B8' }}
               axisLine={{ stroke: 'rgba(148, 163, 184, 0.3)' }} tickLine={false} />
        <YAxis width={52} tick={{ fontSize: 11, fill: '#94A3B8' }}
               axisLine={{ stroke: 'rgba(148, 163, 184, 0.3)' }} tickLine={false}
               tickFormatter={(v) => formatAxisNumber(v)} />
        <Tooltip content={<ChartTooltip unit={unit} />}
                 cursor={{ fill: 'rgba(37, 99, 235, 0.06)' }} />
        {yKeys.map((k, i) => (
          <Bar key={k} dataKey={k} name={k} fill={`url(#bar-grad-${k})`}
               radius={[6, 6, 0, 0]} isAnimationActive animationDuration={700} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Main ReportCardBody
// ---------------------------------------------------------------------------

export default function ReportCardBody({ payload }) {
  if (!payload) return null;

  const {
    summary = '',
    kpis = [],
    chart = null,
    insights = [],
    warnings = [],
  } = payload;

  return (
    <>
      {/* Summary */}
      {summary && (
        <div className="border-b border-border/40 bg-secondary/20 px-4 py-2.5 text-xs leading-relaxed text-foreground/90">
          {summary}
        </div>
      )}

      {/* KPI row */}
      {kpis.length > 0 && (
        <div className="grid grid-cols-2 gap-2 px-4 py-3 sm:grid-cols-4">
          {kpis.slice(0, 4).map((kpi, i) => {
            const delta = kpi.delta || '';
            const isPositive = delta.trim().startsWith('+');
            const isNegative = delta.trim().startsWith('-') || delta.trim().startsWith('\u2193');
            return (
              <motion.div
                key={i}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.05, duration: 0.2 }}
                className={cn(
                  'rounded-xl border border-border/60 bg-gradient-to-br',
                  'from-card to-card/60 p-2.5',
                  'transition-shadow hover:shadow-sm'
                )}
              >
                <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  {kpi.label}
                </div>
                <div className="mt-0.5 text-lg font-semibold text-foreground">
                  <AnimatedKpiValue value={kpi.value} />
                </div>
                <div className="mt-0.5 flex items-center gap-1 text-[10px]">
                  {delta && (
                    <span
                      className={cn(
                        'inline-flex items-center gap-0.5 font-medium',
                        isPositive && 'text-emerald-600 dark:text-emerald-400',
                        isNegative && 'text-red-500 dark:text-red-400',
                        !isPositive && !isNegative && 'text-muted-foreground'
                      )}
                    >
                      {isPositive && <TrendingUp className="h-2.5 w-2.5" />}
                      {isNegative && <TrendingDown className="h-2.5 w-2.5" />}
                      {delta}
                    </span>
                  )}
                  {kpi.caption && (
                    <span className="truncate text-muted-foreground">{kpi.caption}</span>
                  )}
                </div>
              </motion.div>
            );
          })}
        </div>
      )}

      {/* Chart */}
      {chart && chart.data && chart.data.length > 0 && (
        <div className="border-t border-border/40 px-4 py-3">
          {chart.title && (
            <div className="mb-2 text-xs font-medium text-foreground">{chart.title}</div>
          )}
          <ReportChart chart={chart} />
        </div>
      )}

      {/* Insights */}
      {insights.length > 0 && (
        <div className="border-t border-border/40 px-4 py-3">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Insights
          </div>
          <ul className="space-y-1.5">
            {insights.slice(0, 5).map((ins, i) => (
              <li key={i} className="flex items-start gap-2 text-xs leading-relaxed text-foreground/90">
                <InsightIcon name={ins.icon} className="mt-0.5 h-3.5 w-3.5 shrink-0 text-blue-500" />
                <span>{ins.text}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Warnings */}
      {warnings && warnings.length > 0 && (
        <div className="mx-4 mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          {warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-1.5">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
