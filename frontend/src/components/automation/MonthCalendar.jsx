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

  const tasksByDay = useMemo(() => {
    const map = {};
    tasks.forEach((task) => {
      if (!task.last_run) return;
      const idx = grid.findIndex((d) => d && sameDay(d, task.last_run));
      if (idx === -1) return;
      (map[idx] = map[idx] || []).push(task);
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
            const dayTasks = tasksByDay[idx] || [];
            const isToday = sameDay(date, today);
            return (
              <div key={idx} className="min-h-[120px] border-b border-r border-border p-2">
                <div className="mb-1.5 flex justify-start">
                  <span className={`inline-flex h-6 w-6 items-center justify-center text-xs ${isToday ? 'rounded-full bg-primary text-primary-foreground' : 'text-muted-foreground'}`}>{date.getDate()}</span>
                </div>
                <div className="space-y-1">
                  {dayTasks.slice(0, 3).map((task) => {
                    const isDark = task.status === 'running';
                    return (
                      <button
                        key={task.id}
                        onClick={() => navigate(`/automation/${task.id}`)}
                        className={`block w-full rounded-md border px-1.5 py-1 text-left text-[11px] leading-tight transition-colors ${isDark ? 'border-transparent bg-foreground text-background' : 'border-border bg-secondary/60 text-foreground hover:bg-accent'}`}
                        title={translate(task.name)}
                      >
                        <div className={`text-[10px] font-light ${isDark ? 'text-background/70' : 'text-muted-foreground'}`}>{fmtHM(task.last_run)}</div>
                        <div className="truncate font-medium">{translate(task.name)}</div>
                      </button>
                    );
                  })}
                  {dayTasks.length > 3 && (
                    <div className="inline-block rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">{t.automation.moreTasks.replace('{n}', dayTasks.length - 3)}</div>
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