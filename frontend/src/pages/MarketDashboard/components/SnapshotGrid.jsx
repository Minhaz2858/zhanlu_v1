import { TYPO } from '../lib/typography';
import {
  getPriceChangeTextClass,
  formatSignedPriceChange,
  NO_DATA_LABEL,
} from '../lib/priceChange';

/**
 * SnapshotGrid — one card per product, mirroring EDIA's OverviewDashboard
 * snapshot row. Shows name, latest price, daily change pct, date.
 */
export default function SnapshotGrid({ items = [] }) {
  if (!items.length) {
    return <p className={TYPO.listText + ' text-slate-500'}>No snapshot data.</p>;
  }
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      {items.map((s) => {
        const changeClass = getPriceChangeTextClass(s.change_pct);
        const changeText =
          s.change_pct == null
            ? NO_DATA_LABEL
            : `${formatSignedPriceChange(s.change_pct)}%`;
        return (
          <div
            key={s.id}
            className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900"
          >
            <div className="flex items-baseline justify-between">
              <span className={TYPO.cardTitle}>{s.name}</span>
              <span className={TYPO.secondaryData}>{s.date || NO_DATA_LABEL}</span>
            </div>
            <div className="mt-1 flex items-baseline gap-2">
              <span className={TYPO.priceData}>
                {s.price == null ? NO_DATA_LABEL : s.price}
              </span>
              {s.unit && <span className={TYPO.secondaryMono}>{s.unit}</span>}
            </div>
            <div className={`mt-1 text-sm ${changeClass}`}>{changeText}</div>
            {s.note && (
              <div className={TYPO.micro + ' mt-2 italic'}>{s.note}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
