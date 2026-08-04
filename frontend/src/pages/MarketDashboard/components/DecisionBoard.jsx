// DecisionBoard — A+B hybrid: decision matrix (all products at once) with
// expandable rows revealing a mini forecast chart + full AI narrative.
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer, ComposedChart, Area, Line, XAxis, YAxis, Tooltip,
} from 'recharts';
import { Sparkles, ChevronDown } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { normalizeForecastPoints } from '../lib/forecastChartMath';

const ACTION_STYLES = {
  buy: 'bg-green-100 text-green-700 border-green-200 dark:bg-green-900/40 dark:text-green-300 dark:border-green-800',
  sell: 'bg-red-100 text-red-700 border-red-200 dark:bg-red-900/40 dark:text-red-300 dark:border-red-800',
  hold: 'bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-900/40 dark:text-blue-300 dark:border-blue-800',
  watch: 'bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-900/40 dark:text-amber-300 dark:border-amber-800',
};

const TRUST_DOTS = {
  green: 'bg-green-500',
  yellow: 'bg-yellow-500',
  orange: 'bg-orange-500',
  red: 'bg-red-500',
};

function fmtPrice(v) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function fmtPctFraction(v) {
  // 0.029 → "+2.9%"
  if (v == null) return '—';
  const pct = v * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

function fmtProb(v) {
  if (v == null) return '—';
  return `${Math.round(v * 100)}%`;
}

export default function DecisionBoard({ group, selectedId, onSelectProduct }) {
  const [expandedId, setExpandedId] = useState(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ['market-dashboard', 'decision-board', group],
    queryFn: () => marketDashboardApi.decisionBoard(group, 7),
    staleTime: 60_000,
  });

  if (isLoading) {
    return (
      <div className="mx-6 mt-4 rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-900">
        AI 决策看板加载中…
      </div>
    );
  }
  // Board is additive — never break the page on error.
  if (isError || !data || !Array.isArray(data.products) || !data.products.length) {
    return null;
  }

  return (
    <div className="mx-6 mt-4 overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
      <div className="border-b border-slate-100 px-4 py-3 dark:border-slate-800">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-violet-500" />
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
            下周决策看板 · AI Decision Board
          </h2>
          <span className="text-xs text-slate-400">未来 {data.day} 天</span>
        </div>
        <p className="mt-1 text-sm leading-relaxed text-slate-600 dark:text-slate-300">
          {data.briefing_zh}
        </p>
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-100 text-left text-xs text-slate-400 dark:border-slate-800">
            <th className="px-4 py-2 font-medium">物料</th>
            <th className="px-2 py-2 font-medium text-right">现价</th>
            <th className="px-2 py-2 font-medium text-right">7日预测</th>
            <th className="px-2 py-2 font-medium text-right">预期涨跌</th>
            <th className="px-2 py-2 font-medium text-right">上涨概率</th>
            <th className="px-2 py-2 font-medium">AI建议</th>
            <th className="px-2 py-2 font-medium">可靠度</th>
            <th className="px-2 py-2 font-medium">AI 简评</th>
            <th className="w-8 px-2 py-2" />
          </tr>
        </thead>
        <tbody>
          {data.products.map((p) => {
            const isSelected = p.id === selectedId;
            const isExpanded = p.id === expandedId;
            return (
              <DecisionRow
                key={p.id}
                p={p}
                day={data.day}
                isSelected={isSelected}
                isExpanded={isExpanded}
                onSelect={() => onSelectProduct?.(p.id)}
                onToggle={() => setExpandedId(isExpanded ? null : p.id)}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DecisionRow({ p, day, isSelected, isExpanded, onSelect, onToggle }) {
  const chgCls =
    p.expected_change_pct == null
      ? 'text-slate-400'
      : p.expected_change_pct >= 0
        ? 'text-red-600 dark:text-red-400'   // 涨=red (CN convention)
        : 'text-green-600 dark:text-green-400';
  return (
    <>
    <tr
      onClick={onSelect}
      className={`cursor-pointer border-b border-slate-50 transition-colors hover:bg-slate-50 dark:border-slate-800/60 dark:hover:bg-slate-800/40 ${
        isSelected ? 'bg-slate-50 dark:bg-slate-800/50' : ''
      }`}
    >
      <td className="px-4 py-2.5">
        <div className="font-medium text-slate-800 dark:text-slate-100">{p.name_zh}</div>
        <span className="rounded bg-slate-100 px-1 font-mono text-[10px] text-slate-500 dark:bg-slate-800 dark:text-slate-400">
          {p.code}
        </span>
      </td>
      <td className="px-2 py-2.5 text-right tabular-nums">{fmtPrice(p.current_price)}</td>
      <td className="px-2 py-2.5 text-right tabular-nums">{fmtPrice(p.forecast_base)}</td>
      <td className={`px-2 py-2.5 text-right tabular-nums ${chgCls}`}>
        {fmtPctFraction(p.expected_change_pct)}
      </td>
      <td className="px-2 py-2.5 text-right tabular-nums">{fmtProb(p.p_rise)}</td>
      <td className="px-2 py-2.5">
        <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${ACTION_STYLES[p.action] || ACTION_STYLES.watch}`}>
          {p.action_label_zh}
        </span>
      </td>
      <td className="px-2 py-2.5">
        <span className="inline-flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
          <span className={`h-2 w-2 rounded-full ${TRUST_DOTS[p.trust_color] || 'bg-slate-300'}`} />
          {p.trust_label_zh || '—'}
        </span>
      </td>
      <td className="max-w-[220px] truncate px-2 py-2.5 text-xs text-slate-500 dark:text-slate-400">
        {p.one_liner_zh}
      </td>
      <td className="px-2 py-2.5">
        <button
          type="button"
          aria-label={`展开 ${p.name_zh}`}
          onClick={(e) => { e.stopPropagation(); onToggle(); }}
          className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800"
        >
          <ChevronDown className={`h-4 w-4 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
        </button>
      </td>
    </tr>
    {isExpanded && (
      <tr className="border-b border-slate-100 dark:border-slate-800">
        <td colSpan={9} className="bg-slate-50/60 px-4 py-3 dark:bg-slate-800/30">
          <ExpandedCard p={p} day={day} />
        </td>
      </tr>
    )}
    </>
  );
}

const VOL_ZH = { NORMAL: '正常', MODERATE: '偏高', HIGH: '剧烈' };
const CONF_ZH = { high: '高', medium: '中', low: '低' };

function ExpandedCard({ p, day }) {
  const { data } = useQuery({
    queryKey: ['market-dashboard', 'forecast-chart', p.id, 30],
    queryFn: () => marketDashboardApi.forecastChart(p.id, 30),
    staleTime: 60_000,
  });
  const { points } = normalizeForecastPoints(data?.points);

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <div className="h-40 rounded border border-slate-100 bg-white p-2 dark:border-slate-700 dark:bg-slate-900">
        {points.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
              <XAxis dataKey="date" tick={{ fontSize: 10 }} tickFormatter={(d) => String(d).slice(5)} />
              <YAxis tick={{ fontSize: 10 }} domain={['auto', 'auto']} width={50} />
              <Tooltip formatter={(v) => (v == null ? '—' : Number(v).toLocaleString())} />
              <Area type="monotone" dataKey="actual" stroke="#2563eb" fill="#bfdbfe" strokeWidth={1.5} name="实际" />
              <Line type="monotone" dataKey="base" stroke="#f59e0b" strokeDasharray="4 2" dot={false} name={`${day}日预测`} />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-slate-400">
            走势图数据加载中…
          </div>
        )}
      </div>

      <div>
        <p className="text-sm leading-relaxed text-slate-700 dark:text-slate-200">
          {p.narrative_zh}
        </p>
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400 sm:grid-cols-4">
          <div>方向准确率<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{p.directional_accuracy == null ? '—' : `${Math.round(p.directional_accuracy * 100)}%`}</span></div>
          <div>情报事件<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{p.intel_event_count} 条</span></div>
          <div>波动状态<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{VOL_ZH[p.volatility_regime] || '—'}</span></div>
          <div>置信度<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{CONF_ZH[p.confidence] || '低'}</span></div>
        </div>
        {p.skus?.length > 0 && (
          <table className="mt-3 w-full text-xs">
            <thead>
              <tr className="text-left text-slate-400">
                <th className="py-1 font-medium">物料代码</th>
                <th className="py-1 font-medium">名称</th>
                <th className="py-1 font-medium text-right">预测价</th>
              </tr>
            </thead>
            <tbody>
              {p.skus.map((s) => (
                <tr key={s.material_code} className="border-t border-slate-100 dark:border-slate-700">
                  <td className="py-1 font-mono">{s.material_code}</td>
                  <td className="py-1">{s.name}</td>
                  <td className="py-1 text-right tabular-nums">{fmtPrice(s.forecast_base)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
