/**
 * Format an ISO timestamp as a short relative time string
 * ("just now", "5m ago", "3h ago", "2d ago", "3w ago", ...).
 *
 * Shared utility — used by SessionList (per-chat timestamps in the
 * sidebar) and ProjectsView (project card timestamps).
 */
export function formatRelativeTime(iso, lang = 'en') {
  if (!iso) return '';
  const now = Date.now();
  // The server stores timestamps in UTC but the API returns naive
  // ISO strings ("2026-07-24T06:35:44.310907") without a timezone
  // suffix.  JavaScript's Date constructor parses naive strings as
  // LOCAL time, which shifts the displayed time by the user's TZ
  // offset (8h in CST).  If the string has no timezone marker,
  // append "Z" so it's parsed as UTC.  Already-suffixed strings
  // ('...Z' or '...+HH:MM') are left alone.
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const t = new Date(hasTz ? iso : iso + 'Z').getTime();
  if (Number.isNaN(t)) return '';
  const deltaSec = Math.max(0, Math.floor((now - t) / 1000));
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