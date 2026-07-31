// Ported from EDIA_5.1.2/frontend/src/lib/priceChange.ts
export const NO_DATA_LABEL = 'No Data Available';

export function getPriceChangeDirection(value) {
  if (value == null || !Number.isFinite(value)) return 'no_data';
  if (value > 0) return 'increase';
  if (value < 0) return 'decrease';
  return 'unchanged';
}

export function getPriceChangeTextClass(value, tone = 'default') {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return tone === 'bright' ? 'text-red-400' : 'text-red-600';
  if (direction === 'decrease') return tone === 'bright' ? 'text-green-400' : 'text-green-600';
  return tone === 'bright' ? 'text-slate-400' : 'text-slate-500';
}

export function getPriceChangeStrongTextClass(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return 'text-red-700';
  if (direction === 'decrease') return 'text-green-700';
  return 'text-slate-500';
}

export function getPriceChangeBadgeClass(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return 'bg-red-50 text-red-700';
  if (direction === 'decrease') return 'bg-green-50 text-green-700';
  return 'bg-slate-50 text-slate-600';
}

export function getPriceChangeLineColor(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return '#dc2626';
  if (direction === 'decrease') return '#16a34a';
  return '#64748b';
}

export function formatSignedPriceChange(value, options = {}, locale, suffix = '') {
  if (value == null || !Number.isFinite(value)) return NO_DATA_LABEL;
  const formatted = value.toLocaleString(locale, options);
  if (value === 0) return `0.00${suffix}`;
  return `${value > 0 ? '+' : ''}${formatted}${suffix}`;
}

export function getPriceChangeArrow(value) {
  const direction = getPriceChangeDirection(value);
  if (direction === 'increase') return 'up';
  if (direction === 'decrease') return 'down';
  return 'flat';
}
