import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import { Button } from '@/components/ui/button';
import { toast } from '@/components/ui/use-toast';
import { runAutomationTask } from '@/lib/runAutomationTask';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import PageHeader from '@/components/PageHeader';
import CreateResourceDialog from '@/components/CreateResourceDialog';
import { LayoutGrid, Table2, Play, Pause, Loader2, Clock, Calendar, Sparkles, Pencil, FolderOpen, Trash2 } from 'lucide-react';
import MonthCalendar from '@/components/automation/MonthCalendar';
import ProjectSelector from '@/components/automation/ProjectSelector';

import { formatShortDateTime } from '@/lib/time';

// "Run now" hands off to the shared runAutomationTask helper (see
// lib/runAutomationTask.js) — ensure-session -> POST /run -> navigate
// to /chat?session=<sid>. The dispatcher's _notify_chat streams the
// result into the task's owning session; no LLM-mediated handoff.

export default function AutomationTasks() {
  const { t, lang } = useLanguage();
  const [tasks, setTasks] = useState([]);
  const [view, setView] = useState('month');
  const [loading, setLoading] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedProject, setSelectedProject] = useState('all');
  const [deletingTask, setDeletingTask] = useState(null);
  const [deleting, setDeleting] = useState(false);
  // No right-side drawer here — user wanted the pop-up panel removed.
  // "Run now" lives directly on each card / row instead. Clicking it
  // hands the run off to the chat agent (see ``runNow`` below for the
  // full rationale) — either resuming the task's owning chat session
  // (if it was created via chat) or opening a fresh chat thread.
  const navigate = useNavigate();
  const [runningNow, setRunningNow] = useState(false);

  useEffect(() => { load(); }, []);
  async function load() {
    try {
      setLoading(true);
      setTasks(await base44.entities.AutomationTask.list('-updated_date', 200));
    } finally {
      setLoading(false);
    }
  }
  async function toggle(item) {
    await base44.entities.AutomationTask.update(item.id, { status: item.status === 'running' ? 'paused' : 'running' });
    load();
  }
  // "Run now" — adopt a dedicated chat session for this task
  // (Manus UX: one task = one chat), then hand the run off to the
  // chat agent by injecting a structured "Run Automation Task: ..."
  // prompt into the chatbox AND auto-sending it. The user sees:
  //   1. their own "input" (the prompt) as a message bubble,
  //   2. the agent's reply narrating ``execute_automation``,
  //   3. the dispatcher's run result streaming inline.
  // All three live in the same dedicated session so the user has a
  // single timeline of every run (manual + scheduled).
  //
  // Why hand off to the LLM (rather than firing
  // ``POST /api/automations/{id}/run`` directly and just navigating
  // to the session): the previous "fire API + navigate" path left
  // the user with a result message but no record of the *request*
  // that triggered it — the chat looked like the run happened on
  // its own. The prefill+autorun path makes the request visible and
  // routes through ``execute_automation`` so the agent narrates
  // ("Started Monthly Finance Report, execution id …") instead of
  // the result appearing out of nowhere. The "test" the user
  // reported earlier was the *project* name, not the agent — the
  // default ``general_assistant`` agent has ``execute_automation``
  // in its toolset, so the LLM will call it when it sees the
  // structured prompt.
  //
  // Why ensure-session first: tasks created before the
  // one-task-one-session migration may still be sharing a generic
  // session. The dispatcher's ``_notify_chat`` writes the run
  // result to ``task.session_id``; without adopting a dedicated
  // session the result lands in a shared, generically named chat.
  // ensure-session is idempotent — it returns the existing
  // matching session as a no-op.
  async function runNow(task) {
    if (!task || runningNow) return;
    setRunningNow(true);
    try {
      await runAutomationTask(task, { navigate, toast, lang });
    } finally {
      setRunningNow(false);
    }
  }

  async function confirmDelete() {
    if (!deletingTask || deleting) return;
    setDeleting(true);
    try {
      await base44.entities.AutomationTask.delete(deletingTask.id);
      toast({ title: t.automation.delete, description: deletingTask.name });
      setDeletingTask(null);
      load();
    } catch (e) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' });
    } finally {
      setDeleting(false);
    }
  }

  const translate = useTranslate(
    tasks.flatMap((x) => [x.name, x.description, x.schedule, x.last_result].filter(Boolean)),
    lang
  );

  const filteredTasks = selectedProject === 'all'
    ? tasks
    : tasks.filter((t) => (t.project || 'global') === selectedProject);

  const statusStyle = (s) => ({
    running: 'bg-green-100 text-green-700', paused: 'bg-amber-100 text-amber-700',
    completed: 'bg-blue-100 text-blue-700', failed: 'bg-red-100 text-red-700',
  }[s] || '');

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader title={t.automation.title} subtitle={t.automation.subtitle} action={
        <Button onClick={() => setDialogOpen(true)} className="shrink-0">
          <Sparkles className="h-4 w-4" /> {t.automation.newTask}
        </Button>
      } />

      <div className="mb-6 flex flex-wrap items-center justify-between gap-2 border-b border-border">
        <ProjectSelector value={selectedProject} onChange={setSelectedProject} allowAll />
        <div className="flex flex-wrap gap-1">
        <button onClick={() => setView('month')} className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm transition-colors ${view === 'month' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}><Calendar className="h-4 w-4" /> {t.automation.monthView}</button>
        <button onClick={() => setView('card')} className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm transition-colors ${view === 'card' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}><LayoutGrid className="h-4 w-4" /> {t.automation.cardView}</button>
        <button onClick={() => setView('table')} className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm transition-colors ${view === 'table' ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}><Table2 className="h-4 w-4" /> {t.automation.tableView}</button>
        </div>
      </div>

      {loading ? (
        <div className="flex justify-center py-20"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
      ) : filteredTasks.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-border py-20 text-center">
          <p className="text-sm text-muted-foreground">{t.automation.empty}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t.automation.emptyHint}</p>
        </div>
      ) : view === 'month' ? (
        <MonthCalendar tasks={filteredTasks} onNew={() => setDialogOpen(true)} />
      ) : view === 'card' ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {filteredTasks.map((item) => (
            <div key={item.id} className="group rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
              <div className="mb-3 flex items-start justify-between">
                <h3 className="min-w-0 flex-1 break-words font-display text-base text-foreground">{translate(item.name)}</h3>
                <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${statusStyle(item.status)}`}>{t.automation.statuses[item.status]}</span>
              </div>
              <p className="mb-4 break-words text-xs text-muted-foreground">{item.description ? translate(item.description) : t.automation.types[item.type]}</p>
              <div className="mb-4 space-y-1 text-xs text-muted-foreground">
                <div className="flex items-center gap-1.5"><FolderOpen className="h-3 w-3" /> {item.project && item.project !== 'global' ? item.project : t.automation.ungrouped}</div>
                <div className="flex items-center gap-1.5"><Clock className="h-3 w-3" /> {item.schedule ? translate(item.schedule) : t.automation.manual}</div>
                <div>{t.automation.lastRun}：{formatShortDateTime(item.last_run)}</div>
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => runNow(item)}
                  disabled={runningNow}
                  className="inline-flex items-center gap-1 rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
                >
                  {runningNow ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
                  {lang === 'en' ? 'Run now' : '立即运行'}
                </button>
                <button
                  type="button"
                  onClick={() => toggle(item)}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"
                >
                  {item.status === 'running' ? <><Pause className="h-3 w-3" /> {t.automation.pause}</> : <><Play className="h-3 w-3" /> {t.automation.start}</>}
                </button>
                <button
                  type="button"
                  onClick={() => navigate(`/automation/${item.id}`)}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"
                >
                  <Pencil className="h-3 w-3" /> {t.automation.edit || 'Edit'}
                </button>
                <button
                  type="button"
                  onClick={() => setDeletingTask(item)}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-red-600 hover:bg-red-50 hover:border-red-200"
                >
                  <Trash2 className="h-3 w-3" /> {t.automation.delete}
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-card">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-3 text-left font-medium">{t.automation.cols.name}</th>
                <th className="px-4 py-3 text-left font-medium">{t.automation.projectLabel}</th>
                <th className="px-4 py-3 text-left font-medium">{t.automation.cols.type}</th>
                <th className="px-4 py-3 text-left font-medium">{t.automation.cols.status}</th>
                <th className="px-4 py-3 text-left font-medium">{t.automation.cols.schedule}</th>
                <th className="px-4 py-3 text-left font-medium">{t.automation.cols.lastRun}</th>
                <th className="px-4 py-3 text-right font-medium">{t.automation.cols.actions}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {filteredTasks.map((item) => (
                <tr key={item.id} className="hover:bg-secondary/30">
                  <td className="break-words px-4 py-3 text-foreground">{translate(item.name)}</td>
                  <td className="break-words px-4 py-3 text-muted-foreground">{item.project && item.project !== 'global' ? item.project : t.automation.ungrouped}</td>
                  <td className="break-words px-4 py-3 text-muted-foreground">{t.automation.types[item.type]}</td>
                  <td className="px-4 py-3"><span className={`rounded-full px-2 py-0.5 text-xs ${statusStyle(item.status)}`}>{t.automation.statuses[item.status]}</span></td>
                  <td className="break-words px-4 py-3 text-muted-foreground">{item.schedule ? translate(item.schedule) : t.automation.manualShort}</td>
                  <td className="px-4 py-3 text-muted-foreground">{formatShortDateTime(item.last_run)}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      onClick={() => runNow(item)}
                      disabled={runningNow}
                      className="text-xs text-primary hover:underline disabled:opacity-50"
                    >
                      {lang === 'en' ? 'Run' : '运行'}
                    </button>
                    <button type="button" onClick={() => toggle(item)} className="ml-3 text-xs text-primary hover:underline">{item.status === 'running' ? t.automation.pause : t.automation.start}</button>
                    <button type="button" onClick={() => setDeletingTask(item)} className="ml-3 text-xs text-red-600 hover:underline">{t.automation.delete}</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <CreateResourceDialog open={dialogOpen} onOpenChange={setDialogOpen} resourceType="automation" />

      <AlertDialog open={!!deletingTask} onOpenChange={(o) => !o && setDeletingTask(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t.automation.delete}</AlertDialogTitle>
            <AlertDialogDescription>{t.automation.deleteConfirm}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>{t.common?.cancel || 'Cancel'}</AlertDialogCancel>
            <AlertDialogAction onClick={(e) => { e.preventDefault(); confirmDelete(); }} disabled={deleting} className="bg-red-600 hover:bg-red-700">
              {deleting ? <Loader2 className="h-4 w-4 animate-spin" /> : t.automation.delete}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
