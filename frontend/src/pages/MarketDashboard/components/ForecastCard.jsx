import { useQuery } from '@tanstack/react-query';
import { TrendingUp, Globe2, Calendar } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';
import { TYPO } from '../lib/typography';
import { PRODUCT_LABELS } from '@/constants/marketDashboard';

const DEFAULT_PRODUCT_IDS = ['cracked_c5', 'cracked_c9'];

/**
 * ForecastCard — 7-day-ahead AI price outlook.
 *
 * Reads EDIA's v_forecast_price view via /forecast-base-batch and converts
 * the CNY-denominated forecast to USD using /fx/usd-cny. Designed for
 * products that have a forecast in MATERIAL_FORECAST_IDS (cracked_c5,
 * cracked_c9); other products render with a muted "no forecast" note.
 *
 * v1 caveat: the warehouse's v_forecast_price is an audit backfill, not a
 * live ML prediction, so the price reflects the most recent record. The
 * `day` parameter is surfaced as informational; v2 can wire in real ML.
 */
export default function ForecastCard({
  productIds = DEFAULT_PRODUCT_IDS,
  day = 7,
  title = '7-Day Price Outlook',
}) {
  const forecastQuery = useQuery({
    queryKey: ['market-dashboard', 'forecast-base-batch', productIds, day],
    queryFn: () => marketDashboardApi.forecastBaseBatch(productIds, day),
    enabled: productIds.length > 0,
    staleTime: 5 * 60_000,
  });

  const fxQuery = useQuery({
    queryKey: ['market-dashboard', 'fx', 'usd-cny'],
    queryFn: () => marketDashboardApi.fxRate(),
    staleTime: 60 * 60_000,
  });

  const baseByProduct = forecastQuery.data?.base_by_product || {};
  const fxPrice = fxQuery.data?.price;
  const fxUnit = fxQuery.data?.unit;
  const fxAsOf = fxQuery.data?.as_of_date;

  return (
    <div
      className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900"
      data-testid="forecast-card"
    >
      <div className="mb-3 flex items-baseline justify-between">
        <div className="flex items-center gap-2">
          <TrendingUp className="h-4 w-4 text-slate-500" />
          <span className={TYPO.cardTitle}>{title}</span>
        </div>
        <span className={TYPO.secondaryData + ' flex items-center gap-1'}>
          <Calendar className="h-3 w-3" />
          Day {day}
        </span>
      </div>

      {forecastQuery.isLoading && (
        <p className={TYPO.listText + ' text-slate-500'}>Loading forecast…</p>
      )}
      {forecastQuery.isError && (
        <p className={TYPO.listText + ' text-red-600'}>
          Failed: {forecastQuery.error?.message || 'unknown'}
        </p>
      )}

      {forecastQuery.data && !forecastQuery.isError && (
        <>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {productIds.map((pid) => {
              const labels = PRODUCT_LABELS[pid] || {};
              const displayName = labels.label_en || pid;
              const group = labels.group;
              const cny = baseByProduct[pid];
              const usd = cny && fxPrice ? cny / fxPrice : null;
              const hasForecast = cny !== null && cny !== undefined;
              return (
                <div
                  key={pid}
                  className="rounded border border-slate-100 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-800/50"
                >
                  <div className="flex items-baseline justify-between">
                    <span className={TYPO.listTextMedium}>{displayName}</span>
                    {group && (
                      <span className={TYPO.micro + ' uppercase tracking-wide'}>
                        {group}
                      </span>
                    )}
                  </div>
                  {hasForecast ? (
                    <>
                      <div className={TYPO.priceData + ' mt-1'}>
                        ¥{cny.toLocaleString('en-US', { maximumFractionDigits: 0 })}
                        <span className={TYPO.secondaryData + ' ml-1 font-sans'}>
                          CNY/mt
                        </span>
                      </div>
                      {usd !== null && (
                        <div className={TYPO.secondaryMono + ' mt-0.5'}>
                          ≈ ${usd.toLocaleString('en-US', { maximumFractionDigits: 0 })}/mt
                        </div>
                      )}
                    </>
                  ) : (
                    <p className={TYPO.secondaryData + ' mt-1 italic'}>
                      No forecast for this product
                    </p>
                  )}
                </div>
              );
            })}
          </div>

          {fxQuery.data && (
            <div className="mt-3 flex items-center gap-1 border-t border-slate-100 pt-2 dark:border-slate-700">
              <Globe2 className="h-3 w-3 text-slate-400" />
              <span className={TYPO.micro}>
                FX: {fxPrice} {fxUnit} {fxAsOf ? `· as of ${fxAsOf}` : ''}
                {fxQuery.data.data_source === 'static_fallback' && ' (static)'}
              </span>
            </div>
          )}
        </>
      )}
    </div>
  );
}
