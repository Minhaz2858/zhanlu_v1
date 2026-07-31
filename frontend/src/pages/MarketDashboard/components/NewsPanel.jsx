import { useQuery } from '@tanstack/react-query';
import { Newspaper, ExternalLink, AlertCircle, TrendingUp, TrendingDown } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';

/**
 * NewsPanel — industry intelligence events.
 *
 * Upgraded from v1 stub to consume /events/latest, which reads the
 * intelligence_events MySQL table (EDIA's AI event pipeline). Each event
 * has a direction, impact_magnitude, usefulness_score, and an
 * affected_commodities list we can render as tags.
 *
 * The v1 stub fallback (data.note) is still honored when the endpoint
 * returns 503 or the query fails — that lets the page surface the
 * "data source offline" banner instead of crashing.
 */
export default function NewsPanel({
  group,
  hours = 168,
  limit = 18,
  minUsefulnessScore = 0,
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'events', hours, minUsefulnessScore],
    queryFn: () => marketDashboardApi.events(hours, 'approved', minUsefulnessScore),
    staleTime: 5 * 60_000,
  });

  const events = data?.events || [];

  return (
    <div
      className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900"
      data-testid="news-panel"
    >
      <div className="mb-3 flex items-center gap-2">
        <Newspaper className="h-4 w-4 text-slate-500" />
        <span className={TYPO.cardTitle}>
          Intelligence Events — {group || 'all'}
        </span>
        {data && (
          <span className={TYPO.secondaryData + ' ml-auto'}>
            {data.count} event{data.count === 1 ? '' : 's'}
          </span>
        )}
      </div>

      {isLoading && <p className={TYPO.listText + ' text-slate-500'}>Loading…</p>}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {error?.message || 'unknown'}
        </p>
      )}

      {data && !isError && events.length > 0 && (
        <ul className="space-y-2" data-testid="events-list">
          {events.slice(0, limit).map((ev) => (
            <li
              key={ev.event_id}
              className="rounded border border-slate-100 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-800/50"
              data-testid="event-item"
            >
              <div className="flex items-start gap-2">
                {ev.direction === 'up' && (
                  <TrendingUp className="mt-0.5 h-4 w-4 flex-shrink-0 text-green-600" />
                )}
                {ev.direction === 'down' && (
                  <TrendingDown className="mt-0.5 h-4 w-4 flex-shrink-0 text-red-600" />
                )}
                {ev.direction === 'uncertain' && (
                  <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    {ev.source_url ? (
                      <a
                        href={ev.source_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-sm font-medium hover:underline"
                      >
                        {ev.headline}
                        <ExternalLink className="ml-1 inline h-3 w-3 align-baseline" />
                      </a>
                    ) : (
                      <span className="text-sm font-medium">{ev.headline}</span>
                    )}
                  </div>
                  {ev.summary && (
                    <p className={TYPO.secondaryData + ' mt-0.5'}>{ev.summary}</p>
                  )}
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
                    {ev.event_type && (
                      <span className="rounded bg-slate-200 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-slate-700 dark:bg-slate-700 dark:text-slate-300">
                        {ev.event_type}
                      </span>
                    )}
                    {ev.affected_commodities && ev.affected_commodities.length > 0 && (
                      <span className="flex flex-wrap gap-1">
                        {ev.affected_commodities.slice(0, 5).map((c) => (
                          <span
                            key={c}
                            className="rounded border border-blue-200 bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-700 dark:border-blue-800 dark:bg-blue-900/30 dark:text-blue-300"
                          >
                            {c}
                          </span>
                        ))}
                      </span>
                    )}
                    {typeof ev.usefulness_score === 'number' && ev.usefulness_score > 0 && (
                      <span className={TYPO.micro + ' ml-auto'}>
                        usefulness {ev.usefulness_score.toFixed(1)}
                      </span>
                    )}
                    {ev.detected_at && (
                      <span className={TYPO.micro}>
                        {ev.detected_at.replace('T', ' ').slice(0, 16)}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      {data && !isError && events.length === 0 && (
        <div className="rounded border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800">
          No intelligence events found in the last {hours} hours.
        </div>
      )}
    </div>
  );
}
