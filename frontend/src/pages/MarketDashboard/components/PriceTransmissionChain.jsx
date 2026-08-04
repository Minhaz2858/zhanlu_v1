// PriceTransmissionChain — 4-tier horizontal flow matching EDIA upstream design.
// Header: title + subtitle "Crude oil → Naphtha → C5/C9 → Downstream products
// (30-day change)" + "Computed from MySQL data" right. Tiers with colored
// rounded badges (Crude Oil/Naphtha/Cracking Products/Downstream). Node cards
// show EN+ZH names, big 2-decimal price, red ▲ change% + "CNY/ton" unit,
// 30-day trend date range, pink sparkline, italic percentile remark.
// Chinese convention: red = up, green = down.
import { useEffect, useState } from 'react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';

/* ── Inline pink sparkline (90×24) ───────────────────────────── */
function Sparkline({ data }) {
  if (!data || data.length < 2) return null;
  const w = 90, h = 24, pad = 2;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg width={w} height={h} className="inline-block align-middle">
      <polyline
        points={pts.join(' ')}
        fill="none"
        stroke="#f43f5e"
        strokeWidth={1.2}
        strokeLinejoin="round"
      />
    </svg>
  );
}

/* ── Tier badge (colored rounded pill) ──────────────────────── */
const TIER_META = [
  { label: 'Crude Oil', bg: '#1e293b' },          // dark slate
  { label: 'Naphtha', bg: '#7c3aed' },            // purple
  { label: 'Cracking Products', bg: '#2563eb' },  // blue
  { label: 'Downstream', bg: '#16a34a' },         // green
];

/* ── Single node card ───────────────────────────────────────── */
function NodeCard({ node, language = 'en' }) {
  const nameEn = node.name_en;
  const nameZh = node.name_zh;
  const up = node.change_pct_30d != null && node.change_pct_30d > 0;
  const down = node.change_pct_30d != null && node.change_pct_30d < 0;
  // Chinese convention: red = up, green = down
  const changeColor = up
    ? 'text-red-500 dark:text-red-400'
    : down
    ? 'text-emerald-600 dark:text-emerald-400'
    : 'text-slate-500 dark:text-slate-400';
  return (
    <div className="w-[200px] rounded-lg border border-slate-200 bg-white p-3 shadow-sm
      dark:border-slate-700 dark:bg-slate-900">
      {/* Names */}
      <div className="font-semibold text-[13px] leading-tight text-slate-800 dark:text-slate-100">
        {language === 'zh' && nameZh ? nameZh : nameEn}
      </div>
      {nameZh && language !== 'zh' && (
        <div className={TYPO.micro + ' mt-0.5 text-slate-400 dark:text-slate-500'}>
          {nameZh}
        </div>
      )}

      {/* Price + unit */}
      <div className="mt-2 flex items-baseline gap-1.5">
        {node.current_price != null ? (
          <span className="text-[20px] font-bold leading-none tabular-nums text-slate-800 dark:text-slate-100">
            {Number(node.current_price).toLocaleString('en-US', { maximumFractionDigits: 2 })}
          </span>
        ) : (
          <span className="text-[20px] font-bold text-slate-400">—</span>
        )}
        <span className={TYPO.micro + ' text-slate-400 dark:text-slate-500'}>
          {node.unit || 'CNY/ton'}
        </span>
      </div>

      {/* Change % */}
      {node.change_pct_30d != null && (
        <div className={`mt-1 text-[12px] font-semibold tabular-nums ${changeColor}`}>
          {up ? '▲' : down ? '▼' : ''}
          {node.change_pct_30d > 0 ? '+' : ''}
          {node.change_pct_30d.toFixed(2)}%
          <span className={TYPO.micro + ' ml-1 font-normal text-slate-400 dark:text-slate-500'}>
            ton
          </span>
        </div>
      )}

      {/* 30-day Trend range */}
      {node.trend_start_date && node.trend_end_date && (
        <div className={TYPO.micro + ' mt-1.5 text-slate-400 dark:text-slate-500'}>
          30-day Trend: {node.trend_start_date} → {node.trend_end_date}
        </div>
      )}

      {/* Sparkline */}
      {node.sparkline?.length >= 2 && (
        <div className="mt-1.5">
          <Sparkline data={node.sparkline} />
        </div>
      )}

      {/* Percentile remark */}
      {node.percentile_remark && (
        <div className="mt-1.5 text-[11px] italic leading-tight text-slate-500 dark:text-slate-400">
          {node.percentile_remark}
        </div>
      )}
    </div>
  );
}

