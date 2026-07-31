import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';

/**
 * NewsPanel — industry news for the selected section.
 *
 * v1: EDIA's news comes from an AI event pipeline that is out of scope for
 * the read-only MySQL mirror. The endpoint returns {items: [], note: "..."}
 * and this panel surfaces that as an info banner instead of empty cards.
 * When news items arrive in v2+, the same component will render them as a list.
 */
export default function NewsPanel({ group, limit = 18 }) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'news', group, limit],
    queryFn: () => marketDashboardApi.news(group, limit),
    staleTime: 5 * 60_000,
  });

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2">
        <span className={TYPO.cardTitle}>Industry News — {group || 'all'}</span>
      </div>
      {isLoading && <p className={TYPO.listText + ' text-slate-500'}>Loading…</p>}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {error?.message || 'unknown'}
        </p>
      )}
      {data && data.items && data.items.length > 0 && (
        <ul className="space-y-2">
          {data.items.map((it) => (
            <li key={it.event_id} className="text-sm">
              <a href={it.source_url || '#'} className="font-medium hover:underline">
                {it.headline}
              </a>
              {it.summary && (
                <p className={TYPO.secondaryData + ' mt-0.5'}>{it.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      {data && (!data.items || data.items.length === 0) && (
        <div className="rounded border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800">
          {data.note || 'No news available.'}
        </div>
      )}
    </div>
  );
}
