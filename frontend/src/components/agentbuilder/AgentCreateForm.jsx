import { useState, useEffect } from 'react';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { useLanguage } from '@/lib/LanguageProvider';
import { base44 } from '@/api/base44Client';
import LlmModelSelector from '@/components/chat/LlmModelSelector';
import { Bot, Loader2, ArrowRight } from 'lucide-react';

const TEMPLATES = {
  zh: [
    { name: '生产效率分析Agent', desc: '聚焦产线效率瓶颈分析，输出优化建议与产能达成率报告', capabilities: '数据分析, 报表生成' },
    { name: '设备运维诊断Agent', desc: '基于设备运行数据诊断异常，制定预防性维护计划与衰退预警', capabilities: '异常诊断, 维护计划' },
    { name: '质量管控Agent', desc: '分析不良品分布趋势，生成SPC控制图并输出改进措施', capabilities: '质量分析, SPC图表' },
    { name: '供应链调度Agent', desc: '评估库存与交付风险，输出调度建议与补货计划', capabilities: '风险评估, 调度优化' },
  ],
  en: [
    { name: 'Production Efficiency Agent', desc: 'Analyze line efficiency bottlenecks, output optimization suggestions and capacity achievement reports', capabilities: 'Data Analysis, Reporting' },
    { name: 'Equipment Maintenance Agent', desc: 'Diagnose anomalies from equipment data, build preventive maintenance plans and degradation warnings', capabilities: 'Anomaly Diagnosis, Maintenance Planning' },
    { name: 'Quality Control Agent', desc: 'Analyze defect distribution trends, generate SPC control charts and improvement measures', capabilities: 'Quality Analysis, SPC Charts' },
    { name: 'Supply Chain Scheduling Agent', desc: 'Assess inventory and delivery risks, output scheduling advice and replenishment plans', capabilities: 'Risk Assessment, Scheduling' },
  ],
};

// Sentinel for the "Ungrouped" project — aligned with the backend default.
const UNGROUPED = 'global';

/**
 * Step 1 of the embedded Agent builder.
 *
 * Layout (top → bottom):
 *   1. Step indicator card     — "① 填写信息  →  ② 对话配置"
 *   2. Professional templates  — chips that pre-fill the form below
 *   3. Form fields             — name / capabilities / project / description
 *   4. Build CTA               — primary action
 *
 * v2: the project Select now stores `project.id` (the new FK to projects.id),
 * NOT the legacy name string. When `initialProjectId` is set (e.g. called
 * from Project Detail → "New Agent"), that project is pre-selected.
 */
