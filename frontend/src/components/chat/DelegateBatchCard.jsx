import { useState } from 'react';
import { ChevronDown, ChevronUp, Users, AlertTriangle } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * DelegateBatchCard — evidence-inspection surface for multi-agent
 * delegation (P1 Phase 4).
 *
 * Rendered from a `delegate_task` tool_call whose result is the P1 batch
 * contract payload: `{status, summary, count, results: [{agent_name,
 * status, task, response, error, truncated, iterations}]}`. Without this
 * card the batch result only influenced the LLM's synthesized answer
 * invisibly — the user had no way to see WHICH specialist did WHAT, or
 * that one child partially failed while the parent's answer looked fine.
 *
 * Explicitly surfaces partial/failed children (roadmap acceptance:
 * "partial and failure states are explicit, never silently merged"),
 * with the full child response inspectable on expand.
 */

const STATUS_STYLES = {
  completed: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/40',
  partial: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40',
  failed: 'bg-red-500/15 text-red-700 dark:text-red-300 border-red-500/40',
};

function normalizeStatus(r) {
  if (!r) return 'failed';
  if (r.status) return r.status;
  if (r.success === false) return 'failed';
  return r.truncated ? 'partial' : 'completed';
}

export default function DelegateBatchCard({ batch }) {
  const { lang } = useLanguage();
  const [openIdx, setOpenIdx] = useState(null);

  if (!batch || !Array.isArray(batch.results) || batch.results.length === 0) return null;

  const overall = batch.status || (batch.success === false ? 'failed' : 'completed');
  const hasIssues = batch.results.some((r) => normalizeStatus(r) !== 'completed');

  return (
    <div
      data-testid="delegate-batch-card"
      className="mt-2 rounded-lg border border-border bg-card text-xs"
    >
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <Users className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="font-medium text-foreground">
          {lang === 'en'
            ? `Delegated to ${batch.results.length} specialist${batch.results.length === 1 ? '' : 's'}`
            : `已委派 ${batch.results.length} 位专家 Agent`}
        </span>
        {batch.summary && (
          <span className={`ml-auto rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_STYLES[overall] || STATUS_STYLES.completed}`}>
            {batch.summary}
          </span>
        )}
      </div>
      {hasIssues && (
        <div className="flex items-center gap-1.5 border-b border-border bg-amber-500/5 px-3 py-1.5 text-[11px] text-amber-700 dark:text-amber-300">
          <AlertTriangle className="h-3 w-3 shrink-0" />
          {lang === 'en'
            ? 'One or more sub-agents returned partial or failed results — the answer above may be incomplete.'
            : '部分子 Agent 返回了不完整或失败的结果 — 上方回答可能不完整。'}
        </div>
      )}
      <div className="divide-y divide-border">
        {batch.results.map((r, i) => {
          const st = normalizeStatus(r);
          const open = openIdx === i;
          const body = (r.response || r.error || '').trim();
          return (
            <div key={i}>
              <button
                type="button"
                onClick={() => setOpenIdx(open ? null : i)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-secondary/50"
                aria-expanded={open}
              >
                <span className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold ${STATUS_STYLES[st] || STATUS_STYLES.completed}`}>
                  {st}
                </span>
                <span className="shrink-0 font-medium text-foreground">{r.agent_name || 'agent'}</span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">{r.task}</span>
                {open ? <ChevronUp className="h-3 w-3 shrink-0 text-muted-foreground" /> : <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />}
              </button>
              {open && (
                <div className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words border-t border-border/60 bg-secondary/30 px-3 py-2 text-[11px] leading-relaxed text-foreground/90">
                  {body || (lang === 'en' ? '(no response captured)' : '（未捕获响应）')}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
