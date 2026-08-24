// DecisionBoard — A+B hybrid: decision matrix (all products at once) with
// expandable rows revealing a mini forecast chart + full AI narrative.
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer, ComposedChart, Area, Line, XAxis, YAxis, Tooltip,
} from 'recharts';
import { Sparkles, ChevronDown } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { useLanguage } from '@/lib/LanguageProvider';
import ForecastFeedbackModal from './ForecastFeedbackModal';
import { normalizeForecastPoints } from '../lib/forecastChartMath';
import DynamicConfidenceBadge from './DynamicConfidenceBadge';

// UI static strings for the decision board (zh / en). Data-driven fields
// (action_label_zh, trust_label_zh, one_liner_zh, analyst_brief.*) stay as
// the backend provides them — only `name_en`/`name_zh` are dual-language
// from the backend today, so product names switch, other LLM content stays
// as-is until the backend emits English variants.
      const UI = (lang) => lang === 'zh'
  ? {
      loading: 'AI 决策看板加载中…',
      title: '下周决策看板 · AI Decision Board',
      days: '未来 {d} 天',
      range: '{start} – {end}',
      material: '物料',
      price: '现价',
      forecast7d: '7日预测',
      expectedMove: '预期涨跌',
      riseProb: '上涨概率',
      aiSuggestion: 'AI建议',
      reliability: '可靠度',
      aiBrief: 'AI 简评',
      correctForecast: '修正预测',
      expand: '展开 {name}',
      actual: '实际',
      forecastN: '{d}日预测',
      chartLoading: '走势图数据加载中…',
      watchTriggers: '关注触发',
      aiAnalyst: 'AI 分析师撰写',
      dirAccuracy: '方向准确率',
      intelEvents: '情报事件',
      volLabel: '波动水平',
      confLabel: '置信度',
      nVol: '正常',
      mVol: '偏高',
      hVol: '剧烈',
      cHigh: '高',
      cMed: '中',
      cLow: '低',
      expandBtn: '展开',
      roiTrack: '决策命中率',
      roiPnl: '累计盈亏',
      noRoi: '暂无决策记录',
      drivers: '预测驱动因子',
      macroSupplyDemand: '宏观供需',
    }
  : {
      loading: 'Loading AI Decision Board…',
      title: 'Next-Week Decision Board',
      days: 'Next {d} days',
      range: '{start} – {end}',
      material: 'Material',
      price: 'Price',
      forecast7d: '7-Day Forecast',
      expectedMove: 'Expected Move',
      riseProb: 'Rise Probability',
      aiSuggestion: 'AI Suggestion',
      reliability: 'Reliability',
      aiBrief: 'AI Brief',
      correctForecast: 'Correct Forecast',
      expand: 'Expand {name}',
      actual: 'Actual',
      today: 'Today',
      baseForecast: 'Base forecast',
      forecastN: '{d}-day forecast',
      chartLoading: 'Loading chart data…',
      watchTriggers: 'Watch Triggers',
      aiAnalyst: 'Written by AI Analyst',
      dirAccuracy: 'Direction Accuracy',
      intelEvents: 'Intel Events',
      volLabel: 'Volatility',
      confLabel: 'Confidence',
      nVol: 'Normal',
      mVol: 'Elevated',
      hVol: 'Severe',
      cHigh: 'High',
      cMed: 'Medium',
      cLow: 'Low',
      expandBtn: 'Expand',
      roiTrack: 'Decision Hit Rate',
      roiPnl: 'Cumulative P&L',
      noRoi: 'No decision track record yet',
      drivers: 'Forecast Drivers',
      macroSupplyDemand: 'Macro Supply & Demand',
    };

const ACTION_STYLES = {
  buy: 'bg-green-100 text-green-700 border-green-200 dark:bg-green-900/40 dark:text-green-300 dark:border-green-800',
  sell: 'bg-red-100 text-red-700 border-red-200 dark:bg-red-900/40 dark:text-red-300 dark:border-red-800',
  hold: 'bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-900/40 dark:text-blue-300 dark:border-blue-800',
  watch: 'bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-900/40 dark:text-amber-300 dark:border-amber-800',
};


// `emphasis` sections get a highlighted background; `risk` sections get amber styling.
const BRIEF_SECTIONS = [
  { key: 'market_update_zh', zh: '市场动态', en: 'Market Update' },
  { key: 'price_data_zh', zh: '价格数据', en: 'Price Data' },
  { key: 'upstream_logic_zh', zh: '上游传导', en: 'Upstream Logic' },
  { key: 'supply_demand_zh', zh: '供需研判', en: 'Supply & Demand' },
  { key: 'forecast_zh', zh: '价格预测', en: 'Forecast', emphasis: true },
  { key: 'risk_zh', zh: '风险提示', en: 'Risk', risk: true },
];

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

