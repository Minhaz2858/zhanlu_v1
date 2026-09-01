import { Bot, X, Plus, ArrowDown, Repeat, GitBranch } from 'lucide-react';
import { flowStats } from '@/lib/agentFlow';

const samePath = (a, b) => Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);

/**
 * 高级编排流程树：顺序主干 + 可任意嵌套的循环块/并行块。
 * 每段顺序流末尾提供「＋智能体 / ＋循环 / ＋并行」三键，新手可按需组合。
 */
export default function FlowTree({ root, selectedPath, onSelect, onAddStep, onAddToBranch, onAddBranch, onRemove, lang, t }) {
  const isEn = lang === 'en';
  const stats = flowStats(root);

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{isEn ? 'Team Structure' : '团队结构'}</p>
        <span className="text-[10px] text-muted-foreground">
          {stats.agents} {isEn ? 'agents' : '智能体'} · {stats.loops} {isEn ? 'loops' : '循环'} · {stats.parallels} {isEn ? 'paralls' : '并行'}
        </span>
      </div>

      {/* 根节点 */}
      <button
        onClick={() => onSelect([])}
        className={`flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left transition-colors ${
          samePath(selectedPath, []) ? 'bg-primary/10 ring-1 ring-primary/30' : 'bg-primary/5 hover:bg-primary/10'
        }`}
      >
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Bot className="h-3.5 w-3.5" />
        </span>
        <span className="flex-1 truncate text-xs font-medium text-foreground">{root.name || (isEn ? 'Root Agent' : '根代理')}</span>
        <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">SequentialAgent</span>
      </button>

      <div className="mb-1 ml-1 mt-3 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">{t.agentConfig.flowTrunk}</div>

      <div className="ml-3.5 border-l-2 border-primary/30 pl-3">
        <FlowSteps
          steps={root.flow || []}
          makePath={(i) => [i]}
          onInsert={(kind) => onAddStep([], kind)}
          selectedPath={selectedPath}
          onSelect={onSelect}
          onRemove={onRemove}
          onAddStep={onAddStep}
          onAddToBranch={onAddToBranch}
          onAddBranch={onAddBranch}
          lang={lang}
          t={t}
        />
      </div>
    </div>
  );
}

function FlowSteps({ steps, makePath, onInsert, selectedPath, onSelect, onRemove, onAddStep, onAddToBranch, onAddBranch, lang, t }) {
  return (
    <div>
      {(!steps || steps.length === 0) && (
        <p className="px-1 py-1.5 text-[11px] text-muted-foreground">{t.agentConfig.flowEmpty}</p>
      )}
      {steps.map((step, i) => (
        <div key={step.id || i}>
          <FlowStep
            step={step}
            path={makePath(i)}
            selectedPath={selectedPath}
            onSelect={onSelect}
            onRemove={onRemove}
            onAddStep={onAddStep}
            onAddToBranch={onAddToBranch}
            onAddBranch={onAddBranch}
            lang={lang}
            t={t}
          />
          {i < steps.length - 1 && (
            <div className="flex justify-center py-0.5">
              <ArrowDown className="h-3.5 w-3.5 text-primary/50" strokeWidth={2.5} />
            </div>
          )}
        </div>
      ))}
      <InsertButtons onInsert={onInsert} t={t} />
    </div>
  );
}

function FlowStep(props) {
  const { step, path } = props;
  if (step.kind === 'loop') return <LoopBlock {...props} />;
  if (step.kind === 'parallel') return <ParallelBlock {...props} />;
  return <AgentStep step={step} path={path} selectedPath={props.selectedPath} onSelect={props.onSelect} onRemove={props.onRemove} lang={props.lang} />;
}

