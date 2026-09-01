/**
 * Zhanlu time formatting — all user-visible timestamps are rendered in
 * Asia/Shanghai (UTC+8 / CST).  Internal storage stays UTC.
 *
 * Every function here accepts an ISO 8601 string (which may or may not
 * carry a timezone suffix) and formats it for display.
 *
 * Shared utility — used across ~20 display files.
 */

const TZ = 'Asia/Shanghai';

/**
 * Parse an ISO timestamp string into a UTC `Date`.
 *
 * The server stores timestamps in UTC but the API returns naive ISO
 * strings ("2026-07-24T06:35:44.310907") WITHOUT a timezone suffix.
 * JavaScript's Date constructor parses naive strings as LOCAL time.
 * If the string has no timezone marker we append "Z" so it's parsed
 * as UTC.  Already-suffixed strings ('...Z' or '...+HH:MM') are left
 * alone.
 */
function parseUTC(iso) {
  if (!iso) return NaN;
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const d = new Date(hasTz ? iso : iso + 'Z');
  return Number.isNaN(d.getTime()) ? NaN : d;
}

// ------------------------------------------------------------------
// Relative time (unchanged logic — already correct)
// ------------------------------------------------------------------

/**
 * Format an ISO timestamp as a short relative time string
 * ("just now", "5m ago", "3h ago", "2d ago", "3w ago", ...).
 */
export function formatRelativeTime(iso, lang = 'en') {
  if (!iso) return '';
  const now = Date.now();
  const t = parseUTC(iso);
  if (Number.isNaN(t)) return '';
  const deltaSec = Math.max(0, Math.floor((now - t.getTime()) / 1000));
  const minutes = Math.floor(deltaSec / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  const isEn = lang === 'en';
  if (minutes < 1) return isEn ? 'just now' : '刚刚';
  if (minutes < 60) return isEn ? `${minutes}m ago` : `${minutes} 分钟前`;
  if (hours < 24) return isEn ? `${hours}h ago` : `${hours} 小时前`;
  if (days < 7) return isEn ? `${days}d ago` : `${days} 天前`;
  if (days < 30) {
    const w = Math.floor(days / 7);
    return isEn ? `${w}w ago` : `${w} 周前`;
  }
  if (days < 365) {
    const mo = Math.floor(days / 30);
    return isEn ? `${mo}mo ago` : `${mo} 个月前`;
  }
  const y = Math.floor(days / 365);
  return isEn ? `${y}y ago` : `${y} 年前`;
}

// ------------------------------------------------------------------
// Absolute time (all pinned to Asia/Shanghai)
// ------------------------------------------------------------------

/**
 * Full datetime: "2026-08-11 14:52"
 */
export function formatAbsoluteTime(iso) {
  if (!iso) return '—';
  const d = parseUTC(iso);
  if (Number.isNaN(d)) return '—';
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const get = (t) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`;
}

/**
 * Date only: "2026-08-11"
 */
export function formatDate(iso) {
  if (!iso) return '—';
  const d = parseUTC(iso);
  if (Number.isNaN(d)) return '—';
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(d);
  const get = (t) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}`;
}

/**
 * Time of day: "14:52"
 */
export function formatTimeOfDay(iso) {
  if (!iso) return '—';
  const d = parseUTC(iso);
  if (Number.isNaN(d)) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: TZ,
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(d);
}

/**
 * Short datetime (MM-DD HH:mm): "08-11 14:52"
 * Matches the `fmtTime` pattern used across 6+ files.
 */
export function formatShortDateTime(iso) {
  if (!iso) return '—';
  const d = parseUTC(iso);
  if (Number.isNaN(d)) return '—';
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: TZ,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const get = (t) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`;
}

/**
 * Run date for scheduled panels: "8月11日" (zh) or "Aug 11" (en)
 */
export function formatRunDate(iso, lang = 'zh') {
  if (!iso) return '—';
  const d = parseUTC(iso);
  if (Number.isNaN(d)) return '—';
  if (lang === 'en') {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: TZ,
      month: 'short',
      day: 'numeric',
    }).format(d);
  }
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: TZ,
    month: 'long',
    day: 'numeric',
  }).format(d);
}
