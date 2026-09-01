import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { base44 } from '@/api/base44Client';
import { useLanguage } from '@/lib/LanguageProvider';
import { useTranslate } from '@/lib/useTranslate';
import { toast } from '@/components/ui/use-toast';
import PageHeader from '@/components/PageHeader';
import { Play, Pause, Loader2, Clock, Calendar, ChevronRight, Pencil, FileText, RefreshCw } from 'lucide-react';
import ExecutionLogsList from '@/components/automation/ExecutionLogsList';
import { Input } from '@/components/ui/input';
import { authFetch } from '@/api/authFetch';
import { runAutomationTask } from '@/lib/runAutomationTask';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import SkillsSection from '@/components/agent/SkillsSection';
import NotificationSettingsCard from '@/components/automation/NotificationSettingsCard';
import { coerceStringArray } from '@/lib/jsonArray';

import { formatShortDateTime } from '@/lib/time';

// "Run now" hands off to the shared runAutomationTask helper (see
// lib/runAutomationTask.js) — ensure-session -> POST /run -> navigate
// to /chat?session=<sid>. The dispatcher's _notify_chat streams the
// result into the task's owning session; no LLM-mediated handoff.

// Curated set of common IANA timezones for the selector. The cron schedule is
// interpreted in the chosen tz (P0-6: "daily 08:00" fires at 08:00 user-local,
// not 08:00 UTC). next_run_at is stored/returned as UTC and rendered in the
// browser locale, so this only controls how the cron is interpreted.
const COMMON_TIMEZONES = [
  'UTC',
  'Asia/Shanghai',
  'Asia/Hong_Kong',
  'Asia/Tokyo',
  'Asia/Singapore',
  'Asia/Kolkata',
  'Asia/Dubai',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Europe/Moscow',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Sao_Paulo',
  'Australia/Sydney',
  'Pacific/Auckland',
];

