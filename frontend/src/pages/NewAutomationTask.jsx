/**
 * New Automation Task — full-page creation form (2026-11-02).
 *
 * Replaces the modal for the /automation entry point (user request: match
 * the provided mockup — two-column layout with templates + project on the
 * left, Basics / Capabilities cards on the right, sticky footer).
 *
 * Reuses the SAME building blocks as CreateResourceDialog (SchedulePicker,
 * SkillsSection, TEMPLATES, buildAutomationPrefill) so the chat prefill
 * produced here is byte-identical to the dialog's — create_automation
 * parses these exact lines. Submit routes to a brand-new chat session
 * pinned to automation_agent (?newTask=1), exactly like the dialog.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, ListChecks, PenLine, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import SchedulePicker from '@/components/automation/SchedulePicker';
import SkillsSection from '@/components/agent/SkillsSection';
import {
  TEMPLATES, buildAutomationPrefill, buildTemplateString,
} from '@/components/CreateResourceDialog';
import { coerceStringArray } from '@/lib/jsonArray';
import { useLanguage } from '@/lib/LanguageProvider';
import { base44 } from '@/api/base44Client';
import { authFetch } from '@/api/authFetch';

export default function NewAutomationTask() {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const isEn = lang === 'en';

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [schedule, setSchedule] = useState('');
  const [project, setProject] = useState('global');
  const [projects, setProjects] = useState([]);
  const [outputFormat, setOutputFormat] = useState('html');
  const [skills, setSkills] = useState([]);
  const [llmInformedTick, setLlmInformedTick] = useState(false);

  useEffect(() => {
    base44.entities.Project.filter({ status: 'active' })
      .then((projs) => setProjects(projs))
      .catch(() => setProjects([]));
  }, []);

  const templates = TEMPLATES.automation?.[lang] || [];

  function applyTemplate(tpl) {
    setName(tpl.name || '');
    setDescription(tpl.desc || '');
    if (tpl.schedule) setSchedule(buildTemplateString(tpl));
  }

  async function handleSubmit() {
    const prefill = buildAutomationPrefill({
      name, schedule, outputFormat, skills, llmInformedTick,
      project, projectDataSources: [], description, isEn, t,
    });
    const effectiveName = project && project !== 'global' ? project : null;
    const effectiveId = effectiveName
      ? ((projects || []).find((p) => p.name === effectiveName)?.id || null)
      : null;

    // Lifecycle separation (2026-11-03): "Save & Continue in Chat" creates
    // a SERVER-BACKED DRAFT via the Control Plane BEFORE navigating — the
    // pending card in chat then renders from authoritative server state
    // (id + validation) and activation happens only through the user's
    // explicit Confirm & Activate on that card. No task is created here,
    // nothing is scheduled, nothing executes, no artifact is generated.
    let draftId = null;
    try {
      // Best-effort cron resolution so the draft validates immediately;
      // a human schedule string falls back to raw (validation shows the
      // error and the agent refines it in chat).
      let cron = (schedule || '').trim();
      try {
        const parsed = await authFetch('/api/automations/parse-schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ schedule: cron }),
        });
        if (parsed?.ok) {
          const parsedBody = await parsed.json();
          if (parsedBody?.cron) cron = parsedBody.cron;
        }
      } catch { /* fall back to the raw schedule string */ }
      const spec = {
        name: name.trim(),
        project_id: effectiveId,
        objective: description || name.trim(),
        trigger: {
          type: schedule && schedule.toLowerCase() !== 'manual'
            ? 'schedule' : 'manual',
          ...(schedule && schedule.toLowerCase() !== 'manual'
            ? { cron } : {}),
        },
        output: { format: outputFormat },
      };
      const resp = await authFetch('/api/automations/drafts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spec_partial: spec }),
      });
      if (resp?.ok) {
        const draftBody = await resp.json();
        if (draftBody?.draft_id) draftId = draftBody.draft_id;
      }
    } catch { /* the card degrades to prefill-only mode (legacy view) */ }

    const params = new URLSearchParams({ prefill, newTask: '1' });
    if (effectiveName) params.set('projectName', effectiveName);
    if (effectiveId) params.set('projectId', effectiveId);
    if (draftId) params.set('draftId', draftId);
    // Pin the automation creation flow to automation_agent (same rationale
    // as CreateResourceDialog: the project agent may lack create_automation).
    params.set('agentName', 'automation_agent');
    navigate(`/?${params.toString()}`);
  }

  const canSubmit = name.trim().length > 0;
  const OUTPUT_FORMAT_OPTIONS = [
    { value: 'html', label: isEn ? 'HTML report' : 'HTML 报告' },
    { value: 'docx', label: isEn ? 'Word document' : 'Word 文档' },
    { value: 'pptx', label: isEn ? 'PowerPoint deck' : 'PPT 演示' },
    { value: 'pdf', label: isEn ? 'PDF document' : 'PDF 文档' },
    { value: 'md', label: isEn ? 'Markdown' : 'Markdown' },
    { value: 'xlsx', label: isEn ? 'Excel workbook' : 'Excel 表格' },
    { value: 'csv', label: 'CSV' },
    { value: 'json', label: 'JSON' },
  ];

  return (
    /* AppLayout's <main> is h-full overflow-hidden — the PAGE is the scroll
       container (same contract as AutomationTasks' h-full overflow-y-auto).
       min-h-screen here overflowed the clipped parent: content below the
       fold was unreachable (scroll broken, 2026-11-02). */
    <div className="h-full overflow-y-auto bg-muted/30">
      <div className="mx-auto flex min-h-full max-w-6xl flex-col px-6 pb-6 pt-8">
        {/* ── Page header ── */}
        <header className="mb-8 border-b border-border pb-6">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => navigate('/automation')}
              aria-label={isEn ? 'Back' : '返回'}
              className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-foreground transition-colors hover:bg-secondary"
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
            <h1 className="text-2xl font-semibold tracking-tight text-foreground">
              {isEn ? 'New Automation Task' : '新建自动化任务'}
            </h1>
          </div>
          <p className="mt-2 text-sm text-muted-foreground">
            {isEn
              ? 'Fill in basic info, then continue in chat to complete creation and configuration.'
              : '填写基本信息，然后在对话中继续完成创建与配置。'}
          </p>
        </header>

        {/* ── Two-column body ── */}
        <div className="grid flex-1 grid-cols-1 gap-8 lg:grid-cols-[280px_1fr]">
          {/* Left: templates + project */}
          <aside className="space-y-6">
            <div>
              <Label className="mb-2.5 block text-xs font-medium text-foreground/80">
                {isEn ? 'Professional Templates' : '专业模板'}
              </Label>
              <div className="flex flex-wrap gap-2">
                {templates.map((tpl, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => applyTemplate(tpl)}
                    title={tpl.desc}
                    className="rounded-full border border-border bg-card px-3.5 py-2 text-xs text-muted-foreground transition-colors hover:border-foreground/25 hover:text-foreground"
                  >
                    {tpl.name}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <Label className="mb-2 block text-sm font-medium text-foreground">
                {t.createDialog.project}
              </Label>
              <Select value={project} onValueChange={setProject}>
                <SelectTrigger className="h-11 bg-card" data-testid="automation-project-select">
                  <SelectValue placeholder={t.createDialog.projectPh} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="global">{isEn ? 'Ungrouped' : '未分组'}</SelectItem>
                  {projects.filter((p) => p.name !== 'Ungrouped' && p.name !== '未分组').map((p) => (
                    <SelectItem key={p.id} value={p.name}>{p.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </aside>

          {/* Right: Basics + Capabilities cards */}
          <div className="min-w-0 space-y-6">
            {/* Basics */}
            <section className="rounded-xl border border-border bg-card p-5" data-testid="automation-basics-card">
              <div className="mb-4 flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-md bg-secondary text-muted-foreground">
                  <PenLine className="h-3.5 w-3.5" />
                </span>
                <h2 className="text-sm font-semibold text-foreground">
                  {isEn ? 'Basics' : '基本信息'}
                </h2>
              </div>

              <div className="space-y-5">
                <div>
                  <Label className="mb-1.5 block text-xs">
                    {t.createDialog.name} <span className="text-red-500">*</span>
                  </Label>
                  <Input
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder={isEn ? 'Enter a name' : '输入名称'}
                    data-testid="automation-name-input"
                  />
                </div>

                <div>
                  <Label className="mb-1.5 block text-xs">{t.createDialog.schedule}</Label>
                  <SchedulePicker value={schedule} onChange={setSchedule} />
                </div>

                <div>
                  <Label className="mb-1.5 block text-xs">{isEn ? 'Output format' : '输出格式'}</Label>
                  <Select value={outputFormat} onValueChange={setOutputFormat}>
                    <SelectTrigger className="h-11" data-testid="output-format-select">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {OUTPUT_FORMAT_OPTIONS.map((o) => (
                        <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </section>

            {/* Capabilities */}
            <section className="rounded-xl border border-border bg-card p-5" data-testid="automation-capabilities-card">
              <div className="mb-4 flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-md bg-secondary text-muted-foreground">
                  <ListChecks className="h-3.5 w-3.5" />
                </span>
                <h2 className="text-sm font-semibold text-foreground">
                  {isEn ? 'Capabilities' : '能力配置'}
                </h2>
              </div>

              <div className="space-y-4">
                <div
                  data-testid="automation-skills-section"
                  className="rounded-lg border border-border p-4"
                >
                  <SkillsSection
                    form={{ skills }}
                    update={(patch) => { if ('skills' in patch) setSkills(coerceStringArray(patch.skills)); }}
                    t={t}
                  />
                </div>

                <label
                  data-testid="automation-llm-tick"
                  className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-border p-4"
                >
                  <input
                    type="checkbox"
                    checked={llmInformedTick}
                    onChange={(e) => setLlmInformedTick(e.target.checked)}
                    className="mt-0.5 h-3.5 w-3.5 rounded border-border accent-primary"
                  />
                  <span className="min-w-0">
                    <span className="block text-xs font-medium text-foreground">
                      {isEn ? 'LLM-informed tick' : '智能调度'}
                    </span>
                    <span className="block text-[11px] leading-snug text-muted-foreground">
                      {isEn
                        ? 'Before each scheduled run, the agent gets a brief AI briefing on what changed since the last run and what to focus on. Slightly slower ticks, smarter context.'
                        : '每次定时执行前，智能体会收到一份简报：上次运行后发生了什么变化、本次应重点关注什么。执行稍慢，但上下文更聪明。'}
                    </span>
                  </span>
                </label>

                {/* Description — not in the mockup but kept: it feeds the
                    chat prefill ("continue in chat to complete"). */}
                <div>
                  <Label className="mb-1.5 block text-xs">{t.createDialog.description}</Label>
                  <Textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder={t.createDialog.descPh}
                    rows={3}
                    className="resize-none"
                  />
                </div>
              </div>
            </section>
          </div>
        </div>

        {/* ── Sticky footer ── */}
        <footer className="sticky bottom-0 mt-8 border-t border-border bg-background/95 py-4 backdrop-blur">
          <div className="flex items-center justify-end gap-3">
            <Button
              variant="outline"
              onClick={() => navigate('/automation')}
              data-testid="automation-cancel"
            >
              {t.createDialog.cancel}
            </Button>
            <Button
              onClick={handleSubmit}
              disabled={!canSubmit}
              data-testid="automation-save-continue"
            >
              <Sparkles className="h-4 w-4" />
              {isEn ? 'Save & Continue in Chat' : '保存并在对话中继续'}
              <ArrowRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </footer>
      </div>
    </div>
  );
}
