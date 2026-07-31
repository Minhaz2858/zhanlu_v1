/**
 * nextRuns — given a SchedulePicker state or a freeform schedule
 * string, return the next N run times as Date objects.
 *
 * Supports all six picker modes (once / hourly / daily / weekly /
 * monthly / custom-cron) plus the best-effort string formats the
 * picker itself emits ("Daily 08:00", "Weekly Mon, Wed 09:00",
 * "Monthly 1st 09:00", "Every hour", "Custom: 0 8 * * *", and
 * "2026-08-15 14:30").
 *
 * The cron parser is intentionally minimal — it handles the
 * common patterns (asterisks, specific values, ranges, lists,
 * and steps) but is NOT a full cron implementation. For unusual
 * cron expressions (e.g. last-day-of-month, weekday-near-X) it
 * falls back to a coarse approximation or returns an empty list.
 */
// Convention: the SchedulePicker uses Mon=0..Sun=6 for its `days`
// state array. We mirror that here so string parsing and state input
// flow through the same pipeline. The (d + 1) % 7 conversion to
// JS getDay() (Sun=0..Sat=6) happens once, inside `nextWeekly`.
const WEEKDAY_MAP_EN = { mon: 0, tue: 1, wed: 2, thu: 3, fri: 4, sat: 5, sun: 6 };
const WEEKDAY_MAP_ZH = { 一: 0, 二: 1, 三: 2, 四: 3, 五: 4, 六: 5, 日: 6, 天: 6 };

export function nextRuns(input, count = 5, now = new Date()) {
  const state = normalize(input);
  if (!state) return [];
  switch (state.kind) {
    case 'once': {
      const d = new Date(state.date);
      const [h, m] = (state.time || '00:00').split(':').map((x) => parseInt(x, 10));
      d.setHours(h || 0, m || 0, 0, 0);
      return d > now ? [d] : [];
    }
    case 'hourly': return nextHourly(count, now, state.interval);
    case 'daily': return nextDaily(count, now, parseTime(state.time || '00:00'));
    case 'weekly': return nextWeekly(count, now, state.days, parseTime(state.time || '00:00'));
    case 'monthly': return nextMonthly(count, now, state.day, parseTime(state.time || '00:00'));
    case 'cron': return nextCron(count, now, state.expr);
    default: return [];
  }
}

/**
 * Normalize a freeform schedule string OR a SchedulePicker state
 * into a single canonical object the run computer understands.
 */
function normalize(input) {
  if (!input) return null;
  if (typeof input === 'object' && input !== null) {
    // Direct state object: { frequency, time, date, days, dayOfMonth, cron }
    if (input.frequency) {
      if (input.frequency === 'custom') {
        return { kind: 'cron', expr: input.cron || '' };
      }
      return {
        kind: input.frequency,
        time: input.time || '00:00',
        date: input.date,
        days: input.days,
        day: input.dayOfMonth,
        interval: 1,
      };
    }
    return null;
  }
  const v = String(input).trim();
  if (!v) return null;
  const lower = v.toLowerCase();

  if (/^custom\s*:/i.test(v)) {
    return { kind: 'cron', expr: v.replace(/^custom\s*:/i, '').trim() };
  }
  if (/every\s*hour|^每小时$/i.test(v)) return { kind: 'hourly', interval: 1 };
  const m = v.match(/^every\s+(\d+)\s+hours?$/i);
  if (m) return { kind: 'hourly', interval: Math.max(1, parseInt(m[1], 10)) };

  const once = v.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})/);
  if (once) return { kind: 'once', date: once[1], time: `${pad(once[2])}:${once[3]}` };

  const dailyMatch = lower.includes('daily') || /每日/.test(v);
  if (dailyMatch) {
    const t = v.match(/(\d{1,2}):(\d{2})/);
    return { kind: 'daily', time: t ? `${pad(t[1])}:${t[2]}` : '00:00' };
  }
  const weeklyMatch = lower.includes('weekly') || /每周/.test(v);
  if (weeklyMatch) {
    const t = v.match(/(\d{1,2}):(\d{2})/);
    return {
      kind: 'weekly',
      days: parseDays(v),
      time: t ? `${pad(t[1])}:${t[2]}` : '08:00',
    };
  }
  const monthlyMatch = lower.includes('monthly') || /每月/.test(v);
  if (monthlyMatch) {
    const day = v.match(/(\d{1,2})/);
    const t = v.match(/(\d{1,2}):(\d{2})/);
    return {
      kind: 'monthly',
      day: day ? Math.min(31, Math.max(1, parseInt(day[1], 10))) : 1,
      time: t ? `${pad(t[1])}:${t[2]}` : '08:00',
    };
  }
  if (isLikelyCron(v)) return { kind: 'cron', expr: v };
  return null;
}

function parseDays(text) {
  // Default to weekdays if no day list is found.
  const days = new Set();
  const en = text.match(/\b(sun|mon|tue|wed|thu|fri|sat)\b/gi) || [];
  for (const d of en) days.add(WEEKDAY_MAP_EN[d.slice(0, 3).toLowerCase()]);
  const zh = text.match(/[一二三四五六日天]/g) || [];
  for (const d of zh) days.add(WEEKDAY_MAP_ZH[d]);
  if (days.size === 0) [0, 1, 2, 3, 4].forEach((d) => days.add(d));
  return [...days].sort();
}

function parseTime(s) {
  const m = s.match(/(\d{1,2}):(\d{2})/);
  if (!m) return [0, 0];
  return [parseInt(m[1], 10) % 24, parseInt(m[2], 10) % 60];
}

function pad(n) { return String(n).padStart(2, '0'); }