export default function AgentCreateForm({
  name, onNameChange,
  description, onDescriptionChange,
  capabilities, onCapabilitiesChange,
  project, onProjectChange,
  llmModelId, onLlmModelIdChange,
  initialProjectId,
  onBuild,
  building,
  currentStep = 'form',
}) {
  const { t, lang } = useLanguage();
  const [projects, setProjects] = useState([]);
  const isEn = lang === 'en';

  const templates = TEMPLATES[lang] || [];

  useEffect(() => {
    base44.entities.Project.filter({ status: 'active' })
      .then((projs) => setProjects(projs))
      .catch(() => setProjects([]));
  }, []);

  // Apply initialProjectId exactly once after the project list has loaded.
  // We do NOT clobber a value the parent has already set.
  useEffect(() => {
    if (!initialProjectId) return;
    if (project && project !== UNGROUPED) return;
    onProjectChange(initialProjectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialProjectId, projects.length]);

  function applyTemplate(tpl) {
    onNameChange(tpl.name || '');
    onDescriptionChange(tpl.desc || '');
    if (tpl.capabilities) onCapabilitiesChange(tpl.capabilities);
  }

  const stepForm = currentStep === 'form';
  const stepConfig = currentStep === 'config';

  // Resolve the currently-selected project object so we can show its name
  // in the Select trigger.
  const selectedProject = projects.find((p) => p.id === project);

  return (
    <div className="space-y-5">
      {/* ---- 1. Step indicator card ---- */}
      <div className="rounded-lg border border-border bg-card px-4 py-3 shadow-sm">
        <div className="flex items-center justify-center gap-3 text-sm">
          <span
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1 transition-colors ${
              stepForm ? 'bg-primary/10 text-primary' : 'bg-secondary text-muted-foreground'
            }`}
          >
            <span
              className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-semibold ${
                stepForm ? 'bg-primary text-primary-foreground' : 'bg-muted-foreground/30 text-background'
              }`}
            >
              1
            </span>
            {t.agentBuilder?.steps?.form || '填写信息'}
          </span>
          <ArrowRight className="h-3.5 w-3.5 text-muted-foreground/60" />
          <span
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1 transition-colors ${
              stepConfig ? 'bg-primary/10 text-primary' : 'bg-secondary text-muted-foreground'
            }`}
          >
            <span
              className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-semibold ${
                stepConfig ? 'bg-primary text-primary-foreground' : 'bg-muted-foreground/30 text-background'
              }`}
            >
              2
            </span>
            {t.agentBuilder?.steps?.config || '对话配置'}
          </span>
        </div>
      </div>

      {/* ---- 2. Professional templates ---- */}
      {templates.length > 0 && (
        <div>
          <Label className="mb-2 block text-xs text-muted-foreground">
            {t.createDialog.templatesLabel || (isEn ? 'Professional Templates' : '专业模板')}
          </Label>
          <div className="flex flex-wrap gap-1.5">
            {templates.map((tpl, i) => (
              <button
                key={i}
                type="button"
                onClick={() => applyTemplate(tpl)}
                className="rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
              >
                {tpl.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ---- 3. Form fields ---- */}
      <div className="space-y-4">
        <div>
          <Label className="mb-1.5 block text-xs">{t.createDialog.name}</Label>
          <Input
            value={name}
            onChange={(e) => onNameChange(e.target.value)}
            placeholder={t.createDialog.namePh}
          />
        </div>

        <div>
          <Label className="mb-1.5 block text-xs">{t.createDialog.capabilities}</Label>
          <Input
            value={capabilities}
            onChange={(e) => onCapabilitiesChange(e.target.value)}
            placeholder={t.createDialog.capabilitiesPh}
          />
        </div>

        <div>
          <Label className="mb-1.5 block text-xs">{t.createDialog.project}</Label>
          <Select value={project || UNGROUPED} onValueChange={onProjectChange}>
            <SelectTrigger>
              <SelectValue placeholder={t.createDialog.projectPh}>
                {selectedProject ? selectedProject.name : (t.createDialog.globalProject || 'Ungrouped')}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={UNGROUPED}>{t.createDialog.globalProject || 'Ungrouped'}</SelectItem>
              {projects
                .filter((p) => p.name !== 'Ungrouped' && p.name !== '未分组')
                .map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.name}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
          {initialProjectId && selectedProject && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              {isEn
                ? `This agent will be created inside the "${selectedProject.name}" project and will inherit its data sources.`
                : `该智能体将创建在「${selectedProject.name}」项目内，并自动继承该项目的数据源。`}
            </p>
          )}
        </div>

        <div>
          <LlmModelSelector
            value={llmModelId || null}
            onChange={(id) => onLlmModelIdChange?.(id)}
          />
        </div>

        <div>
          <Label className="mb-1.5 block text-xs">{t.createDialog.description}</Label>
          <Textarea
            value={description}
            onChange={(e) => onDescriptionChange(e.target.value)}
            placeholder={t.createDialog.descPh}
            rows={3}
            className="resize-none"
          />
        </div>
      </div>

      {/* ---- 4. Build CTA ---- */}
      {onBuild && (
        <Button
          onClick={onBuild}
          disabled={building}
          className="w-full gap-1.5"
        >
          {building ? <Loader2 className="h-4 w-4 animate-spin" /> : <Bot className="h-4 w-4" />}
          {t.agentBuilder?.build || '通过 Agent Builder 构建'}
        </Button>
      )}
    </div>
  );
}
