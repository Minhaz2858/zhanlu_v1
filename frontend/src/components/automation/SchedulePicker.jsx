import { useEffect, useMemo, useRef, useState } from 'react';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Clock, CalendarDays, Repeat, Hash, AlertCircle, ListOrdered } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { nextRuns, formatRun } from '@/lib/nextRuns';

/**
 * SchedulePicker — visual schedule picker for automation tasks.
 *
 * Replaces the legacy freeform text input with a structured set of
 * controls (Frequency / Time / Day-of-week / Cron) that emits a
 * human-readable string. The output format is intentionally a
 * plain string so the existing automation pipeline (which treats
 * `schedule` as opaque text) keeps working.
 *
 * Frequencies:
 *   - once     → "2026-08-15 14:30"
 *   - hourly   → "Every hour"
 *   - daily    → "Daily 08:00"
 *   - weekly   → "Weekly Mon, Wed, Fri 09:00"
 *   - monthly  → "Monthly 15th 09:00"
 *   - custom   → "Custom: 0 8 * * *"
 */
export default function SchedulePicker({ value, onChange }) {
  const { t, lang } = useLanguage();
  const isEn = lang === 'en';
  const s = t.createDialog.schedulePicker;

  // Best-effort detection of an existing value so we don't blow away
  // schedules that were authored in the legacy freeform field. The
  // detection is intentionally permissive — if we can't parse, we
  // fall back to `custom` and use the raw string as the cron field.
  const parsed = useMemo(() => parseInitial(value, s), [value, s]);

  const [frequency, setFrequency] = useState(parsed.frequency);
  const [time, setTime] = useState(parsed.time);
  const [date, setDate] = useState(parsed.date);
  const [days, setDays] = useState(parsed.days); // array of 0–6 (Mon=0 .. Sun=6)
  const [dayOfMonth, setDayOfMonth] = useState(parsed.dayOfMonth);
  const [cron, setCron] = useState(parsed.cron);

  // Sync internal state when the parent passes a new `value` (e.g.
  // when a template pill is applied). Without this, the picker keeps
  // its old state and the user has to manually re-pick the schedule.
  const lastExternalValue = useRef(value);
  useEffect(() => {
    if (value === lastExternalValue.current) return;
    lastExternalValue.current = value;
    const p = parseInitial(value, s);
    setFrequency(p.frequency);
    setTime(p.time);
    setDate(p.date);
    setDays(p.days);
    setDayOfMonth(p.dayOfMonth);
    setCron(p.cron);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  useEffect(() => {
    const next = composeString({ frequency, time, date, days, dayOfMonth, cron }, s, isEn);
    lastExternalValue.current = next;
    if (next !== value) onChange(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [frequency, time, date, days, dayOfMonth, cron]);

  function toggleDay(idx) {
    setDays((prev) => prev.includes(idx) ? prev.filter((d) => d !== idx) : [...prev, idx].sort());
  }

  const cronValid = frequency !== 'custom' || isValidCron(cron);

  return (
    <div className="space-y-3">
      {/* Frequency row */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_140px]">
        <div>
          <Label className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Repeat className="h-3 w-3" /> {s.frequency}
          </Label>
          <Select value={frequency} onValueChange={setFrequency}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="once">{s.once}</SelectItem>
              <SelectItem value="hourly">{s.hourly}</SelectItem>
              <SelectItem value="daily">{s.daily}</SelectItem>
              <SelectItem value="weekly">{s.weekly}</SelectItem>
              <SelectItem value="monthly">{s.monthly}</SelectItem>
              <SelectItem value="custom">{s.custom}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {frequency !== 'hourly' && frequency !== 'custom' && (
          <div>
            <Label className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Clock className="h-3 w-3" /> {s.time}
            </Label>
            <Input type="time" value={time} onChange={(e) => setTime(e.target.value)} className="font-mono" />
          </div>
        )}
      </div>

      {/* Once: date picker */}
      {frequency === 'once' && (
        <div>
          <Label className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <CalendarDays className="h-3 w-3" /> {isEn ? 'Date' : '日期'}
          </Label>
          <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </div>
      )}

      {/* Weekly: day-of-week pills */}
      {frequency === 'weekly' && (
        <div>
          <Label className="mb-1.5 block text-xs text-muted-foreground">{s.dayOfWeek}</Label>
          <div className="flex flex-wrap gap-1.5">
            {s.weekdays.map((d, i) => {
              const active = days.includes(i);
              return (
                <button
                  key={i}
                  type="button"
                  onClick={() => toggleDay(i)}
                  className={
                    'h-9 min-w-[2.25rem] rounded-full border px-3 text-xs font-medium transition-colors ' +
                    (active
                      ? 'border-primary bg-primary text-primary-foreground'
                      : 'border-border bg-card text-muted-foreground hover:border-primary/40 hover:text-foreground')
                  }
                >
                  {d}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Monthly: day-of-month input */}
      {frequency === 'monthly' && (
        <div className="max-w-[160px]">
          <Label className="mb-1.5 block text-xs text-muted-foreground">{s.dayOfMonth}</Label>
          <div className="relative">
            <Input
              type="number" min={1} max={31} value={dayOfMonth}
              onChange={(e) => setDayOfMonth(clampDay(e.target.value))}
              className="pr-10 font-mono"
            />
            <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-xs text-muted-foreground">
              {s.monthlyDayPh}
            </span>
          </div>
        </div>
      )}

      {/* Custom cron */}
      {frequency === 'custom' && (
        <div>
          <Label className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Hash className="h-3 w-3" /> {s.cron}
          </Label>
          <Input
            value={cron}
            onChange={(e) => setCron(e.target.value)}
            placeholder={s.cronPh}
            className="font-mono"
          />
          {!cronValid && (
            <p className="mt-1.5 flex items-center gap-1 text-xs text-destructive">
              <AlertCircle className="h-3 w-3" /> {s.invalidCron}
            </p>
          )}
        </div>
      )}

      {/* Live preview + next-runs panel. The next-runs list is
          recomputed on every state change so the user can sanity-check
          the schedule at a glance — e.g. confirm "Weekly Mon-Fri 09:00"
          actually fires on Mon-Fri and not on the weekend. */}
      <div className="space-y-1.5 rounded-md border border-dashed border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
        <div className="flex items-center gap-2">
          <Clock className="h-3 w-3 shrink-0" />
          <span className="shrink-0 font-medium">{s.previewLabel}：</span>
          <span className="truncate font-mono text-foreground/80">{value || '—'}</span>
        </div>
        <NextRunsPanel state={{ frequency, time, date, days, dayOfMonth, cron }} label={t.createDialog.nextRuns.label} empty={t.createDialog.nextRuns.empty} more={t.createDialog.nextRuns.more} isEn={isEn} />
      </div>
    </div>
  );
}

function NextRunsPanel({ state, label, empty, more, isEn }) {
  // Cron validation: if the user is in "custom" mode with an invalid
  // expression, don't show phantom runs — wait until they fix it.
  if (state.frequency === 'custom' && !isValidCron(state.cron)) return null;

  const runs = useMemo(() => nextRuns(state, 4), [state]);
  if (!runs.length) {
    return (
      <div className="flex items-center gap-1.5 pl-5 text-[11px] italic text-muted-foreground/80">
        <ListOrdered className="h-3 w-3" /> {empty}
      </div>
    );
  }
  const [first, ...rest] = runs;
  const shown = formatRun(first, isEn);
  const extra = runs.length - 1;
  return (
    <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 pl-5 text-[11px]">
      <ListOrdered className="h-3 w-3 shrink-0 text-primary" />
      <span className="font-medium text-foreground/70">{label}：</span>
      <span className="font-mono text-foreground">{shown}</span>
      {extra > 0 && (
        <span className="text-muted-foreground/80">（{more} {extra}）</span>
      )}
    </div>
  );
}

function clampDay(raw) {
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) return 1;
  if (n < 1) return 1;
  if (n > 31) return 31;
  return n;
}

function isValidCron(expr) {
  if (!expr || typeof expr !== 'string') return false;
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  return parts.every((p) => /^[0-9*,\-/]+$/.test(p));
}

function composeString(state, s, isEn) {
  const { frequency, time, date, days, dayOfMonth, cron } = state;
  switch (frequency) {
    case 'once': {
      if (!date) return '';
      return `${date} ${time || '00:00'}`;
    }
    case 'hourly':
      return isEn ? 'Every hour' : '每小时';
    case 'daily':
      return `${s.daily} ${time || '00:00'}`;
    case 'weekly': {
      if (!days.length) return '';
      const labels = days.map((i) => s.weekdays[i]);
      return `${s.weekly} ${labels.join(', ')} ${time || '00:00'}`;
    }
    case 'monthly': {
      const n = dayOfMonth || 1;
      const ordinal = isEn ? ordinalEn(n) : `${n}日`;
      return `${s.monthly} ${ordinal} ${time || '00:00'}`;
    }
    case 'custom':
      return cron && isValidCron(cron) ? `Custom: ${cron}` : (cron || '');
    default:
      return '';
  }
}

function ordinalEn(n) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod10 !== 11) return `${n}st`;
  if (mod10 === 2 && mod100 !== 12) return `${n}nd`;
  if (mod10 === 3 && mod100 !== 13) return `${n}rd`;
  return `${n}th`;
}

/**
 * Best-effort parse of a pre-existing freeform value so we don't
 * lose schedules authored in the legacy text field. Maps to the
 * closest structured frequency.
 */
function parseInitial(value, s) {
  const empty = {
    frequency: 'daily', time: '08:00', date: '', days: [0, 1, 2, 3, 4], dayOfMonth: 1, cron: '',
  };
  if (!value || typeof value !== 'string') return empty;
  const v = value.trim();
  const lower = v.toLowerCase();

  if (/^custom\s*:/i.test(v) || isValidCron(v)) {
    return { ...empty, frequency: 'custom', cron: v.replace(/^custom\s*:/i, '').trim() };
  }
  if (/every\s*hour|^每小时$/i.test(v)) {
    return { ...empty, frequency: 'hourly' };
  }
  // Match "Daily 08:00" / "每日 08:00"
  const daily = v.match(/(\d{1,2}):(\d{2})/);
  if (daily && (lower.includes('daily') || v.includes('每日'))) {
    return { ...empty, frequency: 'daily', time: `${pad(daily[1])}:${daily[2]}` };
  }
  // Match "Weekly Mon, Wed 09:00" or "每周一 09:00"
  if (lower.includes('weekly') || /每周/.test(v)) {
    const timeMatch = v.match(/(\d{1,2}):(\d{2})/);
    return {
      ...empty, frequency: 'weekly',
      time: timeMatch ? `${pad(timeMatch[1])}:${timeMatch[2]}` : '08:00',
      days: [0, 1, 2, 3, 4],
    };
  }
  // Match "Monthly 15th 09:00" or "每月15日 09:00"
  if (lower.includes('monthly') || /每月/.test(v)) {
    const dayMatch = v.match(/(\d{1,2})/);
    const timeMatch = v.match(/(\d{1,2}):(\d{2})/);
    return {
      ...empty, frequency: 'monthly',
      dayOfMonth: dayMatch ? Math.min(31, Math.max(1, parseInt(dayMatch[1], 10))) : 1,
      time: timeMatch ? `${pad(timeMatch[1])}:${timeMatch[2]}` : '08:00',
    };
  }
  // Match "2026-08-15 14:30" (one-shot)
  const once = v.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})/);
  if (once) {
    return { ...empty, frequency: 'once', date: once[1], time: `${pad(once[2])}:${once[3]}` };
  }
  // Unknown string: put it in the custom cron field as a fallback.
  return { ...empty, frequency: 'custom', cron: v };
}

function pad(n) { return String(n).padStart(2, '0'); }