function isLikelyCron(s) {
  return /^[0-9*,\-/]+\s+[0-9*,\-/]+\s+[0-9*,\-/]+\s+[0-9*,\-/]+\s+[0-9*,\-/]+$/.test(s);
}

// ── Generators ─────────────────────────────────────────────

function nextHourly(count, now, interval) {
  const out = [];
  const d = new Date(now);
  d.setMinutes(0, 0, 0);
  if (d <= now) d.setHours(d.getHours() + interval);
  while (out.length < count) {
    out.push(new Date(d));
    d.setHours(d.getHours() + interval);
  }
  return out;
}

function nextDaily(count, now, [h, m]) {
  const out = [];
  const d = new Date(now);
  d.setHours(h, m, 0, 0);
  if (d <= now) d.setDate(d.getDate() + 1);
  while (out.length < count) {
    out.push(new Date(d));
    d.setDate(d.getDate() + 1);
  }
  return out;
}

function nextWeekly(count, now, days, [h, m]) {
  if (!days || !days.length) return [];
  // Convert picker days (Mon=0..Sun=6) to JS getDay() (Sun=0..Sat=6)
  const jsDays = days.map((d) => (d + 1) % 7).sort((a, b) => a - b);
  const out = [];
  // Look ahead up to 14 days to fill `count` slots.
  for (let offset = 0; offset < 14 && out.length < count; offset++) {
    const d = new Date(now);
    d.setDate(d.getDate() + offset);
    d.setHours(h, m, 0, 0);
    if (d <= now) continue;
    if (jsDays.includes(d.getDay())) out.push(d);
  }
  return out;
}

function nextMonthly(count, now, day, [h, m]) {
  const out = [];
  for (let offset = 0; offset < 12 && out.length < count; offset++) {
    const d = new Date(now.getFullYear(), now.getMonth() + offset, 1, h, m, 0, 0);
    const target = Math.min(day, daysInMonth(d.getFullYear(), d.getMonth()));
    d.setDate(target);
    if (d > now) out.push(d);
  }
  return out;
}

function daysInMonth(year, month) {
  return new Date(year, month + 1, 0).getDate();
}

// ── Minimal cron parser (5-field) ─────────────────────────
//
// Supports:  *, n, n-m, n,m,..., */k, n-m/k
// Does NOT support:  L, W, #, ?, named months/days, year field.

const MONTH_NAMES = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
const DOW_NAMES = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };

function parseField(token, min, max, names) {
  const allow = new Set();
  for (const part of token.split(',')) {
    const [range, stepStr] = part.split('/');
    const step = stepStr ? Math.max(1, parseInt(stepStr, 10)) : 1;
    if (range === '*') {
      for (let v = min; v <= max; v += step) allow.add(v);
    } else if (range.includes('-')) {
      let [a, b] = range.split('-');
      a = names?.[a.slice(0, 3).toLowerCase()] ?? parseInt(a, 10);
      b = names?.[b.slice(0, 3).toLowerCase()] ?? parseInt(b, 10);
      for (let v = a; v <= b; v += step) allow.add(v);
    } else {
      // Single value: add only that value. The "n/k" step form is
      // "every k starting at n" which we expand explicitly here.
      const v = names?.[range.slice(0, 3).toLowerCase()] ?? parseInt(range, 10);
      if (stepStr) {
        for (let i = v; i <= max; i += step) allow.add(i);
      } else {
        allow.add(v);
      }
    }
  }
  return allow;
}

function nextCron(count, now, expr) {
  if (!isLikelyCron(expr)) return [];
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return [];
  const [minField, hourField, domField, monField, dowField] = parts;
  const minutes = parseField(minField, 0, 59);
  const hours = parseField(hourField, 0, 23);
  const months = parseField(monField, 1, 12, MONTH_NAMES);
  // dom and dow are special in cron (OR semantics when both are *).
  // We coerce to a single "is-match-day" set.
  const doms = domField === '*' ? null : parseField(domField, 1, 31);
  const dows = dowField === '*' ? null : parseField(dowField, 0, 6, DOW_NAMES);

  const out = [];
  const cursor = new Date(now);
  cursor.setSeconds(0, 0);
  cursor.setMinutes(cursor.getMinutes() + 1);

  // Limit the search to 366 days to avoid pathological cases.
  const limit = new Date(now.getTime() + 366 * 86400000);
  while (out.length < count && cursor < limit) {
    if (!months.has(cursor.getMonth() + 1)) { cursor.setMonth(cursor.getMonth() + 1, 1); cursor.setHours(0, 0, 0, 0); continue; }
    const dayMatches = (doms == null && dows == null)
      || (doms != null && doms.has(cursor.getDate()))
      || (dows != null && dows.has(cursor.getDay()));
    if (!dayMatches) { cursor.setDate(cursor.getDate() + 1); cursor.setHours(0, 0, 0, 0); continue; }
    if (!hours.has(cursor.getHours())) { cursor.setHours(cursor.getHours() + 1, 0, 0, 0); continue; }
    if (!minutes.has(cursor.getMinutes())) { cursor.setMinutes(cursor.getMinutes() + 1, 0, 0); continue; }
    out.push(new Date(cursor));
    cursor.setMinutes(cursor.getMinutes() + 1);
  }
  return out;
}

// ── Formatting helpers ─────────────────────────────────────

/**
 * Format a Date as a short label like "Mon 28 Jul 09:00". Bilingual
 * via the `isEn` flag — keeps the rest of the app free of
 * per-locale date libraries.
 */
export function formatRun(d, isEn) {
  if (!(d instanceof Date) || Number.isNaN(d.getTime())) return '';
  if (isEn) {
    const w = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()];
    const m = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getMonth()];
    return `${w} ${d.getDate()} ${m} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  return `${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
