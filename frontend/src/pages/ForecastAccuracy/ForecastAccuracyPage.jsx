import React, { useEffect, useState } from 'react';
import { getAccuracyDashboard } from '../../api/marketDashboard';

/**
 * ForecastAccuracyPage
 *
 * Dedicated accuracy dashboard showing:
 *   - Accuracy heatmap: product × horizon matrix colored by MAPE
 *   - MAPE trend sparklines: per-product over 90 days
 *   - Directional accuracy table: % correct direction per product
 *   - Top miss analysis: worst products/horizons
 */

const MAPE_COLORS = {
  good: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  medium: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  bad: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
};

function mapeClass(mape) {
  if (mape === null || mape === undefined) return 'bg-slate-100 text-slate-500';
  if (mape < 5) return MAPE_COLORS.good;
  if (mape < 10) return MAPE_COLORS.medium;
  return MAPE_COLORS.bad;
}

export default function ForecastAccuracyPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getAccuracyDashboard()
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err);
          setData(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen text-slate-400">
        Loading accuracy dashboard…
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex items-center justify-center h-screen text-red-500">
        Failed to load accuracy dashboard
      </div>
    );
  }

  const { accuracy_heatmap, mape_trends, directional_accuracy, top_misses } = data;

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900 p-6">
      <div className="max-w-7xl mx-auto space-y-8">
        <h1 className="text-2xl font-bold text-slate-800 dark:text-slate-100">
          Forecast Accuracy Dashboard
        </h1>

        {/* Accuracy Heatmap */}
        <section className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-6">
          <h2 className="text-lg font-semibold text-slate-700 dark:text-slate-200 mb-4">
            Accuracy Heatmap (MAPE %)
          </h2>
          {accuracy_heatmap.length === 0 ? (
            <p className="text-sm text-slate-400">No accuracy data available</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="text-left py-2 px-3 font-medium text-slate-500">Product</th>
                    {Object.keys(accuracy_heatmap[0]?.horizons || {}).map((h) => (
                      <th key={h} className="text-center py-2 px-3 font-medium text-slate-500">
                        {h}d
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {accuracy_heatmap.map((row) => (
                    <tr key={row.product_key} className="border-b border-slate-100 dark:border-slate-800">
                      <td className="py-2 px-3 font-medium text-slate-700 dark:text-slate-300">
                        {row.product_name}
                      </td>
                      {Object.entries(row.horizons).map(([h, mape]) => (
                        <td key={h} className="py-2 px-3 text-center">
                          <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${mapeClass(mape)}`}>
                            {mape !== null ? `${mape}%` : '—'}
                          </span>
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* MAPE Trends */}
        <section className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-6">
          <h2 className="text-lg font-semibold text-slate-700 dark:text-slate-200 mb-4">
            MAPE Trends (90 days)
          </h2>
          {mape_trends.length === 0 ? (
            <p className="text-sm text-slate-400">No trend data available</p>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {mape_trends.map((item) => (
                <div key={item.product_key} className="border border-slate-200 dark:border-slate-700 rounded-md p-3">
                  <div className="text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                    {item.product_name}
                  </div>
                  <div className="flex items-end gap-0.5 h-16">
                    {item.trend.map((pt, i) => {
                      const height = Math.min(100, (pt.mape / 20) * 100); // scale: 20% = full height
                      return (
                        <div
                          key={i}
                          className="flex-1 rounded-sm"
                          style={{
                            height: `${height}%`,
                            backgroundColor: pt.mape < 5 ? '#22c55e' : pt.mape < 10 ? '#f59e0b' : '#ef4444',
                          }}
                          title={`${pt.date?.slice(0, 10)}: ${pt.mape}%`}
                        />
                      );
                    })}
                  </div>
                  <div className="text-xs text-slate-400 mt-1">
                    Latest: {item.trend[item.trend.length - 1]?.mape}%
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Directional Accuracy */}
        <section className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-6">
          <h2 className="text-lg font-semibold text-slate-700 dark:text-slate-200 mb-4">
            Directional Accuracy
          </h2>
          {directional_accuracy.length === 0 ? (
            <p className="text-sm text-slate-400">No directional data available</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="text-left py-2 px-3 font-medium text-slate-500">Product</th>
                    <th className="text-center py-2 px-3 font-medium text-slate-500">Correct</th>
                    <th className="text-center py-2 px-3 font-medium text-slate-500">Total</th>
                    <th className="text-center py-2 px-3 font-medium text-slate-500">Accuracy</th>
                  </tr>
                </thead>
                <tbody>
                  {directional_accuracy.map((row) => (
                    <tr key={row.product_key} className="border-b border-slate-100 dark:border-slate-800">
                      <td className="py-2 px-3 font-medium text-slate-700 dark:text-slate-300">
                        {row.product_name}
                      </td>
                      <td className="py-2 px-3 text-center text-slate-600">{row.correct}</td>
                      <td className="py-2 px-3 text-center text-slate-600">{row.total}</td>
                      <td className="py-2 px-3 text-center">
                        <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${mapeClass(100 - row.accuracy_pct)}`}>
                          {row.accuracy_pct}%
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* Top Misses */}
        <section className="bg-white dark:bg-slate-800 rounded-lg shadow-sm p-6">
          <h2 className="text-lg font-semibold text-slate-700 dark:text-slate-200 mb-4">
            Top Misses (Worst MAPE)
          </h2>
          {top_misses.length === 0 ? (
            <p className="text-sm text-slate-400">No miss data available</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 dark:border-slate-700">
                    <th className="text-left py-2 px-3 font-medium text-slate-500">Product</th>
                    <th className="text-center py-2 px-3 font-medium text-slate-500">Worst MAPE</th>
                    <th className="text-center py-2 px-3 font-medium text-slate-500">Horizon</th>
                    <th className="text-center py-2 px-3 font-medium text-slate-500">Date</th>
                  </tr>
                </thead>
                <tbody>
                  {top_misses.map((row, i) => (
                    <tr key={i} className="border-b border-slate-100 dark:border-slate-800">
                      <td className="py-2 px-3 font-medium text-slate-700 dark:text-slate-300">
                        {row.product_name}
                      </td>
                      <td className="py-2 px-3 text-center">
                        <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${mapeClass(row.worst_mape)}`}>
                          {row.worst_mape}%
                        </span>
                      </td>
                      <td className="py-2 px-3 text-center text-slate-600">{row.horizon}d</td>
                      <td className="py-2 px-3 text-center text-slate-500">
                        {row.date ? row.date.slice(0, 10) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
