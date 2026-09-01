import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import SchedulePicker from '@/components/automation/SchedulePicker';
import SkillsSection from '@/components/agent/SkillsSection';
import { coerceStringArray } from '@/lib/jsonArray';
import {
  Sparkles, ArrowRight, FolderOpen, Database, FileText, Info, Lock, AlertCircle,
} from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { base44 } from '@/api/base44Client';

const TEMPLATES = {
  automation: {
    zh: [
      { type: 'data_sync', name: '每日销售数据同步', desc: '将ERP系统销售数据每日定时同步至业务数据库，支持增量更新与异常告警', schedule: 'daily', scheduleTime: '08:00' },
      { type: 'report_generation', name: '财务月报自动生成', desc: '按月汇总财务收支数据，生成标准化财务月报并推送至相关负责人', schedule: 'monthly', scheduleTime: '09:00', scheduleDayOfMonth: 1 },
      { type: 'approval_flow', name: '采购审批自动化', desc: '自动流转采购申请审批流程，超时提醒并记录审批轨迹', schedule: 'weekly', scheduleTime: '09:00', scheduleDays: [0, 1, 2, 3, 4] },
      { type: 'agent_inspection', name: '库存异常巡检', desc: '定时巡检库存数据，发现异常自动告警并生成诊断报告', schedule: 'daily', scheduleTime: '16:00' },
      { type: 'data_cleaning', name: '财务流水清洗', desc: '清洗并标准化原始财务流水，去重去噪并校验金额一致性', schedule: 'daily', scheduleTime: '23:00' },
    ],
    en: [
      { type: 'data_sync', name: 'Daily Sales Data Sync', desc: 'Sync ERP sales data to the business database daily with incremental updates and anomaly alerts', schedule: 'daily', scheduleTime: '08:00' },
      { type: 'report_generation', name: 'Monthly Finance Report', desc: 'Aggregate monthly financial data, generate standardized reports and push to stakeholders', schedule: 'monthly', scheduleTime: '09:00', scheduleDayOfMonth: 1 },
      { type: 'approval_flow', name: 'Purchase Approval Flow', desc: 'Automate purchase request approval routing with timeout reminders and audit trails', schedule: 'weekly', scheduleTime: '09:00', scheduleDays: [0, 1, 2, 3, 4] },
      { type: 'agent_inspection', name: 'Inventory Anomaly Inspection', desc: 'Inspect inventory data on schedule, auto-alert on anomalies and generate diagnostic reports', schedule: 'daily', scheduleTime: '16:00' },
      { type: 'data_cleaning', name: 'Financial Transaction Cleaning', desc: 'Clean and standardize raw financial transactions, deduplicate and validate amount consistency', schedule: 'daily', scheduleTime: '23:00' },
    ],
  },
  agent: {
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
  },
  kb: {
    zh: [
      { type: 'vector_db', name: '产品技术知识库', desc: '存储产品手册、技术文档的向量索引，支持语义检索与智能问答' },
      { type: 'business_db', name: '生产业务数据库', desc: '结构化存储产线、设备、工单、物料等业务主数据' },
      { type: 'memory_file', name: '会话记忆文件', desc: '持久化存储对话上下文与用户偏好，支持长期记忆与个性化' },
    ],
    en: [
      { type: 'vector_db', name: 'Product Knowledge Base', desc: 'Vector index of product manuals and technical docs, supporting semantic search and Q&A' },
      { type: 'business_db', name: 'Production Business DB', desc: 'Structured storage for production lines, equipment, work orders and materials' },
      { type: 'memory_file', name: 'Conversation Memory', desc: 'Persist conversation context and user preferences for long-term memory and personalization' },
    ],
  },
  flow: {
    zh: [
      { name: '异常处理决策流', desc: '设备异常→自动诊断→风险分级→派单→跟踪闭环的自动化决策编排' },
      { name: '质量追溯决策流', desc: '不良品→批次定位→根因分析→改进措施的全链路追溯编排' },
    ],
    en: [
      { name: 'Anomaly Handling Flow', desc: 'Equipment anomaly, auto diagnosis, risk grading, dispatch, tracking closed-loop orchestration' },
      { name: 'Quality Traceability Flow', desc: 'Defect, batch location, root cause analysis, improvement measures full-chain orchestration' },
    ],
  },
  report: {
    zh: [
      { name: '月度运营报告', desc: '汇总生产、质量、设备、能耗等核心运营指标，输出月度复盘分析' },
      { name: '设备OEE周报', desc: '按周统计设备综合效率（OEE）及衰退趋势分析' },
    ],
    en: [
      { name: 'Monthly Operations Report', desc: 'Aggregate core operational metrics across production, quality, equipment and energy for monthly review' },
      { name: 'Equipment OEE Weekly', desc: 'Weekly statistics on equipment overall effectiveness (OEE) and degradation trends' },
    ],
  },
  // Full-stack realtime dashboards. These are APP-BUILD requests — the
  // prefill carries an explicit intent chip so the agent routes to the
  // dashboard-generation skill (create_fullstack_dashboard + WebSocket live
  // data), never the static-HTML fallback.
  dashboard: {
    zh: [
      { name: '销售业绩看板', desc: '实时展示销售额、订单量、区域分布与TOP产品趋势，数据来源于已绑定的业务数据库' },
      { name: '生产运营看板', desc: '实时监控产量、良率、设备状态与产能达成率等核心运营指标' },
      { name: '设备OEE监控看板', desc: '实时追踪设备综合效率（OEE）、停机时间与衰退趋势' },
      { name: '财务总览看板', desc: '实时汇总收入、支出、毛利与现金流等关键财务指标' },
    ],
    en: [
      { name: 'Sales Performance Dashboard', desc: 'Live revenue, order volume, regional split and top-product trends from the bound business database' },
      { name: 'Operations KPI Board', desc: 'Live production volume, yield rate, equipment status and capacity achievement KPIs' },
      { name: 'Equipment OEE Monitor', desc: 'Live equipment overall effectiveness (OEE), downtime and degradation trends' },
      { name: 'Financial Overview', desc: 'Live revenue, expenses, gross margin and cash-flow metrics' },
    ],
  },
  file: { zh: [], en: [] },
};

