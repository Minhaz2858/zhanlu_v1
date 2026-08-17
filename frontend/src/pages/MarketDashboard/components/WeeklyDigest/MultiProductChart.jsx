import { useMemo } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts';
import { getChartTheme, rechartsTooltipStyle } from '../../lib/chartTheme';

// 13 distinct colors — picked to be color-blind friendly, ordered to spread
// across the hue circle so adjacent lines in the chart are easy to tell apart.
const SERIES_COLORS = [
  '#3B82F6', // blue
  '#EF4444', // red
  '#10B981', // emerald
  '#F59E0B', // amber
  '#8B5CF6', // violet
  '#06B6D4', // cyan
  '#F97316', // orange
  '#EC4899', // pink
  '#84CC16', // lime
  '#6366F1', // indigo
  '#14B8A6', // teal
  '#A855F7', // purple
  '#0EA5E9', // sky
];

// Multi-product price trend chart. Each product is normalized to 100 at the
// first data point so relative performance is easy to compare on one chart.
export default function MultiProductChart({ products, field = 'price_30d', height = 320, mode = 'rebased' }) {
  const theme = getChartTheme();

  // Filter products that have data
  const valid = (products || []).filter((p) => Array.isArray(p[field]) && p[field].length > 1);

  const { chartData, productKeys, minP, maxP } = useMemo(() => {
    if (!valid.length) return { chartData: [], productKeys: [], minP: 0, maxP: 0 };

    // Build a union of dates across all products
    const dateSet = new Set();
    for (const p of valid) for (const d of p[field]) if (d?.date) dateSet.add(d.date);
    const dates = Array.from(dateSet).sort();
    if (dates.length < 2) return { chartData: [], productKeys: [], minP: 0, maxP: 0 };

    // Index each product's rows by date
    const indexed = valid.map((p) => {
      const byDate = new Map((p[field] || []).map((d) => [d.date, d.price]));
      return { key: p.product_key, label: p.product_name_zh || p.product_key, byDate };
    });

    const rows = [];
    let allMin = Infinity, allMax = -Infinity;
    for (const date of dates) {
      const row = { date, fullDate: date };
      for (const p of indexed) {
        const v = p.byDate.get(date);
        if (v == null) { row[p.key] = null; continue; }
        row[p.key] = mode === 'rebased' ? null : v; // backfill below
        if (mode !== 'rebased') {
          allMin = Math.min(allMin, v);
          allMax = Math.max(allMax, v);
        }
      }
      rows.push(row);
    }

    if (mode === 'rebased') {
      // Compute rebased values per product (first non-null = 100)
      for (const p of indexed) {
        const baseRow = rows.find((r) => r[p.key] != null);
        if (!baseRow) continue;
        const baseVal = baseRow[p.key];
        if (!baseVal) continue;
        for (const row of rows) {
          const v = row[p.key];
          row[p.key] = v == null ? null : (v / baseVal) * 100;
        }
      }
      allMin = 80;
      allMax = 120;
    }

    return {
      chartData: rows,
      productKeys: indexed.map((p) => ({ key: p.key, label: p.label })),
      minP: allMin,
      maxP: allMax,
    };
  }, [valid, field, mode]);

  if (!valid.length || !chartData.length) {
    return (
      <div className="flex items-center justify-center text-xs text-slate-400 h-[320px]">
        Not enough products with price data to compare
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={chartData} margin={{ top: 5, right: 20, left: 5, bottom: 5 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 10, fill: theme.tick }}
          interval="preserveStartEnd"
          tickFormatter={(d) => (d ? d.slice(5) : d)}
        />
        <YAxis
          domain={mode === 'rebased' ? [Math.floor(minP), Math.ceil(maxP)] : [Math.floor(minP * 0.95), Math.ceil(maxP * 1.05)]}
          tick={{ fontSize: 10, fill: theme.tick }}
          tickFormatter={(v) => (mode === 'rebased' ? v.toFixed(0) : v.toLocaleString())}
        />
        <Tooltip
          {...rechartsTooltipStyle(theme)}
          labelFormatter={(label) => `日期: ${label}`}
          formatter={(value, name) => {
            if (value == null) return ['-', name];
            return [mode === 'rebased' ? `${value.toFixed(2)}` : `${value.toLocaleString()} 元/吨`, name];
          }}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        {productKeys.map((p, i) => (
          <Line
            key={p.key}
            type="monotone"
            dataKey={p.key}
            name={p.label}
            stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
            strokeWidth={1.5}
            dot={false}
            connectNulls
            activeDot={{ r: 3 }}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