function AgentStep({ step, path, selectedPath, onSelect, onRemove, lang }) {
  const isEn = lang === 'en';
  const selected = samePath(selectedPath, path);
  return (
    <div
      onClick={() => onSelect(path)}
      className={`group flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-xs transition-colors ${
        selected ? 'bg-primary/10 text-primary ring-1 ring-primary/30' : 'text-foreground hover:bg-secondary/40'
      }`}
    >
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/15 text-primary ring-1 ring-primary/30">
        <Bot className="h-3 w-3" />
      </span>
      <span className="flex-1 truncate">{step.name || (isEn ? 'Agent' : '智能体')}</span>
      <span className="font-mono text-[9px] text-muted-foreground/60">LlmAgent</span>
      <button
        onClick={(e) => { e.stopPropagation(); onRemove(path); }}
        className="text-muted-foreground/0 transition-colors hover:text-destructive group-hover:text-muted-foreground"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}

function LoopBlock({ step, path, selectedPath, onSelect, onRemove, onAddStep, onAddToBranch, onAddBranch, lang, t }) {
  const isEn = lang === 'en';
  const selected = samePath(selectedPath, path);
  const maxIter = step.max_iterations ?? 5;
  return (
    <div className="my-1 rounded-lg border-2 border-dashed border-amber-500/50 bg-amber-500/5 p-2.5">
      <div
        onClick={() => onSelect(path)}
        className={`group flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors ${
          selected ? 'bg-amber-500/15 ring-1 ring-amber-500/40' : 'hover:bg-amber-500/10'
        }`}
      >
        <Repeat className="h-3.5 w-3.5 shrink-0 text-amber-600" strokeWidth={2.5} />
        <span className="flex-1 truncate font-medium text-amber-700">{step.name || (isEn ? 'Loop Block' : '循环块')}</span>
        <span className="text-[10px] text-amber-600/80">{t.agentConfig.maxIterLabel} {maxIter} {t.agentConfig.iterUnit}</span>
        <span className="font-mono text-[9px] text-amber-600/60">LoopAgent</span>
        <button
          onClick={(e) => { e.stopPropagation(); onRemove(path); }}
          className="text-amber-600/0 transition-colors hover:text-destructive group-hover:text-amber-600/60"
        >
          <X className="h-3 w-3" />
        </button>
      </div>
      <div className="ml-3.5 mt-2 border-l-2 border-amber-500/40 pl-3">
        <FlowSteps
          steps={step.flow || []}
          makePath={(i) => [...path, i]}
          onInsert={(kind) => onAddStep(path, kind)}
          selectedPath={selectedPath}
          onSelect={onSelect}
          onRemove={onRemove}
          onAddStep={onAddStep}
          onAddToBranch={onAddToBranch}
          onAddBranch={onAddBranch}
          lang={lang}
          t={t}
        />
      </div>
    </div>
  );
}

function ParallelBlock({ step, path, selectedPath, onSelect, onRemove, onAddStep, onAddToBranch, onAddBranch, lang, t }) {
  const isEn = lang === 'en';
  const selected = samePath(selectedPath, path);
  const branches = step.branches || [];
  return (
    <div className="my-1 rounded-lg border-2 border-primary/40 bg-primary/5 p-2.5">
      <div
        onClick={() => onSelect(path)}
        className={`group flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors ${
          selected ? 'bg-primary/10 ring-1 ring-primary/30' : 'hover:bg-primary/5'
        }`}
      >
        <GitBranch className="h-3.5 w-3.5 shrink-0 text-primary" strokeWidth={2.5} />
        <span className="flex-1 truncate font-medium text-primary">{step.name || (isEn ? 'Parallel Block' : '并行块')}</span>
        <span className="text-[10px] text-muted-foreground">{branches.length} {isEn ? 'branches' : '分支'}</span>
        <span className="font-mono text-[9px] text-muted-foreground/60">ParallelAgent</span>
        <button
          onClick={(e) => { e.stopPropagation(); onRemove(path); }}
          className="text-muted-foreground/0 transition-colors hover:text-destructive group-hover:text-muted-foreground"
        >
          <X className="h-3 w-3" />
        </button>
      </div>
      <div className="mt-2 flex gap-2">
        {branches.map((branch, b) => (
          <div key={b} className="min-w-0 flex-1">
            <div className="mb-1 px-1 text-[10px] text-muted-foreground">{t.agentConfig.branchLabel} {b + 1}</div>
            <div className="ml-2 border-l-2 border-primary/30 pl-2">
              <FlowSteps
                steps={branch}
                makePath={(i) => [...path, b, i]}
                onInsert={(kind) => onAddToBranch(path, b, kind)}
                selectedPath={selectedPath}
                onSelect={onSelect}
                onRemove={onRemove}
                onAddStep={onAddStep}
                onAddToBranch={onAddToBranch}
                onAddBranch={onAddBranch}
                lang={lang}
                t={t}
              />
            </div>
          </div>
        ))}
      </div>
      <button
        onClick={() => onAddBranch(path)}
        className="mt-1.5 inline-flex items-center gap-1 rounded-md border border-dashed border-primary/30 px-2 py-1 text-[11px] text-primary/70 transition-colors hover:border-primary/50 hover:text-primary"
      >
        <Plus className="h-3 w-3" /> {t.agentConfig.addBranch}
      </button>
    </div>
  );
}

function InsertButtons({ onInsert, t }) {
  const btns = [
    { kind: 'agent', label: t.agentConfig.addAgent, Icon: Bot, cls: 'text-primary' },
    { kind: 'loop', label: t.agentConfig.addLoop, Icon: Repeat, cls: 'text-amber-600' },
    { kind: 'parallel', label: t.agentConfig.addParallel, Icon: GitBranch, cls: 'text-primary' },
  ];
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {btns.map((b) => (
        <button
          key={b.kind}
          onClick={() => onInsert(b.kind)}
          className="inline-flex items-center gap-1 rounded-md border border-dashed border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
        >
          <b.Icon className={`h-3 w-3 ${b.cls}`} strokeWidth={2.5} /> ＋{b.label}
        </button>
      ))}
    </div>
  );
}