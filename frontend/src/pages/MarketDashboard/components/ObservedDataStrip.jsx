// ObservedDataStrip — flat 7-day stat row matching EDIA upstream design.
// Renders: "Observed Data" header + date range, then a horizontal row of
// flat stat blocks (product name, big price, 7d change %, Start → End price).
// High/Low (peak/trough) are kept in a hover tooltip to avoid ambiguity with
// the start-to-end change % (a peak on day 1 + trough on day 7 = a decline,
// which contradicts showing a bare "High/Low" range next to a positive %).
// Click a block to select that product (drives the charts below).
// Click the "View" (Eye) button in the corner to open a 7-day history dialog.
import { useState, useEffect } from 'react';
import { Eye } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';

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

function fmtVolume(v) {
  if (v == null) return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return '—';
  return `${n.toLocaleString('en-US', { maximumFractionDigits: 2 })}t`;
}

// "2026-08-11" / "2026/08/11" → "8.11" for a compact display.
function fmtShortDate(d) {
  if (!d) return '—';
  const parts = String(d).replace(/\//g, '-').split('-');
  if (parts.length >= 3) {
    const m = parseInt(parts[1], 10);
    const day = parseInt(parts[2], 10);
    if (!Number.isNaN(m) && !Number.isNaN(day)) return `${m}.${day}`;
  }
  return String(d);
}

// Normalize "YYYY-MM-DD" / "YYYY/MM/DD" → "YYYY-MM-DD" key.
function normalizeDateKey(d) {
  if (!d) return '';
  return String(d).replace(/\//g, '-').slice(0, 10);
}

// Format a Date as local "YYYY-MM-DD".
function isoLocalDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

// Build exactly 7 entries (today → today-6, newest first), merging API rows by
// date and filling missing days with a placeholder row (null price/volume).
function buildLastSevenDays(rows) {
  const byDate = new Map();
  if (Array.isArray(rows)) {
    for (const r of rows) {
      const key = normalizeDateKey(r?.date);
      if (key) byDate.set(key, r);
    }
  }
  const today = new Date();
  const out = [];
  for (let i = 0; i < 7; i += 1) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const key = isoLocalDate(d);
    out.push(byDate.get(key) || { date: key, price: null, volume: null });
  }
  return out;
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
  const [viewPid, setViewPid] = useState(null);

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
          startPrice: first,
          endPrice: cur,
          changePct: pct,
          high: Math.max(...prices),
          low: Math.min(...prices),
          rows,
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
  const viewStat = viewPid ? stats[viewPid] : null;
  const viewLabel = viewPid
    ? labels?.[viewPid] || PRODUCT_LABELS[viewPid] || { en: viewPid, zh: viewPid }
    : null;
  // Always render the full last-7-days window (today → today-6, newest first),
  // filling missing days with placeholder rows.
  const viewRows = buildLastSevenDays(viewStat?.rows);

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
            7-day change ({fmtShortDate(range.end)} - {fmtShortDate(range.start)})
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
          const flat = s?.changePct != null && s.changePct === 0;
          const arrow = up ? '▲' : down ? '▼' : flat ? '→' : '';
          const tooltip = s
            ? `${language === 'zh' ? '最高' : 'High'} ${fmtPrice(s.high)} · ${language === 'zh' ? '最低' : 'Low'} ${fmtPrice(s.low)}`
            : undefined;
          return (
            <div
              key={pid}
              className={`group relative rounded-md transition-colors
                ${isSelected
                  ? 'bg-slate-100 ring-1 ring-slate-300 dark:bg-slate-800 dark:ring-slate-600'
                  : 'hover:bg-slate-50 dark:hover:bg-slate-800/50'
                }`}
            >
              <button
                onClick={() => onSelect && onSelect(pid)}
                title={tooltip}
                className="block w-full px-3 py-2 text-left"
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
                      {language === 'zh' ? '7日' : '7d'}{' '}
                      {s.changePct == null ? '—'
                        : `${s.changePct >= 0 ? '+' : ''}${s.changePct.toFixed(2)}%`}
                      {arrow && <span className="ml-1">{arrow}</span>}
                    </span>
                  </div>
                )}
                {s && (
                  <div className={TYPO.micro + ' mt-0.5 text-slate-400 dark:text-slate-500'}>
                    {`${fmtPrice(s.startPrice)} → ${fmtPrice(s.endPrice)}`}
                  </div>
                )}
                {!s && (
                  <div className={TYPO.micro + ' mt-2 text-slate-400'}>—</div>
                )}
              </button>

              {/* View (7-day history) button */}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  setViewPid(pid);
                }}
                title={language === 'zh' ? '查看近7日历史' : 'View 7-day history'}
                aria-label={language === 'zh' ? '查看近7日历史' : 'View 7-day history'}
                className="absolute right-1.5 top-1.5 rounded p-1 text-slate-400
                  opacity-0 transition-opacity hover:bg-slate-200 hover:text-slate-700
                  focus:opacity-100 group-hover:opacity-100
                  dark:text-slate-500 dark:hover:bg-slate-700 dark:hover:text-slate-200"
              >
                <Eye className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>

      {/* 7-day history dialog */}
      <Dialog open={!!viewPid} onOpenChange={(open) => !open && setViewPid(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>
              {viewLabel ? (language === 'zh' ? viewLabel.zh : viewLabel.en) : ''}{' '}
              {language === 'zh' ? '近7日概览' : 'Last 7 Day Overview'}
            </DialogTitle>
            <DialogDescription>
              {viewRows.length
                ? `${fmtShortDate(viewRows[0]?.date)} - ${fmtShortDate(viewRows[viewRows.length - 1]?.date)}`
                : language === 'zh' ? '暂无数据' : 'No data'}
            </DialogDescription>
          </DialogHeader>

          <div className="mt-1 overflow-hidden rounded-md border border-slate-200
            dark:border-slate-700">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-50 text-left text-xs text-slate-500
                  dark:bg-slate-800/50 dark:text-slate-400">
                  <th className="px-3 py-2 font-medium">
                    {language === 'zh' ? '日期' : 'Date'}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">
                    {language === 'zh' ? '市场价' : 'Market price'}
                  </th>
                  <th className="px-3 py-2 text-right font-medium">
                    {language === 'zh' ? '销量' : 'Sales volume'}
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-700/60">
                {viewRows.length ? (
                  viewRows.map((r, i) => (
                    <tr key={i}>
                      <td className="px-3 py-2 tabular-nums text-slate-600 dark:text-slate-300">
                        {fmtShortDate(r.date)}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums font-medium text-slate-800 dark:text-slate-100">
                        {r.price == null ? '—' : `¥${fmtPrice(r.price)}`}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-slate-600 dark:text-slate-300">
                        {fmtVolume(r.volume)}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={3} className="px-3 py-6 text-center text-slate-400">
                      {language === 'zh' ? '暂无数据' : 'No data'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
