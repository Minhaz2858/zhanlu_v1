import { useState, useEffect, useRef } from 'react';
import { Sparkles, Plus, Check, Database, Code, Clock, Wifi, FileText, Server, Cloud, Cpu, Gauge, Shield, Zap, Workflow, Settings, Bell, Link, Key, AlertTriangle, Boxes, Network, HardDrive, Activity, BarChart3, GitBranch, ListChecks, Loader2 } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

const ICON_MAP = {
  database: Database, db: Database,
  api: Code, code: Code, interface: Code, endpoint: Code,
  clock: Clock, time: Clock, history: Clock, schedule: Clock,
  wifi: Wifi, signal: Wifi, stream: Wifi, realtime: Wifi,
  file: FileText, document: FileText, csv: FileText, report: FileText,
  server: Server, host: Server,
  cloud: Cloud,
  cpu: Cpu, processor: Cpu,
  gauge: Gauge, meter: Gauge,
  shield: Shield, security: Shield, safety: Shield,
  zap: Zap, energy: Zap, power: Zap,
  workflow: Workflow, flow: Workflow, process: Workflow,
  settings: Settings, config: Settings, gear: Settings,
  bell: Bell, alert: Bell, notification: Bell, alarm: Bell,
  link: Link, connect: Link, integration: Link,
  key: Key, auth: Key, credential: Key,
  warning: AlertTriangle, risk: AlertTriangle, error: AlertTriangle,
  boxes: Boxes, inventory: Boxes, stock: Boxes,
  network: Network,
  harddrive: HardDrive, storage: HardDrive, disk: HardDrive,
  activity: Activity, monitor: Activity, status: Activity,
  chart: BarChart3, analytics: BarChart3, graph: BarChart3,
  branch: GitBranch, version: GitBranch,
};

function getIcon(name) {
  if (!name) return null;
  return ICON_MAP[String(name).toLowerCase()] || null;
}

