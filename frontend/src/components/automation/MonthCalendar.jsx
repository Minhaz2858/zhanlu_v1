import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft, ChevronRight, Sparkles } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';

const WEEK_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

function pad(n) { return String(n).padStart(2, '0'); }
function fmtHM(v) {
  if (!v) return '';
  const d = new Date(v);
  const h = pad(d.getHours());
  const m = pad(d.getMinutes());
  return `${h}:${m}`;
}
function sameDay(a, b) {
  const d1 = new Date(a), d2 = new Date(b);
  return d1.getFullYear() === d2.getFullYear() && d1.getMonth() === d2.getMonth() && d1.getDate() === d2.getDate();
}

export default function MonthCalendar({ tasks, onNew }) {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const today = new Date();
  const [cursor, setCursor] = useState(new Date(today.getFullYear(), today.getMonth(), 1));
  const translate = useTranslate(tasks.map((x) => x.name).filter(Boolean), lang);

  const grid = useMemo(() => {
    const year = cursor.getFullYear();
    const month = cursor.getMonth();
    const first = new Date(year, month, 1);
    const startDay = (first.getDay() + 6) % 7; // 0=Mon
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const cells = [];
    for (let i = 0; i < startDay; i++) cells.push(null);
    for (let d = 1; d <= daysInMonth; d++) cells.push(new Date(year, month, d));
    while (cells.length % 7 !== 0) cells.push(null);
    return cells;
  }, [cursor]);

  // Day → entries map. Each entry is {task, kind} where kind='run' (past
  // execution on task.last_run) or kind='next' (scheduled firing on
  // task.next_run_at). Before this change the calendar keyed ONLY off
  // last_run, so scheduled future tasks were invisible — it was a history
  // chart, not a schedule view.
  const dayEntries = useMemo(() => {
    const map = {};
    tasks.forEach((task) => {
      if (task.last_run) {
        const idx = grid.findIndex((d) => d && sameDay(d, task.last_run));
        if (idx !== -1) (map[idx] = map[idx] || []).push({ task, kind: 'run' });
      }
      if (task.next_run_at) {
        const idx = grid.findIndex((d) => d && sameDay(d, task.next_run_at));
        if (idx !== -1) (map[idx] = map[idx] || []).push({ task, kind: 'next' });
      }
    });
    // Sort each day's chips chronologically: upcoming next_run first, then
    // past runs by their last_run time.
    Object.keys(map).forEach((k) => {
      map[k].sort((a, b) => {
        const ta = a.kind === 'next' ? new Date(a.task.next_run_at) : new Date(a.task.last_run);
        const tb = b.kind === 'next' ? new Date(b.task.next_run_at) : new Date(b.task.last_run);
        return ta - tb;
      });
    });
    return map;
  }, [tasks, grid]);

  const monthLabel = `${cursor.getFullYear()}年${cursor.getMonth() + 1}月`;
  const weekLabels = WEEK_KEYS.map((k) => t.automation.weekDays[k]);

  function prev() { setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1)); }
  function next() { setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1)); }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="font-display text-3xl tracking-tight text-foreground">{monthLabel}</h2>
          <p className="mt-2 max-w-xl text-sm text-muted-foreground">{t.automation.calendarDesc}</p>
        </div>
      </div>

      <div className="mb-3 flex items-center gap-1">
        <button onClick={prev} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"><ChevronLeft className="h-4 w-4" /></button>
        <button onClick={next} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"><ChevronRight className="h-4 w-4" /></button>
      </div>

      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <div className="grid grid-cols-7 border-b border-border text-[11px] uppercase tracking-wide text-muted-foreground">
          {weekLabels.map((w) => (
            <div key={w} className="px-3 py-2 text-center font-medium">{w}</div>
          ))}
        </div>
        <div className="grid grid-cols-7">
          {grid.map((date, idx) => {
            if (!date) return <div key={idx} className="min-h-[120px] border-b border-r border-border bg-secondary/30" />;
            const entries = dayEntries[idx] || [];
            const isToday = sameDay(date, today);
            return (
              <div key={idx} className="min-h-[120px] border-b border-r border-border p-2">
                <div className="mb-1.5 flex justify-start">
                  <span className={`inline-flex h-6 w-6 items-center justify-center text-xs ${isToday ? 'rounded-full bg-primary text-primary-foreground' : 'text-muted-foreground'}`}>{date.getDate()}</span>
                </div>
                <div className="space-y-1">
                  {entries.slice(0, 4).map(({ task, kind }) => {
                    const isActive = task.status === 'active' || task.status === 'running';
                    const isNext = kind === 'next';
                    const stamp = isNext ? task.next_run_at : task.last_run;
                    // Solid dark chip for an active task's past run; outline
                    // primary chip for a scheduled (upcoming) firing so the
                    // calendar reads as schedule + history.
                    return (
                      <button
                        key={`${kind}-${task.id}`}
                        onClick={() => navigate(`/automation/${task.id}`)}
                        className={`block w-full rounded-md border px-1.5 py-1 text-left text-[11px] leading-tight transition-colors ${
                          isNext
                            ? 'border-primary/40 bg-primary/5 text-primary hover:bg-primary/10'
                            : isActive
                              ? 'border-transparent bg-foreground text-background'
                              : 'border-border bg-secondary/60 text-foreground hover:bg-accent'
                        }`}
                        title={translate(task.name)}
                      >
                        <div className={`text-[10px] font-light ${isNext ? 'text-primary/70' : isActive ? 'text-background/70' : 'text-muted-foreground'}`}>{fmtHM(stamp)}</div>
                        <div className="truncate font-medium">{translate(task.name)}</div>
                      </button>
                    );
                  })}
                  {entries.length > 4 && (
                    <div className="inline-block rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">{t.automation.moreTasks.replace('{n}', String(entries.length - 4))}</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}