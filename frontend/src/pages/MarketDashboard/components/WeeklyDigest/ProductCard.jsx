import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { BarChart3, TrendingUp, BrainCircuit, ChevronDown } from 'lucide-react';
import PriceTrendChart from './PriceTrendChart';
import ForecastSection from './ForecastSection';
import { TYPO } from '../../lib/typography';

function ChangeBadge({ changePct }) {
  if (changePct == null) return null;
  const isUp = changePct > 0;
  const isDown = changePct < 0;
  const color = isUp ? 'text-red-700 bg-red-50' : isDown ? 'text-green-700 bg-green-50' : 'text-slate-600 bg-slate-100';
  const arrow = isUp ? '↑' : isDown ? '↓' : '→';
  return (
    <span className={`text-xs font-medium px-1.5 py-0.5 rounded ${color}`}>
      {arrow} {Math.abs(changePct).toFixed(1)}%
    </span>
  );
}

function ExpandableSection({ title, icon, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border border-slate-100 rounded-md">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors cursor-pointer"
      >
        <span className="flex items-center gap-1.5">
          {icon}
          {title}
        </span>
        <ChevronDown className={`h-3.5 w-3.5 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && <div className="px-3 pb-3">{children}</div>}
    </div>
  );
}

// Compact markdown renderer for LLM output. Tailwind "prose" with manually
// customized atoms because the full @tailwindcss/typography plugin is not
// installed — we instead set colors/spacing/typography inline.
export function MarkdownText({ children }) {
  if (!children) return null;
  return (
    <div
      className="
        markdown-body
        text-[13px] leading-relaxed text-slate-700
        [&_p]:my-1.5 [&_p]:leading-relaxed
        [&_strong]:font-semibold [&_strong]:text-slate-900
        [&_em]:italic [&_em]:text-slate-600
        [&_ul]:my-1.5 [&_ul]:pl-5 [&_ul]:list-disc
        [&_ol]:my-1.5 [&_ol]:pl-5 [&_ol]:list-decimal
        [&_li]:my-0.5
        [&_h1]:text-base [&_h1]:font-bold [&_h1]:mt-2 [&_h1]:mb-1 [&_h1]:text-slate-900
        [&_h2]:text-sm [&_h2]:font-bold [&_h2]:mt-2 [&_h2]:mb-1 [&_h2]:text-slate-900
        [&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:mt-1.5 [&_h3]:mb-0.5 [&_h3]:text-slate-800
        [&_hr]:my-2 [&_hr]:border-slate-200
        [&_code]:bg-slate-100 [&_code]:text-slate-800 [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded [&_code]:text-xs
        [&_blockquote]:border-l-2 [&_blockquote]:border-slate-300 [&_blockquote]:pl-2 [&_blockquote]:text-slate-600
      "
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}

export default function ProductCard({ product }) {
  const {
    product_name_zh,
    product_name_en,
    current_price,
    change_pct_7d,
    price_12mo,
    price_30d,
    market_analysis,
    forecast_7d,
    forecast_30d,
    ai_decision,
  } = product;

  return (
    <div className="bg-white border border-slate-200 rounded-lg shadow-sm hover:shadow-md transition-shadow">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
        <div>
          <h3 className={TYPO.cardTitle}>{product_name_zh}</h3>
          <p className="text-xs text-slate-500">{product_name_en}</p>
        </div>
        <div className="text-right">
          {current_price != null ? (
            <span className={TYPO.priceData}>
              {current_price.toLocaleString()}
              <span className="text-xs font-normal text-slate-500 ml-1">元/吨</span>
            </span>
          ) : (
            <span className="text-sm text-slate-400">N/A</span>
          )}
          <div className="mt-0.5">
            <ChangeBadge changePct={change_pct_7d} />
            <span className="text-[10px] text-slate-400 ml-1">7d</span>
          </div>
          {forecast_7d?.bull != null && forecast_7d?.bear != null && (
            <div className="mt-1 text-[10px] text-slate-500">
              预测区间{' '}
              <span className="font-semibold text-slate-700">
                {Math.min(forecast_7d.bear, forecast_7d.bull).toLocaleString()} ~{' '}
                {Math.max(forecast_7d.bear, forecast_7d.bull).toLocaleString()}
              </span>{' '}
              元/吨
            </div>
          )}
        </div>
      </div>

      {/* Price charts side by side */}
      <div className="px-4 py-3 grid grid-cols-2 gap-4">
        <PriceTrendChart data={price_12mo} title="近12月价格走势" height={180} />
        <PriceTrendChart data={price_30d} title="本周价格走势" height={180} />
      </div>

      {/* Analysis sections */}
      <div className="px-4 pb-4 space-y-3">
        {/* Market Analysis */}
        {market_analysis && (
          <ExpandableSection title="市场分析 Market Analysis" icon={<BarChart3 className="h-3.5 w-3.5 text-blue-500" />} defaultOpen={true}>
            <MarkdownText>{market_analysis}</MarkdownText>
          </ExpandableSection>
        )}

        {/* Forecast */}
        {(forecast_7d || forecast_30d) && (
          <ExpandableSection title="预测 Forecast" icon={<TrendingUp className="h-3.5 w-3.5 text-indigo-500" />} defaultOpen={true}>
            <ForecastSection
              forecast7d={forecast_7d}
              forecast30d={forecast_30d}
              currentPrice={current_price}
            />
          </ExpandableSection>
        )}

        {/* AI Decision */}
        {ai_decision && (
          <ExpandableSection title="AI决策 AI Decision" icon={<BrainCircuit className="h-3.5 w-3.5 text-violet-500" />} defaultOpen={false}>
            <MarkdownText>{ai_decision}</MarkdownText>
          </ExpandableSection>
        )}
      </div>
    </div>
  );
}
