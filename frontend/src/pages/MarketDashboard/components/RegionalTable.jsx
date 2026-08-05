import { useQuery } from '@tanstack/react-query';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * RegionalTable — per-product regional price breakdown.
 * v1: no spread / cost-floor analysis (rendered as a note under the table).
 */
export default function RegionalTable({ productId }) {
  const { lang } = useLanguage();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'regional', productId],
    queryFn: () => marketDashboardApi.regional(productId),
    staleTime: 60_000,
  });

  const t = lang === 'en'
    ? { regional: 'Regional', loading: 'Loading…', failed: 'Failed', unknown: 'unknown',
        noColumns: 'No regional columns.', region: 'Region', price: 'Price', date: 'Date',
        noData: 'No Data Available' }
    : { regional: '区域', loading: '加载中…', failed: '失败', unknown: '未知',
        noColumns: '暂无区域列。', region: '地区', price: '价格', date: '日期',
        noData: '暂无数据' };

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="mb-2 flex items-baseline justify-between">
        <span className={TYPO.cardTitle}>{productId} — {t.regional}</span>
      </div>
      {isLoading && <p className={TYPO.listText + ' text-slate-500'}>{t.loading}</p>}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          {t.failed}: {error?.message || t.unknown}
        </p>
      )}
      {data && data.regions && data.regions.length === 0 && (
        <p className={TYPO.listText + ' text-slate-500'}>{t.noColumns}</p>
      )}
      {data && data.regions && data.regions.length > 0 && (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="py-1 pr-4">{t.region}</th>
              <th className="py-1 pr-4">{t.price}</th>
              <th className="py-1 pr-4">{t.date}</th>
            </tr>
          </thead>
          <tbody>
            {data.regions.map((r) => (
              <tr key={r.price_type} className="border-t border-slate-100 dark:border-slate-800">
                <td className="py-1 pr-4">{r.label}</td>
                <td className="py-1 pr-4 font-mono">{r.price ?? t.noData}</td>
                <td className="py-1 pr-4 text-xs text-slate-500">{r.date || t.noData}</td>
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