function getTypeOptions(resourceType, t) {
  if (resourceType === 'automation') return t.automation.types;
  if (resourceType === 'kb') return t.detail.kbTypes;
  return null;
}

export default function CreateResourceDialog({ open, onOpenChange, resourceType, onAgentBuild, defaultProjectName, defaultProjectId }) {
  const { t, lang } = useLanguage();
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [type, setType] = useState('');
  const [schedule, setSchedule] = useState('');
  const [capabilities, setCapabilities] = useState('');
  const [project, setProject] = useState('global');
  const [projects, setProjects] = useState([]);
  // Automation deliverable format — emitted into the prefill so the
  // create_automation tool persists it as task.output_format.
  const [outputFormat, setOutputFormat] = useState('html');
  // Skills enabled for the automation — persisted to automation_tasks.skills
  // via the chat prefill (create_automation) as an ordered string array.
  const [skills, setSkills] = useState([]);
  // LLM-informed tick (opt-in): when enabled, each scheduled run gets a
  // per-tick LLM briefing injected into the agent prompt. Emitted into
  // the prefill so create_automation persists it as llm_informed_tick.
  const [llmInformedTick, setLlmInformedTick] = useState(false);
  // Dashboard refresh cadence (seconds) — emitted into the prefill so the
  // fullstack dashboard is built with the chosen refresh_interval_seconds.
  const [refreshInterval, setRefreshInterval] = useState(30);
  // Project data sources — fetched when a project is pre-selected so the
  // user can see what the Agent will inherit. Empty when no project.
  const [projectDataSources, setProjectDataSources] = useState([]);

  useEffect(() => {
    if (!open) return;
    setName(''); setDescription(''); setType(''); setSchedule(''); setCapabilities('');
    setProject(defaultProjectName || 'global');
    setProjectDataSources([]);
    setOutputFormat('html');
    setSkills([]);
    setLlmInformedTick(false);
    setRefreshInterval(30);
    base44.entities.Project.filter({ status: 'active' })
      .then((projs) => setProjects(projs))
      .catch(() => setProjects([]));
  }, [open, resourceType, defaultProjectName]);

  // Fetch the project's bound data sources whenever a project is
  // pre-selected. This is the "what does the agent inherit?" surface.
  useEffect(() => {
    if (!open || !defaultProjectName) return;
    let cancelled = false;
    (async () => {
      try {
        // Prefer the FK; fall back to legacy name field for backends
        // that only know the string.
        let projs = [];
        try {
          projs = defaultProjectId
            ? await base44.entities.Project.filter({ id: defaultProjectId })
            : await base44.entities.Project.filter({ name: defaultProjectName });
        } catch { projs = []; }
        const proj = Array.isArray(projs) ? projs.find((p) => p.id === defaultProjectId) || projs[0] : null;
        const pid = proj?.id;
        if (!pid) return;
        const rows = await base44.entities.KnowledgeBase
          .filter({ project_id: pid }, '-updated_date', 50)
          .catch(() => []);
        if (!cancelled) setProjectDataSources(Array.isArray(rows) ? rows : []);
      } catch { if (!cancelled) setProjectDataSources([]); }
    })();
    return () => { cancelled = true; };
  }, [open, defaultProjectName, defaultProjectId]);

  const templates = TEMPLATES[resourceType]?.[lang] || [];
  const typeOptions = getTypeOptions(resourceType, t);
  const isEn = lang === 'en';
  const resourceLabel = t.createDialog.resourceLabels[resourceType];

  // For the automation case, only the daily/weekly/monthly/etc.
  // templates map to a structured schedule. Custom-string templates
  // are still applied verbatim.
  function applyTemplate(tpl) {
    setName(tpl.name || '');
    setDescription(tpl.desc || '');
    if (tpl.type) setType(tpl.type);
    if (tpl.capabilities) setCapabilities(tpl.capabilities);
    if (resourceType === 'automation') {
      if (tpl.schedule) {
        // Defer the schedule write to the picker by emitting a one-shot
        // synthetic value through the schedule state. The picker will
        // parse the legacy text and switch to the matching mode.
        setSchedule(buildTemplateString(tpl));
      }
    } else if (tpl.schedule) {
      setSchedule(tpl.schedule);
    }
  }

  function getTypeLabel(typeVal) {
    if (!typeVal || !typeOptions) return '';
    return typeOptions[typeVal] || typeVal;
  }

  function buildPrefill() {
    const lines = [];
    if (resourceType === 'dashboard') {
      // Intent chip — the literal "dashboard" keyword deterministically
      // routes the resolver to the dashboard-generation skill (format_intent
      // / soft_intent), which enforces: design-system-first, data contract,
      // create_fullstack_dashboard, WebSocket live updates. Never static HTML.
      lines.push(isEn
        ? 'Build a FULL-STACK REALTIME DASHBOARD (use create_fullstack_dashboard):'
        : '请构建一个全栈实时仪表盘 dashboard（使用 create_fullstack_dashboard）：');
      lines.push(`- ${isEn ? 'Mode' : '模式'}：${isEn
        ? 'FULLSTACK_REALTIME — design-system-first (uiux_design_system), real data from the bound datasource, WebSocket live updates'
        : '全栈实时——设计系统优先（uiux_design_system），基于已绑定数据源的真实数据，WebSocket 实时刷新'}`);
      lines.push(`- ${isEn ? 'Refresh interval' : '刷新间隔'}：${refreshInterval}s`);
    } else {
      lines.push(isEn ? `Create a new ${resourceLabel}:` : `帮我新建一个${resourceLabel}：`);
    }
    if (name) lines.push(`- ${isEn ? 'Name' : '名称'}：${name}`);
    if (type) lines.push(`- ${isEn ? 'Type' : '类型'}：${getTypeLabel(type)}`);
    if (schedule) lines.push(`- ${isEn ? 'Schedule' : '调度规则'}：${schedule}`);
    if (resourceType === 'automation') {
      // The raw value in parentheses lets create_automation's
      // _detect_output_format resolve it without parsing the label.
      const opt = OUTPUT_FORMAT_OPTIONS.find((o) => o.value === outputFormat) || OUTPUT_FORMAT_OPTIONS[0];
      lines.push(`- ${isEn ? 'Output format' : '输出格式'}：${opt.label} (${opt.value})`);
      // Skills chosen in the picker → emitted so create_automation persists
      // them as automation_tasks.skills (progressive-disclosure at runtime).
      if (skills.length) lines.push(`- ${isEn ? 'Skills' : '技能'}：${skills.join(', ')}`);
      // LLM-informed tick → emitted so create_automation persists it as
      // automation_tasks.llm_informed_tick. Explicit keyword makes the
      // LLM pass llm_informed_tick=true in the tool-call args.
      if (llmInformedTick) lines.push(`- ${isEn ? 'LLM-informed tick' : '智能调度'}: yes`);
    }
    if (capabilities) lines.push(`- ${isEn ? 'Tools' : '工具'}：${capabilities}`);
    if (resourceType === 'agent' || resourceType === 'automation' || resourceType === 'kb' || resourceType === 'dashboard') {
      const projName = project === 'global' ? (isEn ? 'Ungrouped' : '未分组') : project;
      lines.push(`- ${isEn ? 'Project' : '所属项目'}：${projName}`);
      if (projectDataSources.length > 0) {
        // The chat agent is smart enough to look up the data source type
        // by name, so the prefill just lists the names. This also avoids
        // duplicating type labels when the name already contains them
        // (e.g. "Demo EMEA Sales (sqlite)").
        const summary = projectDataSources
          .slice(0, 6)
          .map((kb) => kb.name)
          .join(isEn ? ', ' : '、');
        const extra = projectDataSources.length > 6
          ? `${isEn ? ' and ' : '，'}${projectDataSources.length - 6}${t.createDialog.dataSourcesMore}`
          : '';
        lines.push(`- ${isEn ? 'Available data sources' : '可用数据源'}：${summary}${extra}`);
      }
    }
    if (description) lines.push(`- ${isEn ? 'Description' : '描述'}：${description}`);
    return lines.join('\n');
  }

  function handleSubmit() {
    if (resourceType === 'agent') {
      const lines = [isEn ? `Please build me an agent based on the following info (save directly as an AgentApp):` : `请基于以下信息为我构建一个智能体（直接保存为 AgentApp）：`];
      if (name) lines.push(`- ${isEn ? 'Name' : '名称'}：${name}`);
      if (capabilities) lines.push(`- ${isEn ? 'Tools' : '工具'}：${capabilities}`);
      lines.push(`- ${isEn ? 'Project' : '所属项目'}：${project === 'global' ? (isEn ? 'Ungrouped' : '未分组') : project}`);
      if (description) lines.push(`- ${isEn ? 'Description' : '描述'}：${description}`);
      onOpenChange(false);
      if (typeof onAgentBuild === 'function') {
        onAgentBuild(lines.join('\n'));
      } else {
        navigate(`/agent-builder?prefill=${encodeURIComponent(lines.join('\n'))}`);
      }
      return;
    }
    const prefill = buildPrefill();
    onOpenChange(false);
    // For the structured-creation flow we ALWAYS route to a brand-new
    // chat session — every automation/task should get its own clean
    // conversation (Manus-style). The `?newTask=1` flag tells Chat.jsx
    // to create a fresh session rather than appending to the currently-
    // active one.
    //
    // We pass the *effective* project (the locked-in project when the
    // dialog was opened from a project page, or whatever the user just
    // selected from the dropdown when opened from a no-project entry
    // point like the global Automation Tasks page) so the new session
    // is always tagged with the right project — not Ungrouped.
    const effectiveName = projectLocked ? defaultProjectName : (project && project !== 'global' ? project : null);
    const effectiveId = projectLocked ? defaultProjectId : (() => {
      // Look up the project id for the dropdown-selected project so the
      // new ChatSession rows can FK to the project (Recent Chats tab,
      // project-scoped queries, etc.). Best-effort — falls back to
      // name-only tagging if the lookup fails.
      if (!effectiveName) return null;
      const match = (projects || []).find((p) => p.name === effectiveName);
      return match?.id || null;
    })();
    const params = new URLSearchParams({ prefill, newTask: '1' });
    if (effectiveName) params.set('projectName', effectiveName);
    if (effectiveId) params.set('projectId', effectiveId);
    navigate(`/?${params.toString()}`);
  }

  const canSubmit = name.trim().length > 0;

  const OUTPUT_FORMAT_OPTIONS = [
    { value: 'html', label: 'HTML report' },
    { value: 'docx', label: 'Word document' },
    { value: 'pptx', label: 'PowerPoint deck' },
    { value: 'pdf', label: 'PDF document' },
    { value: 'md', label: 'Markdown' },
    { value: 'xlsx', label: 'Excel workbook' },
    { value: 'csv', label: 'CSV' },
    { value: 'json', label: 'JSON' },
  ];

  // The locked-project path is the new default when the dialog is
  // opened from inside a project. The legacy free-choice dropdown
  // is only shown when the user opened the dialog from a place
  // without project context (e.g. the global Automation Tasks page).
  const projectLocked = Boolean(defaultProjectName);
  const showProjectBlock = resourceType === 'agent' || resourceType === 'automation' || resourceType === 'kb' || resourceType === 'dashboard';

  const REFRESH_OPTIONS = [
    { value: 15, label: isEn ? '15s' : '15 秒' },
    { value: 30, label: isEn ? '30s' : '30 秒' },
    { value: 60, label: isEn ? '1 min' : '1 分钟' },
    { value: 300, label: isEn ? '5 min' : '5 分钟' },
  ];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{isEn ? `New ${resourceLabel}` : `新建${resourceLabel}`}</DialogTitle>
          <DialogDescription>{t.createDialog.desc}</DialogDescription>
        </DialogHeader>

        {templates.length > 0 && (
          <div>
            <Label className="mb-2 block text-xs text-muted-foreground">{t.createDialog.templatesLabel}</Label>
            <div className="flex flex-wrap gap-1.5">
              {templates.map((tpl, i) => (
                <button key={i} onClick={() => applyTemplate(tpl)} className="rounded-full border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground">
                  {tpl.name}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="space-y-4">
          {/* ── Project context block ──
              When opened from a project, this is a read-only badge plus
              the data sources the agent will inherit. Otherwise, the
              legacy dropdown is shown so the user can choose. */}
          {showProjectBlock && projectLocked && (
            <ProjectContextBlock
              projectName={defaultProjectName}
              isEn={isEn}
              dataSources={projectDataSources}
              t={t}
            />
          )}
          {showProjectBlock && !projectLocked && (
            <div>
              <Label className="mb-1.5 block text-xs">{t.createDialog.project}</Label>
              <Select value={project} onValueChange={setProject}>
                <SelectTrigger><SelectValue placeholder={t.createDialog.projectPh} /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="global">{t.createDialog.globalProject}</SelectItem>
                  {projects.filter((p) => p.name !== 'Ungrouped' && p.name !== '未分组').map((p) => (
                    <SelectItem key={p.id} value={p.name}>{p.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div>
            <Label className="mb-1.5 block text-xs">{t.createDialog.name}</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t.createDialog.namePh} />
          </div>
          {typeOptions && resourceType !== 'automation' && (
            <div>
              <Label className="mb-1.5 block text-xs">{t.createDialog.type}</Label>
              <Select value={type} onValueChange={setType}>
                <SelectTrigger><SelectValue placeholder={t.createDialog.typePh} /></SelectTrigger>
                <SelectContent>
                  {Object.entries(typeOptions).map(([val, label]) => (
                    <SelectItem key={val} value={val}>{label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {resourceType === 'automation' && (
            <div>
              <Label className="mb-1.5 block text-xs">{t.createDialog.schedule}</Label>
              <SchedulePicker value={schedule} onChange={setSchedule} />
            </div>
          )}
          {resourceType === 'automation' && (
            <div>
              <Label className="mb-1.5 block text-xs">{isEn ? 'Output format' : '输出格式'}</Label>
              <Select value={outputFormat} onValueChange={setOutputFormat}>
                <SelectTrigger data-testid="output-format-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {OUTPUT_FORMAT_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {resourceType === 'automation' && (
            <div data-testid="automation-skills-section">
              <SkillsSection
                form={{ skills }}
                update={(patch) => { if ('skills' in patch) setSkills(coerceStringArray(patch.skills)); }}
                t={t}
              />
              {/* LLM-informed tick — opt-in per-tick LLM briefing for
                  scheduled runs. Persisted via the prefill (create_automation
                  → llm_informed_tick). */}
              <label
                data-testid="automation-llm-tick"
                className="mt-3 flex cursor-pointer items-start gap-2 rounded-lg border border-border bg-background px-3 py-2.5"
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
            </div>
          )}
          {resourceType === 'dashboard' && (
            <div>
              <Label className="mb-1.5 block text-xs">{t.createDialog.refreshCadence}</Label>
              <Select value={String(refreshInterval)} onValueChange={(v) => setRefreshInterval(Number(v))}>
                <SelectTrigger data-testid="dashboard-refresh-select">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {REFRESH_OPTIONS.map((o) => (
                    <SelectItem key={o.value} value={String(o.value)}>{o.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="mt-1.5 text-[11px] leading-snug text-muted-foreground">
                {t.createDialog.dashboardHint}
              </p>
            </div>
          )}
          {resourceType === 'agent' && (
            <div>
              <Label className="mb-1.5 block text-xs">{t.createDialog.capabilities}</Label>
              <Input value={capabilities} onChange={(e) => setCapabilities(e.target.value)} placeholder={t.createDialog.capabilitiesPh} />
            </div>
          )}
          <div>
            <Label className="mb-1.5 block text-xs">{t.createDialog.description}</Label>
            <Textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t.createDialog.descPh} rows={3} className="resize-none" />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>{t.createDialog.cancel}</Button>
          <Button onClick={handleSubmit} disabled={!canSubmit}>
            <Sparkles className="h-4 w-4" /> {resourceType === 'agent' ? t.agentBuilder.buildButton : t.createDialog.submit} <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * ProjectContextBlock — read-only project badge with the bound
 * data sources listed underneath. Used when the dialog is opened
 * from inside a project so the user can see what the Agent will
 * inherit without re-picking anything.
 */
function ProjectContextBlock({ projectName, isEn, dataSources, t }) {
  const kbs = dataSources || [];
  const MAX_VISIBLE = 6;
  const visible = kbs.slice(0, MAX_VISIBLE);
  const hidden = kbs.length - visible.length;

  return (
    <TooltipProvider delayDuration={150}>
      <div className="rounded-lg border border-border bg-muted/30 p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm">
            <span className="flex h-7 w-7 items-center justify-center rounded-md bg-primary/10 text-primary">
              <FolderOpen className="h-3.5 w-3.5" />
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{t.createDialog.projectBadge}</span>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="inline-flex cursor-help items-center text-muted-foreground">
                      <Info className="h-3 w-3" />
                    </span>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="max-w-xs">
                    {t.createDialog.projectInherited}
                  </TooltipContent>
                </Tooltip>
              </div>
              <div className="truncate font-medium text-foreground">{projectName}</div>
            </div>
          </div>
          <span className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-2 py-0.5 text-[11px] text-muted-foreground">
            <Lock className="h-3 w-3" /> {isEn ? 'Locked' : '已锁定'}
          </span>
        </div>

        <div className="mt-3">
          <div className="mb-1.5 text-[11px] uppercase tracking-wide text-muted-foreground">
            {t.createDialog.dataSourcesLabel}
            {kbs.length > 0 && (
              <span className="ml-1.5 rounded-full bg-primary/10 px-1.5 py-px text-[10px] font-medium text-primary">
                {kbs.length}
              </span>
            )}
          </div>
          {kbs.length === 0 ? (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <AlertCircle className="h-3 w-3 shrink-0" />
              {t.createDialog.dataSourcesEmpty}
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {visible.map((kb) => (
                <DataSourceChip key={kb.id} kb={kb} t={t} />
              ))}
              {hidden > 0 && (
                <span className="inline-flex items-center rounded-full border border-dashed border-border bg-card px-2.5 py-1 text-xs text-muted-foreground">
                  {t.createDialog.dataSourcesMore} {hidden}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </TooltipProvider>
  );
}

function DataSourceChip({ kb, t }) {
  if (!kb) return null;
  const isFile = kb.source_kind === 'file';
  const Icon = isFile ? FileText : Database;
  const typeStr = kb.db_type
    ? (t?.kb?.dbTypes?.[kb.db_type] || kb.db_type)
    : (isFile
        ? (t?.kb?.fileTypes?.[kb.file_type] || t?.kb?.sourceKinds?.file || 'file')
        : (t?.kb?.sourceKinds?.database || 'database'));
  return (
    <span
      title={`${kb.name} · ${typeStr}`}
      className="inline-flex max-w-[14rem] items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs"
    >
      <Icon className="h-3 w-3 shrink-0 text-muted-foreground" />
      <span className="truncate font-medium text-foreground">{kb.name}</span>
      <span className="shrink-0 rounded bg-muted px-1.5 py-px text-[10px] text-muted-foreground">{typeStr}</span>
    </span>
  );
}

/**
 * Convert a structured template entry to the human-readable
 * schedule string the SchedulePicker knows how to parse. Keeps
 * template application in sync with the new picker.
 */
function buildTemplateString(tpl) {
  const s = tpl.schedule;
  if (s === 'daily' || s === 'weekly' || s === 'monthly' || s === 'hourly' || s === 'once' || s === 'custom') {
    const time = tpl.scheduleTime || '08:00';
    if (s === 'daily') return `Daily ${time}`;
    if (s === 'weekly') return `Weekly Mon-Fri ${time}`;
    if (s === 'monthly') return `Monthly ${tpl.scheduleDayOfMonth || 1}th ${time}`;
    if (s === 'hourly') return 'Every hour';
    if (s === 'once') return `${new Date().toISOString().slice(0, 10)} ${time}`;
  }
  return tpl.schedule || '';
}
