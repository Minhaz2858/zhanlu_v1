import { Bot, X, Plus, Network, ArrowDown, RotateCw, GitBranch, CornerDownRight, Repeat } from 'lucide-react';
import { WORKFLOW_CLASSES, localizedTopology } from '@/lib/agentArchitecture';

function samePath(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
}

const ROOT_CLS = {
  sequence: 'SequentialAgent',
  loop: 'LoopAgent',
  parallel: 'ParallelAgent',
};

export default function AgentTeamTree({ node, selectedPath, onSelect, onAdd, onRemove, onUpdateIterations, lang, t }) {
  const topology = node.topology || 'standalone';
  const isTeam = topology !== 'standalone';
  const cls = ROOT_CLS[topology] || 'LlmAgent';
  const subs = node.sub_agents || [];
  const maxIter = node.max_iterations ?? 5;
  const topoInfo = localizedTopology(topology, lang);
  const rootName = node.name || (lang === 'en' ? 'Root Agent' : '根代理');

  return (
    <div className="rounded-xl border border-border bg-card p-5">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{lang === 'en' ? 'Team Structure' : '团队结构'}</p>
        {isTeam && <span className="text-[10px] text-muted-foreground">{subs.length} {lang === 'en' ? 'sub-agents' : '子代理'}</span>}
      </div>

      {/* Root 节点 */}
      <button
        onClick={() => onSelect([])}
        className={`flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left transition-colors ${
          samePath(selectedPath, []) ? 'bg-primary/10 ring-1 ring-primary/30' : 'bg-primary/5 hover:bg-primary/10'
        }`}
      >
        <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Bot className="h-3.5 w-3.5" />
        </span>
        <span className="flex-1 truncate text-xs font-medium text-foreground">{rootName}</span>
        <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">{cls}</span>
        {topology === 'loop' && (
          <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">↻{maxIter}</span>
        )}
      </button>

      {/* 子代理 — 按拓扑区分展示 */}
      {!isTeam ? (
        <p className="mt-3 px-1 py-1 text-[11px] text-muted-foreground">
          {lang === 'en' ? 'Single agent mode — no sub-agents needed.' : '单一智能体模式，无需子代理。'}
        </p>
      ) : topology === 'sequence' ? (
        <SequenceTree subs={subs} selectedPath={selectedPath} onSelect={onSelect} onRemove={onRemove} onAdd={onAdd} lang={lang} t={t} />
      ) : topology === 'loop' ? (
        <LoopTree subs={subs} maxIter={maxIter} onUpdateIterations={onUpdateIterations} selectedPath={selectedPath} onSelect={onSelect} onRemove={onRemove} onAdd={onAdd} lang={lang} t={t} />
      ) : (
        <ParallelTree subs={subs} selectedPath={selectedPath} onSelect={onSelect} onRemove={onRemove} onAdd={onAdd} lang={lang} t={t} />
      )}

      {/* 拓扑说明 */}
      {isTeam && (
        <div className="mt-3 flex items-center gap-1.5 border-t border-border/50 px-1 pt-2 text-[10px] text-muted-foreground">
          <CornerDownRight className="h-2.5 w-2.5 shrink-0" />
          <span>{topoInfo.desc}</span>
        </div>
      )}
    </div>
  );
}

/* ── 顺序协作：编号管道 + 显著向下箭头 ── */
function SequenceTree({ subs, selectedPath, onSelect, onRemove, onAdd, lang, t }) {
  return (
    <div className="mt-2 ml-3.5 border-l-2 border-primary/30 pl-3">
      {subs.map((s, i) => {
        const childPath = [i];
        const selected = samePath(selectedPath, childPath);
        return (
          <div key={s.id || i}>
            <SubNode s={s} path={childPath} selected={selected} onSelect={onSelect} onRemove={onRemove} index={i} numbered lang={lang} />
            {i < subs.length - 1 && (
              <div className="flex flex-col items-center py-0.5">
                <div className="h-3 w-0.5 bg-primary/40" />
                <ArrowDown className="h-3.5 w-3.5 text-primary/60" strokeWidth={2.5} />
              </div>
            )}
          </div>
        );
      })}
      <AddButton onAdd={onAdd} t={t} />
    </div>
  );
}

