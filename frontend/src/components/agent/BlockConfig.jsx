import { Section, Field, inputCls } from './AgentParts';
import { Repeat, GitBranch, Plus, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';

/** 循环/并行块的配置面板（高级编排模式下选中块时显示） */
export default function BlockConfig({ node, update, onAddBranch, onRemoveBranch, lang, t }) {
  const isEn = lang === 'en';
  const isLoop = node.kind === 'loop';

  return (
    <Section
      title={t.agentConfig.blockConfig}
      desc={isLoop ? t.agentConfig.loopBlockDesc : t.agentConfig.parallelBlockDesc}
      icon={isLoop ? Repeat : GitBranch}
    >
      <div className="space-y-4">
        <Field label={t.agentConfig.name}>
          <input
            value={node.name || ''}
            onChange={(e) => update({ name: e.target.value })}
            className={inputCls}
            placeholder={isLoop ? (isEn ? 'Loop block name' : '循环块名称') : (isEn ? 'Parallel block name' : '并行块名称')}
          />
        </Field>

        {isLoop ? (
          <Field label={`${t.agentConfig.maxIterLabel} (${t.agentConfig.iterUnit})`} hint={isEn ? 'Terminate after this many iterations' : '达到此次数后终止循环'}>
            <input
              type="number"
              min={1}
              max={50}
              value={node.max_iterations ?? 5}
              onChange={(e) => update({ max_iterations: Math.max(1, Number(e.target.value) || 1) })}
              className={inputCls}
            />
          </Field>
        ) : (
          <div>
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium text-muted-foreground">
                {t.agentConfig.branchLabel} ({(node.branches || []).length})
              </span>
              <Button size="sm" variant="outline" onClick={onAddBranch} className="h-7 gap-1 text-xs">
                <Plus className="h-3 w-3" /> {t.agentConfig.addBranch}
              </Button>
            </div>
            <div className="space-y-1">
              {(node.branches || []).map((br, b) => (
                <div key={b} className="flex items-center gap-2 rounded-md border border-border bg-secondary/20 px-2 py-1.5 text-xs">
                  <GitBranch className="h-3 w-3 text-primary/60" />
                  <span className="text-muted-foreground">{t.agentConfig.branchLabel} {b + 1}</span>
                  <span className="text-muted-foreground/60">· {br.length} {isEn ? 'steps' : '步骤'}</span>
                  <button onClick={() => onRemoveBranch(b)} className="ml-auto text-muted-foreground transition-colors hover:text-destructive">
                    <Trash2 className="h-3 w-3" />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </Section>
  );
}