/**
 * Custom tooltip for the ExpandedCard mini-chart.
 *
 * The old code hardcoded the decision-board horizon (`day=7`) into the Line's
 * `name`, so Recharts' default tooltip showed "7-day forecast" at EVERY hovered
 * X position — even 30 days out. This tooltip derives the label from the
 * hovered datum itself:
 *   - actual series            -> t.actual
 *   - base in 'forecast' zone  -> "{N}-day forecast" where N = days from the
 *                                 today-anchor (first zone==='today' point)
 *   - base in 'today'/past     -> t.today
 *
 * `points` is the already-normalized array (normalizeForecastPoints), so the
 * anchor logic stays identical to ForecastTrendChart.
 */
export function ChartTooltip({ active, payload, label, points, t }) {
  if (!active || !payload || !payload.length) return null;
  const anchor = points?.find((p) => p.zone === 'today');
  const DAY_MS = 86400000;
  return (
    <div className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 shadow-md dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200">
      <div className="mb-0.5 font-medium text-slate-400 dark:text-slate-400">{label}</div>
      {payload.map((entry) => {
        const datum = entry.payload;
        let label = t.actual;
        if (entry.dataKey === 'base') {
          if (datum?.zone === 'forecast' && anchor) {
            const n = Math.round((new Date(datum.date) - new Date(anchor.date)) / DAY_MS);
            label = n > 0 ? t.forecastN.replace('{d}', n) : t.today;
          } else {
            label = t.today;
          }
        }
        return (
          <div key={entry.dataKey || entry.name} className="flex items-center justify-between gap-4">
            <span className="mr-2">{label}</span>
            <span className="font-semibold tabular-nums">
              {entry.value == null ? '—' : Number(entry.value).toLocaleString()}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// Localize the forecast window [generatedAt, generatedAt + day] as a short
// date range (e.g. "Aug 14 – Aug 20, 2026" / "2026年8月14日 – 8月20日").
// Returns null when generated_at is missing or unparseable so the caller can
// gracefully degrade to showing just the "{d} days" label (current behavior).
function formatForecastRange(generatedAt, day, lang) {
  if (!generatedAt) return null;
  const start = new Date(generatedAt);
  if (!Number.isFinite(start.getTime())) return null;
  const end = new Date(start.getTime() + day * 86400000);
  if (lang === 'zh') {
    const startStr = start.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
    const sameYear = start.getFullYear() === end.getFullYear();
    const endStr = sameYear
      ? end.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric' })
      : end.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
    return { start: startStr, end: endStr };
  }
  const startStr = start.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  const endStr = end.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  return { start: startStr, end: endStr };
}

export default function DecisionBoard({ group, selectedId, onSelectProduct }) {
  const { lang } = useLanguage();
  const t = UI(lang);
  const [expandedId, setExpandedId] = useState(null);
  const [feedbackProduct, setFeedbackProduct] = useState(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ['market-dashboard', 'decision-board', group],
    queryFn: () => marketDashboardApi.decisionBoard(group, 7),
    staleTime: 60_000,
  });

  const range = formatForecastRange(data?.generated_at, data?.day, lang);

  if (isLoading) {
    return (
      <div className="mx-6 mt-4 shrink-0 rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-900">
        {t.loading}
      </div>
    );
  }
  // Board is additive — never break the page on error.
  if (isError || !data || !Array.isArray(data.products) || !data.products.length) {
    return null;
  }

  // shrink-0 is REQUIRED: the page root is a flex column with h-full +
  // overflow-y-auto. Without it, flex-shrink squeezes this card (it has
  // overflow-hidden, so min-height collapses) and clips the expanded row.
  return (
    <div className="mx-6 mt-4 shrink-0 overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
      <div className="border-b border-slate-100 px-4 py-3 dark:border-slate-800">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-violet-500" />
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
            {t.title}
          </h2>
          <span className="text-xs text-slate-400">{t.days.replace('{d}', data.day)}</span>
          {range && (
            <span className="text-xs text-slate-400">
              · {t.range.replace('{start}', range.start).replace('{end}', range.end)}
            </span>
          )}
        </div>
        <p className="mt-1 text-sm leading-relaxed text-slate-600 dark:text-slate-300">
          {data.briefing_zh}
        </p>
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-100 text-left text-xs text-slate-400 dark:border-slate-800">
            <th className="px-4 py-2 font-medium">{t.material}</th>
            <th className="px-2 py-2 font-medium text-right">{t.price}</th>
            <th className="px-2 py-2 font-medium text-right">{t.forecast7d}</th>
            <th className="px-2 py-2 font-medium text-right">{t.expectedMove}</th>
            <th className="px-2 py-2 font-medium text-right">{t.riseProb}</th>
            <th className="px-2 py-2 font-medium">{t.aiSuggestion}</th>
            <th className="px-2 py-2 font-medium">{t.reliability}</th>
            <th className="px-2 py-2 font-medium">{t.aiBrief}</th>
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
                lang={lang}
                t={t}
                group={group}
                isSelected={isSelected}
                isExpanded={isExpanded}
                onSelect={() => onSelectProduct?.(p.id)}
                onToggle={() => setExpandedId(isExpanded ? null : p.id)}
                onFeedback={() => setFeedbackProduct({ id: p.id, aiPrice: p.forecast_base })}
              />
            );
          })}
        </tbody>
      </table>
      {feedbackProduct && (
        <ForecastFeedbackModal
          productId={feedbackProduct.id}
          aiPrice={feedbackProduct.aiPrice}
          lang={lang}
          onClose={() => setFeedbackProduct(null)}
        />
      )}
    </div>
  );
}

function DecisionRow({ p, day, lang, t, group, isSelected, isExpanded, onSelect, onToggle, onFeedback }) {
  const name = lang === 'zh' ? p.name_zh : (p.name_en || p.name_zh);
  const chgCls =
    p.expected_change_pct == null
      ? 'text-slate-400'
      : p.expected_change_pct >= 0
        ? 'text-red-600 dark:text-red-400'   // 涨=red (CN convention)
        : 'text-green-600 dark:text-green-400';
  // P0.4: When p_rise is suppressed (honesty-gate downgrade) but data exists,
  // also suppress expected_change_pct to avoid misleading "+0.0%" display.
  const pRiseSuppressed = p.p_rise == null && p.forecast_base != null;
  const displayChg = pRiseSuppressed ? null : p.expected_change_pct;
  return (
    <>
    <tr
      onClick={onSelect}
      className={`cursor-pointer border-b border-slate-50 transition-colors hover:bg-slate-50 dark:border-slate-800/60 dark:hover:bg-slate-800/40 ${
        isSelected ? 'bg-slate-50 dark:bg-slate-800/50' : ''
      }`}
    >
      <td className="px-4 py-2.5">
        <div className="font-medium text-slate-800 dark:text-slate-100">{name}</div>
        <span className="rounded bg-slate-100 px-1 font-mono text-[10px] text-slate-500 dark:bg-slate-800 dark:text-slate-400">
          {p.code}
        </span>
      </td>
      <td className="px-2 py-2.5 text-right tabular-nums">{fmtPrice(p.current_price)}</td>
      <td className="px-2 py-2.5 text-right tabular-nums">{fmtPrice(p.forecast_base)}</td>
      <td className={`px-2 py-2.5 text-right tabular-nums ${chgCls}`}>
        {fmtPctFraction(displayChg)}
      </td>
      <td className="px-2 py-2.5 text-right tabular-nums">{fmtProb(p.p_rise)}</td>
      <td className="px-2 py-2.5">
        <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${ACTION_STYLES[p.action] || ACTION_STYLES.watch}`}>
          {p.action_label_zh}
        </span>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onFeedback?.(); }}
          className="ml-1 align-middle text-[10px] text-blue-600 hover:underline dark:text-blue-400"
        >
          {t.correctForecast}
        </button>
      </td>
        <td className="px-2 py-2.5">
          <DynamicConfidenceBadge productKey={p.product_id} lang={lang} />
        </td>
      <td className="max-w-[220px] truncate px-2 py-2.5 text-xs text-slate-500 dark:text-slate-400">
        {p.one_liner_zh}
      </td>
      <td className="px-2 py-2.5">
        <button
          type="button"
          aria-label={t.expand.replace('{name}', name)}
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
          <ExpandedCard p={p} day={day} lang={lang} t={t} group={group} />
        </td>
      </tr>
    )}
    </>
  );
}

function ExpandedCard({ p, day, lang, t, group }) {
  const { data } = useQuery({
    queryKey: ['market-dashboard', 'forecast-chart', p.id, 30],
    queryFn: () => marketDashboardApi.forecastChart(p.id, 30),
    staleTime: 60_000,
  });
  const { points } = normalizeForecastPoints(data?.points);
  const VOL = { NORMAL: t.nVol, MODERATE: t.mVol, HIGH: t.hVol };
  const CONF = { high: t.cHigh, medium: t.cMed, low: t.cLow };

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <div className="h-40 w-full min-w-0 rounded border border-slate-100 bg-white p-2 dark:border-slate-700 dark:bg-slate-900">
        {points.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
              <XAxis dataKey="date" tick={{ fontSize: 10 }} tickFormatter={(d) => String(d).slice(5)} />
              <YAxis tick={{ fontSize: 10 }} domain={['auto', 'auto']} width={50} />
              <Tooltip content={<ChartTooltip points={points} t={t} />} />
              <Area type="monotone" dataKey="actual" stroke="#2563eb" fill="#bfdbfe" strokeWidth={1.5} name={t.actual} />
              <Line type="monotone" dataKey="base" stroke="#f59e0b" strokeDasharray="4 2" dot={false} name={t.baseForecast} />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-slate-400">
            {t.chartLoading}
          </div>
        )}
      </div>

      <div className="min-w-0">
          {p.analyst_brief ? (
            <div className="space-y-1.5 text-sm leading-relaxed text-slate-700 dark:text-slate-200">
              {BRIEF_SECTIONS.map((sec) => {
                const val = p.analyst_brief[sec.key];
                if (!val) return null;
                const isMacro = group === 'upstream' && sec.key === 'supply_demand_zh';
                const title = isMacro
                  ? t.macroSupplyDemand
                  : (lang === 'zh' ? sec.zh : sec.en);
                if (sec.emphasis) {
                  return (
                    <div key={sec.key} className="rounded-md bg-blue-50 px-2.5 py-1.5 dark:bg-blue-900/20">
                      <span className="mr-1 text-xs font-semibold text-blue-700 dark:text-blue-300">{title}:</span>
                      <span>{val}</span>
                    </div>
                  );
                }
                if (sec.risk) {
                  return (
                    <p key={sec.key} className="text-xs text-amber-600 dark:text-amber-400">
                      <span className="font-medium">{title}:</span>{val}
                    </p>
                  );
                }
                return (
                  <p key={sec.key}>
                    <span className="mr-1 text-xs font-medium text-slate-500 dark:text-slate-400">{title}:</span>
                    {val}
                  </p>
                );
              })}
              {p.analyst_brief.watch_triggers_zh?.length > 0 && (
                <div>
                  <div className="text-xs font-medium text-slate-500 dark:text-slate-400">{t.watchTriggers}</div>
                  <ul className="mt-0.5 list-disc pl-4 text-xs text-slate-500 dark:text-slate-400">
                    {p.analyst_brief.watch_triggers_zh.map((trig, i) => (
                      <li key={i}>{trig}</li>
                    ))}
                  </ul>
                </div>
              )}
              {p.analyst_brief.source === 'llm' && (
                <span className="inline-block rounded bg-violet-50 px-1.5 py-0.5 text-[10px] text-violet-600 dark:bg-violet-900/30 dark:text-violet-300">{t.aiAnalyst}</span>
              )}
            </div>
          ) : (
            <p className="text-sm leading-relaxed text-slate-700 dark:text-slate-200">
              {p.narrative_zh}
            </p>
          )}
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400 sm:grid-cols-5">
          <div>{t.dirAccuracy}<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{p.directional_accuracy == null ? '—' : `${Math.round(p.directional_accuracy * 100)}%`}</span></div>
          <div>{t.intelEvents}<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{p.intel_event_count}{lang === 'zh' ? ' 条' : ''}</span>
            {p.intel_event_count === 0 && (
              <span className="block text-[10px] font-normal text-slate-400 dark:text-slate-500">{lang === 'zh' ? '近期无重大市场事件' : 'No major market events recently'}</span>
            )}
          </div>
          <div>{t.volLabel}<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{VOL[p.volatility_regime] || '—'}</span></div>
          <div>{t.confLabel}<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{CONF[p.confidence] || t.cLow}</span></div>
          <div>
            {t.roiTrack}
            <br />
            {p.decision_roi && p.decision_roi.total_realized > 0 ? (
              <span className={`font-semibold ${p.decision_roi.accuracy_pct >= 50 ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'}`}>
                {Math.round(p.decision_roi.accuracy_pct)}%
                <span className="block text-[10px] font-normal text-slate-400 dark:text-slate-500">
                  {p.decision_roi.total_realized}/{p.decision_roi.total_decisions}{lang === 'zh' ? ' 已兑现' : ' realized'}
                </span>
              </span>
            ) : (
              <span className="text-xs text-slate-400">{t.noRoi}</span>
            )}
          </div>
        </div>
        {p.skus?.length > 0 && (
          <table className="mt-3 w-full text-xs">
            <thead>
              <tr className="text-left text-slate-400">
                <th className="py-1 font-medium">{lang === 'zh' ? '物料代码' : 'Material Code'}</th>
                <th className="py-1 font-medium">{lang === 'zh' ? '名称' : 'Name'}</th>
                <th className="py-1 font-medium text-right">{lang === 'zh' ? '预测价' : 'Forecast Price'}</th>
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
        {p.drivers?.length > 0 && (
          <div className="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800">
            <p className="mb-1 text-xs font-medium text-slate-500 dark:text-slate-400">{t.drivers}</p>
            <div className="flex flex-wrap gap-1.5">
              {p.drivers.map((d, i) => (
                <span key={i} className="inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                  <span className="font-mono">{d.feature}</span>
                  <span className="text-slate-400">{Math.round(d.weight * 100)}%</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
