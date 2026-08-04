// ObservedDataStrip — flat 7-day stat row matching EDIA upstream design.
// Renders: "Observed Data" header + date range, then a horizontal row of
// flat stat blocks (product name, big price, Price X%, High/Low).
// Click a block to select that product (drives the charts below).
import { useState, useEffect } from 'react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';

const PRODUCT_LABELS = {
  crude_oil: { en: 'Brent Crude Oil', zh: '布伦特原油' },
  naphtha: { en: 'Japanese Naphtha', zh: '日本石脑油' },
  cracked_c5: { en: 'Cracked C5', zh: '裂解碳五' },
};

function fmtPrice(v) {
  if (v == null) return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

export default function ObservedDataStrip({
  products,
  selectedId,
  onSelect,
  language = 'en',
  labels,
}) {
  const [stats, setStats] = useState({});
  const [range, setRange] = useState({ start: null, end: null });

  useEffect(() => {
    let cancelled = false;
    const pids = products || Object.keys(PRODUCT_LABELS);

    const load = async () => {
      const results = await Promise.all(
        pids.map(async (pid) => {
          try {
            const rows = await marketDashboardApi.history(pid, 7);
            return [pid, rows];
          } catch {
            return [pid, null];
          }
        }),
      );
      if (cancelled) return;

      const map = {};
      let start = null;
      let end = null;
      for (const [pid, rows] of results) {
        if (!rows || !rows.length) {
          map[pid] = null;
          continue;
        }
        const prices = rows.map((r) => Number(r.price)).filter((v) => !Number.isNaN(v));
        if (!prices.length) {
          map[pid] = null;
          continue;
        }
        const cur = prices[prices.length - 1];
        const first = prices[0];
        const pct = first ? ((cur - first) / first) * 100 : null;
        map[pid] = {
          current: cur,
          changePct: pct,
          high: Math.max(...prices),
          low: Math.min(...prices),
        };
        const firstDate = rows[0]?.date;
        const lastDate = rows[rows.length - 1]?.date;
        if (firstDate && (!start || firstDate < start)) start = firstDate;
        if (lastDate && (!end || lastDate > end)) end = lastDate;
      }
      setStats(map);
      setRange({ start, end });
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [products]);

  const pids = products || Object.keys(PRODUCT_LABELS);

  return (
    <div className="border-b border-slate-200 bg-white px-6 pt-5 pb-5
      dark:border-slate-700 dark:bg-slate-900">
      {/* Header */}
      <div className="flex items-baseline justify-between">
        <h3 className={TYPO.cardTitle + ' text-slate-800 dark:text-slate-100'}>
          Observed Data
        </h3>
        {range.start && range.end && (
          <span className={TYPO.micro + ' text-slate-500 dark:text-slate-400'}>
            7-day change ({range.end} - {range.start})
          </span>
        )}
      </div>

      {/* Stat row */}
      <div className="mt-4 grid gap-6" style={{ gridTemplateColumns: `repeat(${pids.length}, minmax(0, 1fr))` }}>
        {pids.map((pid) => {
          const label = labels?.[pid] || PRODUCT_LABELS[pid] || { en: pid, zh: pid };
          const s = stats[pid];
          const isSelected = selectedId === pid;
          const up = s?.changePct != null && s.changePct > 0;
          const down = s?.changePct != null && s.changePct < 0;
          return (
            <button
              key={pid}
              onClick={() => onSelect && onSelect(pid)}
              className={`group rounded-md px-3 py-2 text-left transition-colors
                ${isSelected
                  ? 'bg-slate-100 ring-1 ring-slate-300 dark:bg-slate-800 dark:ring-slate-600'
                  : 'hover:bg-slate-50 dark:hover:bg-slate-800/50'
                }`}
            >
              <div className={TYPO.micro + ' text-slate-500 dark:text-slate-400'}>
                {language === 'zh' ? label.zh : label.en}
              </div>
              <div className="mt-1.5 flex items-baseline gap-1.5">
                <span className="text-[22px] font-bold leading-none tabular-nums text-slate-800 dark:text-slate-100">
                  {fmtPrice(s?.current)}
                </span>
              </div>
              {s && (
                <div className={`mt-1 ${TYPO.micro}`}>
                  <span className={
                    up ? 'text-emerald-600 dark:text-emerald-400'
                    : down ? 'text-red-500 dark:text-red-400'
                    : 'text-slate-500 dark:text-slate-400'
                  }>
                    Price {s.changePct == null ? '—'
                      : `${s.changePct >= 0 ? '+' : ''}${s.changePct.toFixed(2)}%`}
                  </span>
                </div>
              )}
              {s && (
                <div className={TYPO.micro + ' mt-0.5 text-slate-400 dark:text-slate-500'}>
                  High {fmtPrice(s.high)} Low {fmtPrice(s.low)}
                </div>
              )}
              {!s && (
                <div className={TYPO.micro + ' mt-2 text-slate-400'}>—</div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}