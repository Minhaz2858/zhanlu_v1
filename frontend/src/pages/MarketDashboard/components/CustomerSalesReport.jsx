import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import {
  PieChart, Pie, Cell, Tooltip as RcTooltip, ResponsiveContainer,
  LineChart, Line, XAxis, YAxis, CartesianGrid, Legend,
} from 'recharts';
import { reportsApi } from '@/api/reports';
import { TYPO } from '../lib/typography';
import { getChartTheme, rechartsTooltipStyle } from '../lib/chartTheme';

const DONUT_COLORS = ['#3B82F6', '#6366F1', '#8B5CF6', '#A855F7', '#D946EF', '#E2E8F0'];
const LINE_COLORS = ['#3B82F6', '#6366F1', '#8B5CF6', '#A855F7', '#D946EF', '#F59E0B', '#10B981'];

function YoYBadge({ yoy }) {
  if (yoy == null || yoy === 0) {
    return <Minus className="inline h-3.5 w-3.5 text-slate-400" />;
  }
  const up = yoy > 0;
  const color = up ? 'text-red-600 bg-red-50' : 'text-green-600 bg-green-50';
  const Icon = up ? TrendingUp : TrendingDown;
  return (
    <span className={`inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-xs font-medium ${color}`}>
      <Icon className="h-3 w-3" />
      {Math.abs(yoy).toFixed(1)}%
    </span>
  );
}