/* ── 循环协作：显眼循环容器 + 回环箭头 ── */
function LoopTree({ subs, maxIter, onUpdateIterations, selectedPath, onSelect, onRemove, onAdd, lang, t }) {
  const isEn = lang === 'en';
  return (
    <div className="mt-2 ml-3.5 border-l-2 border-amber-500/50 pl-3">
      <div className="relative rounded-lg border-2 border-dashed border-amber-500/50 bg-amber-500/5 p-2.5">
        {/* 循环回环弧 */}
        <div className="absolute -right-2 top-1/2 flex flex-col items-center">
          <div className="h-12 w-4 rounded-r-full border-r-2 border-t-2 border-b-2 border-amber-500/40" />
          <RotateCw className="-mt-1 h-3.5 w-3.5 text-amber-500/60" strokeWidth={2.5} />
        </div>
        <div className="mb-2.5 flex items-center gap-1.5 rounded-md bg-amber-500/10 px-2 py-1 text-[10px] font-semibold text-amber-700">
          <Repeat className="h-3.5 w-3.5 shrink-0" strokeWidth={2.5} />
          <span className="shrink-0">{isEn ? 'LOOP BODY' : '循环体'}</span>
          <span className="text-amber-600/40">·</span>
          <span className="shrink-0 text-amber-600/80">{isEn ? 'max' : '最多'}</span>
          <input
            type="number"
            min={1}
            max={50}
            value={maxIter}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => onUpdateIterations && onUpdateIterations(Math.max(1, Number(e.target.value) || 1))}
            className="w-10 rounded border border-amber-500/40 bg-card px-1 py-0.5 text-center text-[11px] font-bold text-amber-700 focus:outline-none"
          />
          <span className="shrink-0 text-amber-600/80">{isEn ? 'iterations' : '次'}</span>
        </div>
        <div className="space-y-0.5 pr-5">
          {subs.length === 0 ? (
            <p className="px-1 py-1 text-xs text-muted-foreground">{t.agentConfig.noSubAgents}</p>
          ) : subs.map((s, i) => {
            const childPath = [i];
            const selected = samePath(selectedPath, childPath);
            return (
              <div key={s.id || i}>
                <SubNode s={s} path={childPath} selected={selected} onSelect={onSelect} onRemove={onRemove} lang={lang} variant="loop" />
                {i < subs.length - 1 && (
                  <div className="flex flex-col items-center py-0.5">
                    <div className="h-2.5 w-0.5 bg-amber-500/50" />
                    <ArrowDown className="h-3 w-3 text-amber-500/60" strokeWidth={2.5} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="mt-2 flex items-center justify-center gap-1.5 text-[10px] font-medium text-amber-600/80">
          <RotateCw className="h-3 w-3" strokeWidth={2.5} />
          <span>{isEn ? 'repeat until termination' : '循环直至终止条件'}</span>
        </div>
      </div>
      <AddButton onAdd={onAdd} t={t} />
    </div>
  );
}

/* ── 并行协作：明显分支树 ── */
function ParallelTree({ subs, selectedPath, onSelect, onRemove, onAdd, lang, t }) {
  const isEn = lang === 'en';
  return (
    <div className="mt-2 ml-3.5 border-l-2 border-primary/50 pl-3">
      <div className="mb-2 flex items-center gap-1.5 rounded-md bg-primary/10 px-2 py-1 text-[10px] font-semibold text-primary">
        <GitBranch className="h-3.5 w-3.5 shrink-0" strokeWidth={2.5} />
        <span>{isEn ? 'PARALLEL BRANCHES' : '并行分支'}</span>
      </div>
      <div className="space-y-1.5">
        {subs.length === 0 ? (
          <p className="px-1 py-1 text-xs text-muted-foreground">{t.agentConfig.noSubAgents}</p>
        ) : subs.map((s, i) => {
          const childPath = [i];
          const selected = samePath(selectedPath, childPath);
          return (
            <div key={s.id || i} className="relative pl-4">
              <span className="absolute left-0 top-1/2 h-px w-4 -translate-y-1/2 bg-primary/60" />
              <span className="absolute left-0 top-0 h-1/2 w-0.5 bg-primary/40" />
              <SubNode s={s} path={childPath} selected={selected} onSelect={onSelect} onRemove={onRemove} lang={lang} variant="parallel" index={i} />
            </div>
          );
        })}
      </div>
      <AddButton onAdd={onAdd} t={t} />
    </div>
  );
}

/* ── 共享子节点 ── */
function SubNode({ s, path, selected, onSelect, onRemove, index, numbered, variant, lang }) {
  const isEn = lang === 'en';
  const childCls = (s.topology && s.topology !== 'standalone') ? (WORKFLOW_CLASSES[s.topology] || 'LlmAgent') : 'LlmAgent';
  const variantRing =
    variant === 'loop' ? 'border-l-2 border-amber-500/50'
    : variant === 'parallel' ? 'border-l-2 border-primary/50'
    : '';
  return (
    <div
      onClick={() => onSelect(path)}
      className={`group flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-xs transition-colors ${
        selected ? 'bg-primary/10 text-primary ring-1 ring-primary/30' : 'text-foreground hover:bg-secondary/40'
      } ${variantRing}`}
    >
      {numbered ? (
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[10px] font-bold text-primary ring-1 ring-primary/30">{index + 1}</span>
      ) : variant === 'loop' ? (
        <RotateCw className="h-3.5 w-3.5 shrink-0 text-amber-500/70" strokeWidth={2} />
      ) : variant === 'parallel' ? (
        <GitBranch className="h-3.5 w-3.5 shrink-0 text-primary/70" strokeWidth={2} />
      ) : (
        <Network className="h-3 w-3 shrink-0 text-muted-foreground" />
      )}
      <span className="flex-1 truncate">{s.name || (isEn ? 'Sub-Agent' : '子智能体')}</span>
      <span className="font-mono text-[9px] text-muted-foreground/60">{childCls}</span>
      <button
        onClick={(e) => { e.stopPropagation(); onRemove(path); }}
        className="text-muted-foreground/0 transition-colors hover:text-destructive group-hover:text-muted-foreground"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}

function AddButton({ onAdd, t }) {
  return (
    <button
      onClick={onAdd}
      className="mt-1.5 inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-dashed border-border px-2.5 py-1.5 text-xs text-muted-foreground hover:border-primary/40 hover:text-foreground"
    >
      <Plus className="h-3 w-3" /> {t.agentConfig.addSubAgent}
    </button>
  );
}