/* ── Dotted arrow between tiers ─────────────────────────────── */
function TierArrow() {
  return (
    <div className="flex items-center px-1.5 flex-shrink-0 self-center">
      <svg width="28" height="14" className="text-slate-300 dark:text-slate-600">
        <defs>
          <marker
            id="tc-arrow"
            markerWidth="6"
            markerHeight="6"
            refX="5"
            refY="3"
            orient="auto"
          >
            <polygon points="0 0, 6 3, 0 6" fill="currentColor" />
          </marker>
        </defs>
        <line
          x1="0" y1="7" x2="22" y2="7"
          stroke="currentColor"
          strokeWidth={1.2}
          strokeDasharray="3 3"
          markerEnd="url(#tc-arrow)"
        />
      </svg>
    </div>
  );
}

/* ── Tier column (badge + stack of cards) ───────────────────── */
function Tier({ meta, nodes, language }) {
  return (
    <div className="flex flex-col items-center">
      <div
        className="mb-3 inline-block rounded-full px-3 py-1 text-[11px] font-semibold text-white shadow-sm"
        style={{ backgroundColor: meta.bg }}
      >
        {meta.label}
      </div>
      <div className="flex flex-col gap-2.5">
        {nodes.map((n) => (
          <NodeCard key={n.product_id} node={n} language={language} />
        ))}
      </div>
    </div>
  );
}

/* ── Main 4-tier flow ───────────────────────────────────────── */
export default function PriceTransmissionChain({ language = 'en' }) {
  const [tiers, setTiers] = useState([]);

  useEffect(() => {
    let cancelled = false;
    const fetch = async () => {
      try {
        const data = await marketDashboardApi.priceChain();
        if (!cancelled) setTiers(data.tiers || []);
      } catch {
        /* silent fail — empty state */
      }
    };
    fetch();
    const id = setInterval(fetch, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="border-b border-slate-200 bg-white px-6 pt-5 pb-6
      dark:border-slate-700 dark:bg-slate-900">
      {/* Header */}
      <div className="flex items-end justify-between gap-4">
        <div>
          <h3 className={TYPO.cardTitle + ' text-slate-800 dark:text-slate-100'}>
            Price Transmission Chain
          </h3>
          <p className={TYPO.micro + ' mt-1 text-slate-500 dark:text-slate-400'}>
            Crude oil → Naphtha → C5/C9 → Downstream products (30-day change)
          </p>
        </div>
        <span className={TYPO.micro + ' italic text-slate-400 dark:text-slate-500'}>
          Computed from MySQL data
        </span>
      </div>

      {/* Tier flow */}
      {!tiers.length ? (
        <div className="py-10 text-center">
          <p className={TYPO.listText + ' text-slate-500'}>Loading price chain…</p>
        </div>
      ) : (
        <div className="mt-5 overflow-x-auto">
          <div className="flex items-start gap-0 min-w-max pb-2">
            {tiers.map((tier, ti) => (
              <div key={ti} className="flex items-start">
                {ti > 0 && <TierArrow />}
                <Tier
                  meta={TIER_META[ti] || { label: `Tier ${ti + 1}`, bg: '#64748b' }}
                  nodes={tier}
                  language={language}
                />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}