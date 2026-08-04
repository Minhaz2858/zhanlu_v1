// DecisionBoard — A+B hybrid: decision matrix (all products at once) with
// expandable rows revealing a mini forecast chart + full AI narrative.
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Sparkles, ChevronDown } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';

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

function DecisionRow({ p, isSelected, isExpanded, onSelect, onToggle }) {
  const chgCls =
    p.expected_change_pct == null
      ? 'text-slate-400'
      : p.expected_change_pct >= 0
        ? 'text-red-600 dark:text-red-400'   // 涨=red (CN convention)
        : 'text-green-600 dark:text-green-400';
  return (
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
  );
}
