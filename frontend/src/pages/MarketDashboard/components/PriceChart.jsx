import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  ResponsiveContainer, ComposedChart, Area, Line,
  XAxis, YAxis, Tooltip,
} from 'recharts';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import { getChartTheme, rechartsTooltipStyle } from '../lib/chartTheme';

/**
 * useDarkMode — self-contained dark mode detection.
 * Reads document.documentElement.classList for 'dark' + observes changes.
 * Avoids depending on a specific ThemeProvider package.
 */
function useDarkMode() {
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const update = () => setIsDark(document.documentElement.classList.contains('dark'));
    update();
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  }, []);
  return isDark;
}

/**
 * PriceChart — per-product recharts ComposedChart (Area fill + Line stroke).
 * Mirrors EDIA's chart styling. Empty / loading states are surfaced inline.
 */
export default function PriceChart({ productId, days = 30 }) {
  const isDark = useDarkMode();
  const chartTheme = getChartTheme(isDark);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'history', productId, days],
    queryFn: () => marketDashboardApi.history(productId, days),
    staleTime: 60_000,
  });

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2 flex items-baseline justify-between">
        <span className={TYPO.cardTitle}>{productId}</span>
        <span className={TYPO.secondaryData}>{days}d</span>
      </div>
      {isLoading && (
        <p className={TYPO.listText + ' text-slate-500'}>Loading…</p>
      )}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {error?.message || 'unknown'}
        </p>
      )}
      {!isLoading && !isError && (!data || data.length === 0) && (
        <p className={TYPO.listText + ' text-slate-500'}>No history.</p>
      )}
      {!isLoading && !isError && data && data.length > 0 && (
        <div style={{ width: '100%', height: 200 }}>
          <ResponsiveContainer>
            <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id={`grad-${productId}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={chartTheme.axis} stopOpacity={0.4} />
                  <stop offset="100%" stopColor={chartTheme.axis} stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <XAxis
                dataKey="date"
                stroke={chartTheme.tick}
                tick={{ fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: chartTheme.axis }}
              />
              <YAxis
                stroke={chartTheme.tick}
                tick={{ fontSize: 11 }}
                tickLine={false}
                axisLine={{ stroke: chartTheme.axis }}
                width={48}
              />
              <Tooltip contentStyle={rechartsTooltipStyle(chartTheme, isDark)} />
              <Area
                type="monotone"
                dataKey="price"
                stroke={chartTheme.axis}
                fill={`url(#grad-${productId})`}
                strokeWidth={1}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="price"
                stroke={chartTheme.tick}
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
