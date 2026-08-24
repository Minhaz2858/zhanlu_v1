import React, { useEffect, useState } from 'react';
import { getRegimeStatus } from '../../../api/marketDashboard';

/**
 * DynamicConfidenceBadge
 *
 * Replaces the static trust-tier badge with a regime-aware badge that
 * reflects:
 *   - Current market regime (bull / bear / volatile / sideways)
 *   - Recent model accuracy (MAPE)
 *   - Directional accuracy
 *   - Distribution shift score (placeholder for P3-1D)
 *
 * Color scheme:
 *   🟢 high   — stable regime, MAPE < 5%
 *   🟡 medium  — elevated volatility or MAPE 5-10%
 *   🔴 low     — trend break or MAPE > 10%
 */

const LEVEL_STYLES = {
  high: {
    dot: 'bg-green-500',
    text: 'text-green-700 dark:text-green-300',
    bg: 'bg-green-50 dark:bg-green-900/20',
    border: 'border-green-200 dark:border-green-800',
    label: { zh: '高', en: 'High' },
  },
  medium: {
    dot: 'bg-yellow-500',
    text: 'text-yellow-700 dark:text-yellow-300',
    bg: 'bg-yellow-50 dark:bg-yellow-900/20',
    border: 'border-yellow-200 dark:border-yellow-800',
    label: { zh: '中', en: 'Medium' },
  },
  low: {
    dot: 'bg-red-500',
    text: 'text-red-700 dark:text-red-300',
    bg: 'bg-red-50 dark:bg-red-900/20',
    border: 'border-red-200 dark:border-red-800',
    label: { zh: '低', en: 'Low' },
  },
};

const REGIME_ICONS = {
  bull: '📈',
  bear: '📉',
  volatile: '⚡',
  sideways: '➡️',
  unknown: '❓',
};

export default function DynamicConfidenceBadge({ productKey, lang = 'zh' }) {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getRegimeStatus(productKey)
      .then((data) => {
        if (!cancelled) {
          setStatus(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err);
          setStatus(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [productKey]);

  if (loading) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
        <span className="h-2 w-2 rounded-full bg-slate-300 animate-pulse" />
        {lang === 'zh' ? '加载中…' : 'Loading…'}
      </span>
    );
  }

  if (error || !status) {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
        <span className="h-2 w-2 rounded-full bg-slate-300" />
        —
      </span>
    );
  }

  const level = status.confidence_level || 'medium';
  const style = LEVEL_STYLES[level] || LEVEL_STYLES.medium;
  const regimeIcon = REGIME_ICONS[status.regime] || REGIME_ICONS.unknown;
  const label = style.label[lang] || style.label.en;

  return (
    <span
      className={`inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-md border ${style.bg} ${style.border} ${style.text} cursor-help`}
      title={status.message || ''}
    >
      <span className={`h-2 w-2 rounded-full ${style.dot}`} />
      <span>{label}</span>
      <span className="opacity-60">{regimeIcon}</span>
      {status.recent_mape !== null && status.recent_mape !== undefined && (
        <span className="opacity-60">MAPE {status.recent_mape}%</span>
      )}
    </span>
  );
}