export default function AutomationTaskDetail() {
  const { id } = useParams();
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const [task, setTask] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({ name: '', type: '', description: '', schedule: '', project: 'global', output_format: 'html', timezone: 'UTC' });
  const [saving, setSaving] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [running, setRunning] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [projects, setProjects] = useState([]);

  useEffect(() => {
    setEditing(false);
    load();
    base44.entities.Project.filter({ status: 'active' }).then(setProjects).catch(() => setProjects([]));
  }, [id]);

  const load = useCallback(async () => {
    try { setTask(await base44.entities.AutomationTask.get(id)); }
    catch { setTask(null); }
    finally { setLoading(false); }
  }, [id]);

  // Manus-style toggle: actually switches the backend dispatcher on/off
  async function toggle() {
    setToggling(true);
    try {
      const next = task.status === 'active' ? 'paused' : 'active';
      const res = await authFetch(`/api/automations/${id}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: next }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      toast({
        title: next === 'paused' ? t.automation.pause : t.automation.start,
        description: data.next_run_at
          ? `${lang === 'en' ? 'Next run:' : '下次运行:'} ${formatShortDateTime(data.next_run_at)}`
          : (t.detail.saved || 'OK'),
      });
      await load();
      setRefreshKey((k) => k + 1);
    } catch (e) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' });
    } finally { setToggling(false); }
  }

  // "Run Now" — adopt a dedicated chat session for this task
  // (Manus UX: one task = one chat), then hand the run off to the
  // chat agent by injecting a structured "Run Automation Task: ..."
  // prompt into the chatbox AND auto-sending it. The user sees:
  //   1. their own "input" (the prompt) as a message bubble,
  //   2. the agent's reply narrating ``execute_automation``,
  //   3. the dispatcher's run result streaming inline.
  // All three live in the same dedicated session so the user has a
  // single timeline of every run (manual + scheduled).
  //
  // See AutomationTasks.jsx for the full rationale on why we hand
  // off to the LLM via prefill+autorun instead of firing the API
  // directly. The "test" the user reported earlier was the
  // *project* name, not the agent — the default ``general_assistant``
  // agent has ``execute_automation`` in its toolset, so the LLM
  // will call it when it sees the structured prompt.
  async function runNow() {
    if (running || !task) return;
    setRunning(true);
    try {
      await runAutomationTask(task, { navigate, toast, lang });
    } finally {
      setRunning(false);
    }
  }

  function startEdit() {
    setEditForm({
      name: task.name || '',
      type: task.type || '',
      description: task.description || '',
      schedule: task.schedule || '',
      project: task.project || 'global',
      output_format: task.output_format || 'html',
      timezone: task.timezone || 'UTC',
    });
    setEditing(true);
  }

  async function saveEdit() {
    setSaving(true);
    try {
      // Use the new parse-schedule endpoint to validate before saving.
      if (editForm.schedule && editForm.schedule.trim() && editForm.schedule.trim().toLowerCase() !== 'manual') {
        const res = await authFetch('/api/automations/parse-schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ schedule: editForm.schedule, timezone: editForm.timezone }),
        });
        if (res.ok) {
          const data = await res.json();
          if (data.success && data.next_run_at) {
            toast({
              title: lang === 'en' ? 'Schedule parsed' : '计划已解析',
              description: `${lang === 'en' ? 'Next run:' : '下次运行:'} ${formatShortDateTime(data.next_run_at)} (${data.description})`,
            });
          }
        }
      }
      await base44.entities.AutomationTask.update(id, editForm);
      toast({ title: t.detail.saveBasicInfo, description: t.detail.saved || 'OK' });
      setEditing(false);
      await load();
      setRefreshKey((k) => k + 1);
    } catch (e) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' });
    } finally { setSaving(false); }
  }

  const translate = useTranslate([task?.name, task?.description, task?.schedule, task?.last_result].filter(Boolean), lang);

  if (loading) return <div className="flex h-full items-center justify-center"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>;
  if (!task) return <div className="px-8 py-8"><PageHeader title={t.detail.notFound} /></div>;

  // Map our internal statuses onto the UI's existing badge styles.
  const statusStyle = (s) => ({
    active: 'bg-green-100 text-green-700',
    running: 'bg-green-100 text-green-700',
    paused: 'bg-amber-100 text-amber-700',
    completed: 'bg-blue-100 text-blue-700',
    failed: 'bg-red-100 text-red-700',
  }[s] || 'bg-secondary text-muted-foreground');

  const flowLabels = { pending: t.detail.flowPending, running: t.detail.flowRunning, completed: t.detail.flowCompleted, failed: t.detail.flowFailed };
  const flowSteps = ['pending', 'running', task.status === 'failed' ? 'failed' : 'completed'];
  const currentIdx = task.status === 'failed' ? 2 : task.status === 'completed' ? 2 : (task.status === 'active' || task.status === 'running') ? 1 : 0;

  const nextRunLabel = task.next_run_at
    ? formatShortDateTime(task.next_run_at)
    : (task.cron_expression ? (lang === 'en' ? 'computing…' : '计算中…') : t.automation.manual);

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <PageHeader
        title={translate(task.name)}
        subtitle={t.automation.types[task.type] || task.type}
        action={
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={runNow}
              disabled={running}
              className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50 hover:opacity-90"
              title={lang === 'en' ? 'Trigger an immediate run' : '立即触发一次执行'}
            >
              {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {running ? (lang === 'en' ? 'Starting…' : '启动中…') : (lang === 'en' ? 'Run Now' : '立即运行')}
            </button>
            <button
              type="button"
              onClick={toggle}
              disabled={toggling}
              className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-2 text-sm font-medium text-foreground disabled:opacity-50 hover:bg-secondary"
            >
              {toggling ? <Loader2 className="h-4 w-4 animate-spin" /> : task.status === 'active' ? <><Pause className="h-4 w-4" /> {lang === 'en' ? 'Pause' : '暂停'}</> : <><Play className="h-4 w-4" /> {lang === 'en' ? 'Resume' : '继续'}</>}
            </button>
          </div>
        }
      />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <TaskSkillsSection task={task} t={t} lang={lang} onSaved={load} />
          <Section title={t.detail.basicInfo} action={
            !editing && (
              <button type="button" onClick={startEdit} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary">
                <Pencil className="h-3 w-3" /> {t.detail.editBasicInfo}
              </button>
            )
          }>
            {editing ? (
              <div className="space-y-3">
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground">{t.createDialog.name}</label>
                  <Input value={editForm.name} onChange={(e) => setEditForm({ ...editForm, name: e.target.value })} />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground">{t.createDialog.type}</label>
                  <Select value={editForm.type} onValueChange={(v) => setEditForm({ ...editForm, type: v })}>
                    <SelectTrigger><SelectValue placeholder={t.createDialog.typePh} /></SelectTrigger>
                    <SelectContent>
                      {Object.entries(t.automation.types).map(([val, label]) => (
                        <SelectItem key={val} value={val}>{label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground">
                    {t.createDialog.schedule}
                    <span className="ml-1 text-[10px] text-muted-foreground/70">
                      ({lang === 'en' ? 'cron or natural language' : 'cron 或自然语言'})
                    </span>
                  </label>
                  <Input value={editForm.schedule} onChange={(e) => setEditForm({ ...editForm, schedule: e.target.value })} placeholder={t.createDialog.schedulePh} />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground">
                    {lang === 'en' ? 'Timezone' : '时区'}
                    <span className="ml-1 text-[10px] text-muted-foreground/70">
                      ({lang === 'en' ? 'schedule interpreted in this tz' : '计划按此时区解释'})
                    </span>
                  </label>
                  <Select value={editForm.timezone} onValueChange={(v) => setEditForm({ ...editForm, timezone: v })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {COMMON_TIMEZONES.map((tz) => (
                        <SelectItem key={tz} value={tz}>{tz}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground">
                    {lang === 'en' ? 'Output Format' : '输出格式'}
                  </label>
                  <Select value={editForm.output_format} onValueChange={(v) => setEditForm({ ...editForm, output_format: v })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="html">HTML Report</SelectItem>
                      <SelectItem value="pptx">PowerPoint (.pptx)</SelectItem>
                      <SelectItem value="docx">Word (.docx)</SelectItem>
                      <SelectItem value="pdf">PDF (.pdf)</SelectItem>
                      <SelectItem value="md">Markdown (.md)</SelectItem>
                      <SelectItem value="json">JSON</SelectItem>
                      <SelectItem value="csv">CSV</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground">{t.createDialog.project}</label>
                  <Select value={editForm.project} onValueChange={(v) => setEditForm({ ...editForm, project: v })}>
                    <SelectTrigger><SelectValue placeholder={t.createDialog.projectPh} /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="global">{t.createDialog.globalProject}</SelectItem>
                      {projects.map((p) => (
                        <SelectItem key={p.id} value={p.name}>{p.name}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <label className="mb-1.5 block text-xs text-muted-foreground">{t.createDialog.description}</label>
                  <Textarea value={editForm.description} onChange={(e) => setEditForm({ ...editForm, description: e.target.value })} placeholder={t.createDialog.descPh} rows={3} className="resize-none" />
                </div>
                <div className="flex gap-2">
                  <Button onClick={saveEdit} disabled={saving || !editForm.name.trim()} size="sm">
                    {saving ? t.common.saving : t.detail.saveBasicInfo}
                  </Button>
                  <Button onClick={() => setEditing(false)} variant="outline" size="sm">{t.detail.cancelEdit}</Button>
                </div>
              </div>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-4">
                  <Field label={t.automation.status}>
                    <span className={`rounded-full px-2 py-0.5 text-xs ${statusStyle(task.status)}`}>
                      {task.status === 'active' ? (lang === 'en' ? 'Active' : '运行中') : (t.automation.statuses?.[task.status] || task.status)}
                    </span>
                  </Field>
                  <Field label={lang === 'en' ? 'Backend engine' : '后端引擎'}>
                    <span className="inline-flex items-center gap-1.5 text-sm text-green-600 dark:text-green-400">
                      <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
                      {lang === 'en' ? 'Connected' : '已连接'}
                    </span>
                  </Field>
                  <Field label={t.detail.triggerCondition}>
                    <span className="inline-flex items-center gap-1 text-sm">
                      <Calendar className="h-3.5 w-3.5 text-muted-foreground" />
                      {task.schedule ? translate(task.schedule) : t.automation.manual}
                    </span>
                  </Field>
                  <Field label={lang === 'en' ? 'Next run' : '下次运行'}>
                    <span className="inline-flex items-center gap-1 text-sm">
                      <Clock className="h-3.5 w-3.5 text-muted-foreground" /> {nextRunLabel}
                    </span>
                  </Field>
                  <Field label={lang === 'en' ? 'Output format' : '输出格式'}>
                    <span className="inline-flex items-center gap-1 text-sm">
                      <FileText className="h-3.5 w-3.5 text-muted-foreground" /> {(task.output_format || 'html').toUpperCase()}
                    </span>
                  </Field>
                  <Field label={t.automation.lastRun}>
                    <span className="inline-flex items-center gap-1 text-sm">
                      <Clock className="h-3.5 w-3.5 text-muted-foreground" /> {formatShortDateTime(task.last_run)}
                    </span>
                  </Field>
                  <Field label={t.detail.createdDate}><span className="text-sm">{formatShortDateTime(task.created_date)}</span></Field>
                </div>
                {task.cron_expression && (
                  <div className="mt-4 border-t border-border pt-3 text-xs text-muted-foreground">
                    <span className="font-mono">{task.cron_expression}</span>
                    <span className="ml-2">{lang === 'en' ? '(parsed schedule)' : '（已解析的计划）'}</span>
                  </div>
                )}
                <div className="mt-4 border-t border-border pt-4">
                  <div className="mb-1.5 text-xs text-muted-foreground">{t.createDialog.project}</div>
                  <p className="text-sm text-foreground">
                    {task.project && task.project !== 'global' ? task.project : t.automation.ungrouped}
                  </p>
                </div>
                {task.description && (
                  <div className="mt-4 border-t border-border pt-4">
                    <p className="text-sm text-foreground">{translate(task.description)}</p>
                  </div>
                )}
              </>
            )}
          </Section>

          <Section title={t.detail.executionStatus}>
            <div className="flex items-center gap-2">
              {flowSteps.map((step, i) => (
                <div key={step} className="flex flex-1 items-center gap-2">
                  <div className={`flex flex-1 items-center gap-2 rounded-lg border px-3 py-2.5 ${i <= currentIdx ? 'border-primary/30 bg-primary/5' : 'border-border'}`}>
                    <span className={`flex h-6 w-6 items-center justify-center rounded-full text-xs ${i <= currentIdx ? 'bg-primary text-primary-foreground' : 'bg-secondary text-muted-foreground'}`}>{i + 1}</span>
                    <span className={`text-sm ${i <= currentIdx ? 'text-foreground' : 'text-muted-foreground'}`}>{flowLabels[step]}</span>
                  </div>
                  {i < flowSteps.length - 1 && <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />}
                </div>
              ))}
            </div>
          </Section>

          <Section
            title={t.detail.nodeLogs}
            action={
              <button
                type="button"
                onClick={() => setRefreshKey((k) => k + 1)}
                className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"
              >
                <RefreshCw className="h-3 w-3" /> {lang === 'en' ? 'Refresh' : '刷新'}
              </button>
            }
          >
            <ExecutionLogsList task={task} noLogsText={t.detail.noLogs} refreshKey={refreshKey} />
          </Section>
        </div>

        <div className="space-y-6">
          <Section title={t.detail.metadata}>
            <div className="space-y-3 text-sm">
              <Row label={t.detail.createdDate} value={formatShortDateTime(task.created_date)} />
              <Row label={t.detail.updatedDate} value={formatShortDateTime(task.updated_date)} />
              <div className="flex items-center justify-between border-t border-border pt-3">
                <span className="text-muted-foreground">ID</span>
                <span className="font-mono text-xs text-muted-foreground">{task.id.slice(-8)}</span>
              </div>
              {task.cron_expression && (
                <div className="flex items-center justify-between border-t border-border pt-3">
                  <span className="text-muted-foreground">Cron</span>
                  <span className="font-mono text-xs text-foreground">{task.cron_expression}</span>
                </div>
              )}
            </div>
          </Section>

          <NotificationSettingsCard task={task} onSaved={load} />
        </div>
      </div>
    </div>
  );
}

function Section({ title, action, children }) {
  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="font-display text-base text-foreground">{title}</h3>
        {action}
      </div>
      {children}
    </div>
  );
}

// Shows the skills attached to this automation task as chips, with an inline
// "edit skills" flow reusing the shared SkillsSection picker. Persists via
// PATCH AutomationTask.update (the generic CRUD maps `skills` to the new
// automation_tasks.skills column).
function TaskSkillsSection({ task, t, lang, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [skills, setSkills] = useState(() => coerceStringArray(task?.skills));
  const [saving, setSaving] = useState(false);

  const list = coerceStringArray(task?.skills);
  const title = t.automation?.skills || (lang === 'en' ? 'Skills' : '技能');

  async function save() {
    setSaving(true);
    try {
      await base44.entities.AutomationTask.update(task.id, { skills });
      toast({ title: t.detail.saveBasicInfo, description: t.detail.saved || 'OK' });
      setEditing(false);
      onSaved?.();
    } catch (e) {
      toast({ title: 'Error', description: e.message, variant: 'destructive' });
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section
      title={title}
      action={
        !editing && (
          <button
            type="button"
            onClick={() => { setSkills(coerceStringArray(task?.skills)); setEditing(true); }}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary"
          >
            <Pencil className="h-3 w-3" /> {t.detail.editBasicInfo}
          </button>
        )
      }
    >
      {editing ? (
        <div className="space-y-3">
          <SkillsSection
            form={{ skills }}
            update={(p) => { if ('skills' in p) setSkills(coerceStringArray(p.skills)); }}
            t={t}
          />
          <div className="flex gap-2">
            <Button onClick={save} disabled={saving} size="sm">
              {saving ? t.common.saving : t.detail.saveBasicInfo}
            </Button>
            <Button
              onClick={() => { setEditing(false); setSkills(coerceStringArray(task?.skills)); }}
              variant="outline"
              size="sm"
            >
              {t.detail.cancelEdit}
            </Button>
          </div>
        </div>
      ) : list.length ? (
        <div className="flex flex-wrap gap-2">
          {list.map((s) => (
            <span key={s} className="rounded-md border border-border bg-secondary/50 px-2 py-0.5 text-xs text-foreground">
              {s}
            </span>
          ))}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          {t.automation?.noSkills || (lang === 'en' ? 'No skills enabled' : '未启用技能')}
        </p>
      )}
    </Section>
  );
}
function Field({ label, children }) {
  return (
    <div>
      <div className="mb-1.5 text-xs text-muted-foreground">{label}</div>
      <div>{children}</div>
    </div>
  );
}
function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground">{value}</span>
    </div>
  );
}
