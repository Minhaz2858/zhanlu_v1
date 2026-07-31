import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import { NO_DATA_LABEL } from '../lib/priceChange';

/**
 * RegionalTable — per-product regional price breakdown.
 * v1: no spread / cost-floor analysis (rendered as a note under the table).
 */
export default function RegionalTable({ productId }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'regional', productId],
    queryFn: () => marketDashboardApi.regional(productId),
    staleTime: 60_000,
  });

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2 flex items-baseline justify-between">
        <span className={TYPO.cardTitle}>{productId} — Regional</span>
      </div>
      {isLoading && <p className={TYPO.listText + ' text-slate-500'}>Loading…</p>}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {error?.message || 'unknown'}
        </p>
      )}
      {data && data.regions && data.regions.length === 0 && (
        <p className={TYPO.listText + ' text-slate-500'}>No regional columns.</p>
      )}
      {data && data.regions && data.regions.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="py-1 pr-4">Region</th>
              <th className="py-1 pr-4">Price</th>
              <th className="py-1 pr-4">Date</th>
            </tr>
          </thead>
          <tbody>
            {data.regions.map((r) => (
              <tr key={r.price_type} className="border-t border-slate-100 dark:border-slate-800">
                <td className="py-1 pr-4">{r.label}</td>
                <td className="py-1 pr-4 font-mono">{r.price ?? NO_DATA_LABEL}</td>
                <td className="py-1 pr-4 text-xs text-slate-500">{r.date || NO_DATA_LABEL}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data?.note && (
        <p className={TYPO.micro + ' mt-2 italic'}>{data.note}</p>
      )}
    </div>
  );
}
