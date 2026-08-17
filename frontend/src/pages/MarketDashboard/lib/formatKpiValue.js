/**
 * formatKpiValue — compact placeholder for per-cell KPI numbers.
 *
 * A short dash ('--') keeps tight grids readable vs. the long
 * "No Data Available" string. Null/NaN (the norm for ERP supply-chain
 * data — missing inventory, days, or pending) render as '--'.
 *
 * @param {number|null|undefined} value
 * @param {string} [locale] BCP-47 locale tag (defaults to 'en-US').
 * @returns {string} '--' or a locale-formatted number.
 */
export function formatKpiValue(value, locale = 'en-US') {
  if (value == null || !Number.isFinite(value)) return '--';
  return value.toLocaleString(locale, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}
