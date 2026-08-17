import { useQuery } from '@tanstack/react-query';
import { Truck } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import { formatKpiValue } from '../lib/formatKpiValue';

function fmtDate(iso, locale) {
  if (!iso) return null;
  try {
    return new Date(iso + 'T00:00:00').toLocaleDateString(locale, {
      year: 'numeric', month: 'short', day: 'numeric',
    });
  } catch {
    return iso;
  }
}

/**
 * SupplyChainSnapshot — per-product ERP supply-chain panel.
 *
 * Renders directly below the RegionalTable on each product page. Surfaces the
 * already-computed grade-level KPIs (30d Sales / Inventory / Days Pending /
 * Pending Qty) grouped by material_code × 牌号, plus 4 aggregated KPI cards.
 * Filtered by the product selected in the Observed Data strip (same contract
 * as RegionalTable). All data + summary come from the backend — no client math.
 */
export default function SupplyChainSnapshot({ productId, language = 'zh' }) {
  const locale = language === 'zh' ? 'zh-CN' : 'en-US';

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['market-dashboard', 'supply-chain', productId],
    queryFn: () => marketDashboardApi.supplyChain(productId),
    staleTime: 60_000,
    enabled: !!productId,
  });

  const t = language === 'en'
    ? {
        title: 'Supply Chain Snapshot',
        windowLabel: '30-day window',
        live: 'live from ERP',
        sales30d: '30d Sales',
        inventory: 'Inventory',
        pending: 'Pending',
        avgDays: 'Avg Days Pending',
        unitTon: 't',
        unitDay: 'd',
        grade: 'Grade',
        materialCode: 'Material Code',
        daysPending: 'Days Pending',
        loading: 'Loading…',
        failed: 'Failed',
        unknown: 'unknown',
        noMapping: 'No ERP mapping for this product',
      }
    : {
        title: '供应链快照',
        windowLabel: '30 天窗口',
        live: '来自 ERP 实时数据',
        sales30d: '30 天销量',
        inventory: '库存',
        pending: '待出库',
        avgDays: '平均待出库天数',
        unitTon: '吨',
        unitDay: '天',
        grade: '牌号',
        materialCode: '物料编码',
        daysPending: '待出库天数',
        loading: '加载中…',
        failed: '加载失败',
        unknown: '未知',
        noMapping: '该产品暂无 ERP 映射数据',
      };

  const rows = data?.rows || [];
  const summary = data?.summary;
  const hasData = rows.length > 0;

  const start = fmtDate(data?.window_start, locale);
  const end = fmtDate(data?.window_end, locale);
  const windowText = start && end
    ? `${t.windowLabel} · ${start} – ${end} · ${t.live}`
    : t.windowLabel;

  const kpis = [
    { label: t.sales30d, value: summary?.sales_30d_tons, unit: t.unitTon },
    { label: t.inventory, value: summary?.inventory_tons, unit: t.unitTon },
    { label: t.pending, value: summary?.pending_tons, unit: t.unitTon },
    { label: t.avgDays, value: summary?.avg_days_pending, unit: t.unitDay },
  ];

  return (
    <div className="mt-4 rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
      {/* Card header */}
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Truck className="h-4 w-4 text-slate-400 dark:text-slate-500" aria-hidden="true" />
          <span className={TYPO.cardTitle}>{productId} — {t.title}</span>
        </div>
        <span className="text-[11px] text-slate-400 dark:text-slate-500">{windowText}</span>
      </div>

      {isLoading && <p className={TYPO.listText + ' text-slate-500'}>{t.loading}</p>}
      {isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          {t.failed}: {error?.message || t.unknown}
        </p>
      )}

      {/* KPI cards — always render (values fall back to '--') */}
      {!isLoading && !isError && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {kpis.map((k) => (
            <div
              key={k.label}
              className="rounded-md border border-slate-100 bg-slate-50/60 px-3 py-2.5 dark:border-slate-800 dark:bg-slate-800/40"
            >
              <div className="text-[10px] font-semibold uppercase tracking-[0.05em] text-slate-400 dark:text-slate-500">
                {k.label}
              </div>
              <div className="mt-1 flex items-baseline gap-1">
                <span className="text-[22px] font-bold tabular-nums leading-none text-foreground">
                  {formatKpiValue(k.value, locale)}
                </span>
                <span className="text-[11px] text-slate-400 dark:text-slate-500">{k.unit}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Detail table */}
      {!isLoading && !isError && !hasData && (
        <p className={TYPO.listText + ' mt-4 text-slate-500'}>{t.noMapping}</p>
      )}

      {!isLoading && !isError && hasData && (
        <table className="mt-4 w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-[0.03em] text-slate-500 dark:text-slate-400">
              <th className="py-1 pr-4 font-medium">{t.grade}</th>
              <th className="py-1 pr-4 text-right font-medium">{t.sales30d}</th>
              <th className="py-1 pr-4 text-right font-medium">{t.inventory}</th>
              <th className="py-1 pr-4 text-right font-medium">{t.daysPending}</th>
              <th className="py-1 pr-4 text-right font-medium">{t.pending}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {rows.map((r) => (
              <tr key={r.sku_code || r.grade}>
                <td className="py-1.5 pr-4">
                  <span className="block text-[13px] font-medium text-foreground" title={r.grade}>
                    {r.grade}
                  </span>
                  <span className="block text-[10px] text-slate-400 dark:text-slate-500" title={r.sku_code}>
                    {r.sku_code}
                  </span>
                </td>
                <td className="py-1.5 pr-4 text-right font-mono text-[12px] text-foreground">
                  {formatKpiValue(r.sales_30d_tons, locale)}
                </td>
                <td className="py-1.5 pr-4 text-right font-mono text-[12px] text-foreground">
                  {formatKpiValue(r.inventory_tons, locale)}
                </td>
                <td className="py-1.5 pr-4 text-right font-mono text-[12px] text-foreground">
                  {formatKpiValue(r.inventory_days, locale)}
                </td>
                <td className="py-1.5 pr-4 text-right font-mono text-[12px] text-foreground">
                  {formatKpiValue(r.pending_tons, locale)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