export default function CustomerSalesReport({ org }) {
  const [month, setMonth] = useState('');
  const [material, setMaterial] = useState('');

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['reports', 'customer-sales', { org, month, material }],
    queryFn: () => reportsApi.customerSales({ org, month, material }),
    staleTime: 60_000,
  });

  // Derive available months from data, fall back to empty
  const availableMonths = data?.available_months || [];
  const availableMaterials = data?.available_materials || [];

  // Set default month to latest available
  if (!month && availableMonths.length > 0 && !isLoading) {
    // Don't call setMonth during render — use callback below
  }
  const effectiveMonth = month || availableMonths[availableMonths.length - 1] || '';

  // KPI card component
  const KpiCard = ({ title, value, unit, yoy }) => (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <div className={TYPO.secondaryData}>{title}</div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className={TYPO.priceData}>
          {value != null ? Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '--'}
        </span>
        {unit && <span className="text-xs text-slate-500">{unit}</span>}
      </div>
      <div className="mt-1.5">
        <YoYBadge yoy={yoy} />
      </div>
    </div>
  );

  if (isLoading) {
    return <div className="py-8 text-center text-sm text-slate-500">Loading customer sales data…</div>;
  }

  if (isError) {
    return (
      <div className="rounded border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-400">
        Failed to load customer sales: {error?.message || 'unknown'}
      </div>
    );
  }

  if (!data) return null;

  const s = data.summary || {};
  const theme = getChartTheme(false);
  const ttStyle = rechartsTooltipStyle(theme, false);

  // ── Filters ──
  const filters = (
    <div className="flex flex-wrap items-center gap-3 mb-5">
      <select
        value={month || effectiveMonth}
        onChange={(e) => setMonth(e.target.value)}
        className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-700 focus:border-slate-400 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
      >
        {availableMonths.map((m) => (
          <option key={m} value={m}>{m}</option>
        ))}
      </select>
      <select
        value={material}
        onChange={(e) => setMaterial(e.target.value)}
        className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-sm text-slate-700 focus:border-slate-400 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300"
      >
        <option value="">所有物料</option>
        {availableMaterials.map((m) => (
          <option key={m} value={m}>{m}</option>
        ))}
      </select>
      <span className={TYPO.secondaryData}>
        数据截止: {effectiveMonth || '--'}
      </span>
    </div>
  );

  // ── KPI Cards ──
  const kpiCards = (
    <div className="grid grid-cols-2 gap-4 mb-6">
      <KpiCard title="销售额" value={s.total_sales_amount_wan} unit="万元" yoy={s.sales_amount_yoy} />
      <KpiCard title="销量" value={s.total_sales_qty} unit="吨" yoy={s.sales_qty_yoy} />
    </div>
  );

  // ── Customer Share Donut ──
  const donut = data.customer_share && data.customer_share.length > 0 ? (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <div className={TYPO.cardTitle}>客户销量占比</div>
      <div className="mt-2" style={{ width: '100%', height: 240 }}>
        <ResponsiveContainer>
          <PieChart>
            <Pie
              data={data.customer_share}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              outerRadius={80}
              innerRadius={45}
              paddingAngle={2}
            >
              {data.customer_share.map((entry, i) => (
                <Cell key={entry.name} fill={DONUT_COLORS[i % DONUT_COLORS.length]} />
              ))}
            </Pie>
            <RcTooltip contentStyle={ttStyle} formatter={(v) => `${Number(v).toLocaleString()} 吨`} />
            <Legend
              verticalAlign="bottom"
              wrapperStyle={{ fontSize: 11, color: theme.tick }}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
    </div>
  ) : null;

  // ── Weekly Price Chart ──
  const priceChart = data.price_chart && data.price_chart.length > 0 ? (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <div className={TYPO.cardTitle}>周度价格走势</div>
      <div className="mt-2" style={{ width: '100%', height: 260 }}>
        <ResponsiveContainer>
          <LineChart data={data.price_chart}>
            <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
            <XAxis dataKey="week" tick={{ fontSize: 11, fill: theme.tick }} />
            <YAxis tick={{ fontSize: 11, fill: theme.tick }} />
            <RcTooltip contentStyle={ttStyle} formatter={(v) => `¥${Number(v).toLocaleString()}`} />
            <Legend wrapperStyle={{ fontSize: 11, color: theme.tick }} />
            {data.products.slice(0, 5).map((p, i) => (
              <Line
                key={p}
                type="monotone"
                dataKey={p}
                stroke={LINE_COLORS[i % LINE_COLORS.length]}
                strokeWidth={2}
                dot={false}
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  ) : null;

  // ── Weekly Sales Chart ──
  const salesChart = data.sales_chart && data.sales_chart.length > 0 ? (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <div className={TYPO.cardTitle}>周度销量分布</div>
      <div className="mt-2" style={{ width: '100%', height: 260 }}>
        <ResponsiveContainer>
          <LineChart data={data.sales_chart}>
            <CartesianGrid strokeDasharray="3 3" stroke={theme.grid} />
            <XAxis dataKey="week" tick={{ fontSize: 11, fill: theme.tick }} />
            <YAxis tick={{ fontSize: 11, fill: theme.tick }} />
            <RcTooltip contentStyle={ttStyle} formatter={(v) => `${Number(v).toLocaleString()} 吨`} />
            <Legend wrapperStyle={{ fontSize: 11, color: theme.tick }} />
            {data.products.slice(0, 5).map((p, i) => (
              <Line
                key={p}
                type="monotone"
                dataKey={p}
                stroke={LINE_COLORS[i % LINE_COLORS.length]}
                strokeWidth={2}
                dot={false}
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  ) : null;

  // ── Product Growth List ──
  const growthList = data.product_growth && data.product_growth.length > 0 ? (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <div className={TYPO.cardTitle}>产品同比增长</div>
      <div className="mt-2 space-y-1.5">
        {data.product_growth.map((item) => (
          <div key={item.product} className="flex items-center justify-between rounded px-2 py-1 hover:bg-slate-50 dark:hover:bg-slate-750">
            <span className="text-sm text-slate-700 dark:text-slate-300">{item.product}</span>
            <div className="flex items-center gap-3">
              <span className="text-sm font-mono text-slate-600 dark:text-slate-400">
                {item.qty.toLocaleString()} 吨
              </span>
              <YoYBadge yoy={item.yoy} />
            </div>
          </div>
        ))}
      </div>
    </div>
  ) : null;

  // ── Sales Detail Table ──
  const detailTable = data.sales_detail && data.sales_detail.length > 0 ? (
    <div className="rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-800 overflow-hidden">
      <div className="border-b border-slate-200 p-4 dark:border-slate-700">
        <span className={TYPO.cardTitle}>销售明细</span>
        <span className={`ml-2 ${TYPO.secondaryData}`}>Top 50</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-800">
              <th className="px-4 py-2 text-left text-xs font-semibold text-slate-600 dark:text-slate-400">产品</th>
              <th className="px-4 py-2 text-left text-xs font-semibold text-slate-600 dark:text-slate-400">客户</th>
              {(data.weeks || []).slice(0, 6).map((w) => (
                <th key={w} className="px-2 py-2 text-right text-xs font-semibold text-slate-600 dark:text-slate-400">{w}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.sales_detail.map((row, idx) => (
              <tr
                key={`${row.product}-${row.customer}-${idx}`}
                className="border-b border-slate-100 hover:bg-slate-50 dark:border-slate-800 dark:hover:bg-slate-750"
              >
                <td className="px-4 py-2 text-slate-800 dark:text-slate-200">{row.product}</td>
                <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{row.customer}</td>
                {(row.weeks || []).slice(0, 6).map((w, wi) => (
                  <td key={wi} className="px-2 py-2 text-right font-mono text-slate-700 dark:text-slate-300">
                    {w.qty > 0 ? w.qty.toLocaleString() : '-'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  ) : null;

  return (
    <div className="space-y-6">
      {filters}
      {kpiCards}

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {donut}
        {growthList}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {priceChart}
        {salesChart}
      </div>

      {detailTable}

      {data.debug && (
        <details className="rounded border border-amber-200 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-950">
          <summary className="cursor-pointer text-xs font-semibold text-amber-800 dark:text-amber-400">
            Debug: 价格来源诊断
          </summary>
          <pre className="mt-2 max-h-60 overflow-auto text-[10px] text-amber-700 dark:text-amber-300">
            {JSON.stringify(data.debug, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