export default function ClarifyOptions({ block, onSelectOption, onSelectOther }) {
  const { t, lang } = useLanguage();
  const { prompt, subtext, options = [], step, total } = block;
  const [selected, setSelected] = useState(null);
  const [autoCollecting, setAutoCollecting] = useState(false);
  const autoCollectFiredRef = useRef(false);
  const stepNum = Number(step) || 0;
  const totalNum = Number(total) || 0;
  const hasProgress = stepNum > 0 && totalNum > 0;
  // Multi-step legacy mode: the LLM is asking questions one at a time
  // instead of using the new batch format. We auto-request a batch
  // re-emission so the user sees a single batched form, not N separate
  // single-question forms.
  const isMultiStepLegacy = hasProgress && totalNum >= 2;

  // Auto-trigger: when we land on step 1 of a multi-step clarify,
  // silently ask the LLM to re-emit all questions in batch format.
  // The user sees a brief "collecting…" state, then the batch form.
  useEffect(() => {
    if (!isMultiStepLegacy) return;
    if (autoCollectFiredRef.current) return;
    if (stepNum !== 1) return;
    autoCollectFiredRef.current = true;
    setAutoCollecting(true);
    const reaskTimer = setTimeout(() => {
      onSelectOption?.(
        '【系统提示】请使用 clarify_batch 字段一次性输出所有需要确认的配置问题（共 ' + totalNum + ' 步），不要逐个使用 step/total。我希望一次性看到所有问题并一次性回答。'
      );
      // Drop the spinner once the re-ask request has been sent so the
      // user isn't stuck on the loading card if the LLM still answers
      // with step/total again (or is slow). The next ClarifyOptions
      // instance will then render the real form.
      setAutoCollecting(false);
    }, 600);
    // Safety net: if for any reason the spinner is still showing after
    // 3s (e.g. onSelectOption threw, or the response was throttled),
    // clear it so the user is never trapped on the loading card.
    const safetyTimer = setTimeout(() => setAutoCollecting(false), 3000);
    return () => {
      clearTimeout(reaskTimer);
      clearTimeout(safetyTimer);
    };
  }, [isMultiStepLegacy, stepNum, totalNum, onSelectOption]);

  function handleManualReask() {
    if (selected || autoCollectFiredRef.current) return;
    autoCollectFiredRef.current = true;
    onSelectOption?.(
      '请使用 clarify_batch 字段一次性列出所有需要确认的配置问题（不要逐个使用 step/total），我会一次性回答。'
    );
    setAutoCollecting(false);
  }

  function handlePick(label) {
    if (selected) return;
    setSelected(label);
    onSelectOption?.(label);
  }

  function handleReaskBatch() {
    if (selected) return;
    setSelected('__reask_batch__');
    onSelectOption?.(
      '请使用 clarify_batch 字段一次性列出所有需要确认的配置问题（不要逐个使用 step/total），我会一次性回答。'
    );
  }

  // While auto-collecting, show a brief loading state instead of the
  // single-step options — the user is about to see a batched form.
  // We also surface a manual "switch to batch" escape hatch in case the
  // LLM keeps answering with step/total and the user wants to break out.
  if (autoCollecting) {
    return (
      <div className="mt-3 flex items-center gap-3 rounded-xl border border-primary/30 bg-primary/5 px-4 py-3 text-sm text-foreground">
        <Loader2 className="h-4 w-4 animate-spin text-primary" />
        <div className="flex-1">
          <div className="font-medium">{lang === 'zh' ? `正在汇总 ${totalNum} 个配置问题…` : `Collating ${totalNum} config questions…`}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">{lang === 'zh' ? '将以批量表单一次性呈现，避免分步来回。' : 'Presented all at once as a batch form to avoid back-and-forth.'}</div>
        </div>
        <button
          type="button"
          onClick={handleManualReask}
          className="rounded-md border border-primary/30 bg-background/60 px-2 py-1 text-[11px] font-medium text-primary transition-colors hover:bg-primary/10"
        >
          {lang === 'zh' ? '切换 → 批量表单' : 'Switch → Batch Form'}
        </button>
      </div>
    );
  }

  return (
    <div className="mt-3 space-y-3">
      {isMultiStepLegacy && (
        <button
          onClick={handleReaskBatch}
          disabled={!!selected}
          className="flex w-full items-center justify-between gap-2 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2 text-left text-xs text-foreground transition-colors hover:bg-primary/10 disabled:opacity-40"
        >
          <span className="flex items-center gap-1.5">
            <ListChecks className="h-3.5 w-3.5 text-primary" />
            <span className="font-medium">{lang === 'zh' ? `检测到分步提问（共 ${totalNum} 步）` : `Step-by-step questioning detected (${totalNum} steps)`}</span>
            <span className="text-muted-foreground">·</span>
            <span className="text-muted-foreground">{lang === 'zh' ? '点击改为一次性列出所有问题' : 'Click to list all questions at once'}</span>
          </span>
          <span className="text-[11px] text-primary">{lang === 'zh' ? '切换 →' : 'Switch →'}</span>
        </button>
      )}
      {hasProgress && (
        <div className="flex items-center gap-3">
          <span className="shrink-0 text-xs font-medium text-muted-foreground">{t.chat.clarify.step} {stepNum}/{totalNum}</span>
          <div className="flex flex-1 gap-1.5">
            {Array.from({ length: totalNum }).map((_, i) => (
              <div key={i} className={`h-1 flex-1 rounded-full transition-colors ${i < stepNum ? 'bg-primary' : 'bg-border'}`} />
            ))}
          </div>
        </div>
      )}
      <div className="rounded-xl border border-border bg-secondary/30 p-3.5">
        <div className="mb-2 flex items-center gap-2">
          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-foreground text-background">
            <Sparkles className="h-3 w-3" />
          </span>
          <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">{t.chat.clarify.tag}</span>
        </div>
        <p className="text-sm font-medium leading-relaxed text-foreground">{prompt}</p>
        {subtext && <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{subtext}</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        {options.map((opt, idx) => {
          const letter = String.fromCharCode(65 + idx);
          const Icon = getIcon(opt.icon);
          const isSelected = selected === opt.label;
          const isDimmed = selected && !isSelected;
          return (
            <button
              key={idx}
              onClick={() => handlePick(opt.label)}
              disabled={!!selected}
              className={`group relative flex min-w-[150px] flex-1 flex-col gap-2 rounded-xl border p-3 text-left transition-all ${
                isSelected
                  ? 'border-primary bg-primary/5 ring-1 ring-primary/30'
                  : isDimmed
                  ? 'border-border bg-card opacity-40'
                  : 'border-border bg-card hover:border-primary/40 hover:bg-secondary/30'
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="flex h-6 w-6 items-center justify-center rounded-md bg-secondary text-xs font-semibold text-foreground">{letter}</span>
                {isSelected ? (
                  <Check className="h-4 w-4 text-primary" />
                ) : Icon ? (
                  <Icon className="h-4 w-4 text-muted-foreground" />
                ) : null}
              </div>
              <span className="text-sm font-medium leading-snug text-foreground">{opt.label}</span>
              {opt.desc && <span className="text-xs leading-relaxed text-muted-foreground">{opt.desc}</span>}
            </button>
          );
        })}
      </div>
      <button
        onClick={() => { if (!selected) onSelectOther?.(); }}
        disabled={!!selected}
        className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-border bg-card py-2 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground disabled:opacity-40"
      >
        <Plus className="h-3.5 w-3.5" /> {t.chat.clarify.other} · {t.chat.clarify.otherDesc}
      </button>
    </div>
  );
}