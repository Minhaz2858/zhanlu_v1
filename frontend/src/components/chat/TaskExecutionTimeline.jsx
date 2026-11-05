// Unified Agent Execution Timeline (2026-11-01).
//
// ONE primary execution card replacing the three duplicated displays (plan
// checklist, live execution rows, agent panel):
//   A. Task Progress Card  — steps from the ACTUAL execution plan
//      (plan_step_added/completed live events — never hardcoded).
//   B. Nested Agent Activity — delegated specialists + their tool-call
//      summaries render INSIDE the step they served (hierarchical).
//   C. Technical Execution Details — the existing AgentExecutionTree moves
//      into a collapsed <details> section (passed in as `detailsContent`).
//
// Single source of truth: everything derives from the SAME live_events
// stream via the existing pure reducers (AgentExecutionTree's
// buildTreeFromLiveEvents / summarizeNodes). No new state-management
// system — derived, deterministic, compatible between live streaming and
// persisted history (Chat.jsx persists live_events on the final message).
import { useMemo, useState, createContext, useContext } from 'react';
import { Check, Circle, XCircle, AlertTriangle, Loader2, ListChecks, ChevronDown, Wrench } from 'lucide-react';

/**
 * Lets the embedded LiveActivityStream footer "Collapse" button collapse the
 * WHOLE merged card (2026-11-02 user request: "when click on the collapse all
 * need to be collaps"). Default is inert so standalone (non-embedded) feeds
 * keep their own internal collapse behavior.
 */
export const TimelineCollapseContext = createContext({
  collapsed: false,
  collapseAll: null,
});
import { useLanguage } from '@/lib/LanguageProvider';
import { buildTreeFromLiveEvents } from './AgentExecutionTree';

// ── Plan-step derivation — DYNAMIC + replan-aware (2026-11-02) ───────────
// Consumes the real orchestration events:
//   plan_step_added    → a task exists (title, task_id, description)
//   plan_step_completed→ task done
//   plan_updated       → supervisor REVISED the plan mid-turn:
//                        status superseded | failed | pending. Merged by
//                        task_id: completed tasks stay completed, the
//                        affected row updates in place, superseded rows stay
//                        visible (struck through) — never silently erased.
// Terminal vocabulary: pending / running / done / failed / superseded.
// Statuses are ordered so terminal events can never regress (sticky).
export function derivePlanSteps(events) {
  const byIndex = new Map();
  const rank = { pending: 0, running: 1, done: 2, failed: 2, superseded: 2 };
  const apply = (idx, fn) => {
    if (idx == null) return;
    const cur = byIndex.get(idx) || { step_index: idx, title: '', status: 'pending' };
    fn(cur);
    byIndex.set(idx, cur);
  };
  for (const ev of events || []) {
    const p = ev?.params || {};
    if (ev?.type === 'plan_step_added') {
      apply(p.step_index, (cur) => {
        if (!cur.title && p.title) cur.title = p.title;
        if (p.task_id) cur.task_id = p.task_id;
        if (p.description) cur.description = p.description;
      });
    } else if (ev?.type === 'plan_step_completed') {
      apply(p.step_index, (cur) => {
        if (cur.status !== 'superseded') cur.status = 'done';
        if (p.task_id) cur.task_id = p.task_id;
      });
    } else if (ev?.type === 'plan_updated') {
      apply(p.step_index, (cur) => {
        const next = p.status || 'pending';
        if (p.title) cur.title = p.title;
        if (p.task_id) cur.task_id = p.task_id;
        cur.revised = true; // streaming fast-path must not override a revision
        // sticky: never regress a terminal row to pending; a failed row can
        // be re-opened by an explicit pending (supervisor retry).
        if (rank[next] >= rank[cur.status] || cur.status === 'failed') cur.status = next;
      });
    }
  }
  return [...byIndex.values()].sort((a, b) => a.step_index - b.step_index);
}

// ── Business-friendly labels: hide raw redaction placeholders like
//    "[data]"; fall back to the honest positional label. NEVER invent a
//    name — "Step N" says exactly what it is. ─────────────────────────────
const PLACEHOLDER_TITLE = /^\[[^\]]*\]$|^\{\{?\w+\}?\}$|^<[^>]*>$|^\s*$/;
export function displayStepTitle(step) {
  const t = (step?.title || '').trim();
  return PLACEHOLDER_TITLE.test(t) ? `Step ${step?.step_index ?? ''}`.trim() : t;
}

// ── Agent → step attribution: an agent belongs to the step being worked
//    when it started (after step N-1 completed, before step N completed).
//    Deterministic from event timestamps; no fabricated assignment. ──────
export function attributeAgentsToSteps(events, steps) {
  // 2026-11-02: agents whose delegate call carries an explicit plan task
  // link (agent_started.params.plan_task_id) attach to that task directly —
  // no timestamp guessing. The heuristic below remains the fallback.
  const stepsByTaskId = new Map((steps || []).filter((s) => s.task_id).map((s) => [s.task_id, s.step_index]));
  const completions = (events || [])
    .filter((e) => e?.type === 'plan_step_completed' && e.params?.step_index != null)
    .map((e) => ({ idx: e.params.step_index, ts: e.ts ? Date.parse(e.ts) : 0 }))
    .sort((a, b) => a.ts - b.ts);
  const starts = new Map();
  const explicit = new Map();
  for (const e of events || []) {
    if (e?.type === 'agent_started' && e.params?.agent_execution_id) {
      starts.set(e.params.agent_execution_id, e.ts ? Date.parse(e.ts) : 0);
      const tid = e.params?.plan_task_id;
      if (tid && stepsByTaskId.has(tid)) explicit.set(e.params.agent_execution_id, stepsByTaskId.get(tid));
    } else if (e?.type === 'subagent_invoked' && e.params?.subagent_id) {
      starts.set(e.params.subagent_id, e.ts ? Date.parse(e.ts) : 0);
    }
  }
  const out = new Map();
  for (const [id, ts] of starts) {
    if (explicit.has(id)) { out.set(id, explicit.get(id)); continue; }
    // Last completion at-or-before the agent's start; the agent works on
    // the NEXT step (the one in progress). None → first step. After ALL
    // completions → the next pending step if one exists (sequential
    // delegation), else the last completed step (synthesis tail work).
    let assigned = steps.length ? steps[0].step_index : 0;
    for (const c of completions) {
      if (c.ts <= ts) assigned = c.idx + 1;
    }
    if (completions.length && completions[completions.length - 1].ts <= ts) {
      const lastIdx = completions[completions.length - 1].idx;
      const next = steps.find(
        (s) => s.step_index > lastIdx && s.status !== 'superseded',
      );
      assigned = next ? next.step_index : lastIdx;
    }
    out.set(id, assigned);
  }
  return out;
}

const STATUS_STYLES = {
  completed: 'text-emerald-600 dark:text-emerald-400',
  done: 'text-emerald-600 dark:text-emerald-400',
  running: 'text-blue-600 dark:text-blue-400',
  partial: 'text-amber-600 dark:text-amber-400',
  failed: 'text-red-600 dark:text-red-400',
  cancelled: 'text-muted-foreground',
  superseded: 'text-muted-foreground/60',
  not_run: 'text-muted-foreground',
  pending: 'text-muted-foreground/70',
};

function StatusDot({ status }) {
  const cls = STATUS_STYLES[status] || STATUS_STYLES.pending;
  if (status === 'running') return <Loader2 className={`h-3.5 w-3.5 animate-spin ${cls}`} />;
  if (status === 'done' || status === 'completed') return <Check className={`h-3.5 w-3.5 ${cls}`} />;
  if (status === 'failed') return <XCircle className={`h-3.5 w-3.5 ${cls}`} />;
  if (status === 'partial') return <AlertTriangle className={`h-3.5 w-3.5 ${cls}`} />;
  if (status === 'superseded') return <Check className={`h-3 w-3 ${cls}`} />;
  if (status === 'pending' || status === 'not_run') return <Circle className={`h-3 w-3 ${cls}`} />;
  return <Circle className={`h-3 w-3 ${cls}`} />;
}

const statusLabel = (st, isEn) => {
  switch (st) {
    case 'done':
    case 'completed': return isEn ? 'Completed' : '已完成';
    case 'running': return isEn ? 'Running' : '运行中';
    case 'pending': return isEn ? 'Pending' : '待执行';
    case 'partial': return isEn ? 'Partial' : '部分完成';
    case 'failed': return isEn ? 'Failed' : '失败';
    case 'cancelled': return isEn ? 'Cancelled' : '已取消';
    case 'superseded': return isEn ? 'Revised' : '已调整';
    case 'not_run': return isEn ? 'Not run' : '未执行';
    default: return st || '';
  }
};

function AgentRow({ node, isEn }) {
  const toolCount = (node.children || []).filter((c) => c.is_tool).length;
  const label = node.agent_name || node.agent_label || 'specialist';
  return (
    <div
      data-testid={`agent-status-${node.id}`}
      className="flex items-center gap-1.5 rounded-md bg-secondary/30 px-2 py-1 text-[11px]"
    >
      <StatusDot status={node.status} />
      <span className="font-medium text-foreground/90">{label}</span>
      <span className={`ml-auto ${STATUS_STYLES[node.status] || 'text-muted-foreground'}`}>
        {statusLabel(node.status, isEn)}
      </span>
      {toolCount > 0 && (
        <span className="flex items-center gap-0.5 text-muted-foreground">
          · <Wrench className="h-2.5 w-2.5" /> {isEn ? `${toolCount} tool call${toolCount > 1 ? 's' : ''}` : `${toolCount} 次工具调用`}
        </span>
      )}
    </div>
  );
}

/**
 * The ONE primary execution timeline. Renders nothing for plain turns
 * (no plan steps, no agent events) — those keep the existing UI.
 */
export default function TaskExecutionTimeline({
  liveEvents,
  streamingPlanSteps,
  streaming = false,
  detailsContent = null,
  defaultDetailsOpen = false,
  activityContent = null,
}) {
  const { lang } = useLanguage();
  const isEn = lang === 'en';
  // 2026-11-02: the whole card is user-collapsible (header click), so the
  // plan stepper AND the merged-in live activity collapse together — one
  // section instead of two stacked cards.
  const [collapsed, setCollapsed] = useState(false);

  const steps = useMemo(() => {
    // 2026-11-02: live_events derivation is authoritative (it alone carries
    // plan_updated revisions + task_id identity). Streaming steps (fast
    // direct-SSE path) fill gaps only — they never override a revised row.
    const derived = derivePlanSteps(liveEvents);
    const streaming = Array.isArray(streamingPlanSteps) ? streamingPlanSteps : [];
    if (!derived.length) return streaming;
    const streamMap = new Map(streaming.map((s) => [s.step_index, s]));
    const out = derived.map((d) => {
      const st = streamMap.get(d.step_index);
      if (st && !d.revised && d.status === 'pending' && st.status === 'done') {
        return { ...d, status: 'done' };
      }
      return d;
    });
    for (const st of streaming) {
      if (!derived.some((d) => d.step_index === st.step_index)) out.push(st);
    }
    return out.sort((a, b) => a.step_index - b.step_index);
  }, [liveEvents, streamingPlanSteps]);

  // Single source: the SAME reducer the Technical Execution Details uses.
  const tree = useMemo(
    () => buildTreeFromLiveEvents(liveEvents, { finalize: !streaming }),
    [liveEvents, streaming],
  );

  const attribution = useMemo(
    () => attributeAgentsToSteps(liveEvents, steps),
    [liveEvents, steps],
  );

  // With merged-in activity, the card must render even without plan steps
  // or an agent tree — otherwise the embedded feed would vanish.
  if (!steps.length && !tree && !activityContent) return null;

  const roots = tree?.roots || [];
  const doneCount = steps.filter((s) => s.status === 'done').length;
  const progress = steps.length ? Math.round((doneCount / steps.length) * 100) : null;
  const currentStep = steps.find((s) => s.status !== 'done');
  const verification = tree?.verification || null;
  const evidenceCount = tree?.summary?.evidence_count || 0;

  const agentsByStep = new Map();
  const unassigned = [];
  for (const root of roots) {
    const stepIdx = attribution.get(root.id);
    if (stepIdx == null) unassigned.push(root);
    else {
      if (!agentsByStep.has(stepIdx)) agentsByStep.set(stepIdx, []);
      agentsByStep.get(stepIdx).push(root);
    }
  }

  return (
    <TimelineCollapseContext.Provider value={{ collapsed, collapseAll: () => setCollapsed(true) }}>
    <div
      data-testid="task-execution-timeline"
      className="mb-3 rounded-xl border border-border/80 bg-card px-3 py-2.5"
    >
      {/* Card header: status + progress — click toggles the whole card */}
      <button
        type="button"
        data-testid="task-execution-toggle"
        onClick={() => setCollapsed((v) => !v)}
        className="mb-2 flex w-full items-center gap-2 text-left"
        aria-expanded={!collapsed}
      >
        <ListChecks className="h-4 w-4 shrink-0 text-indigo-500" />
        <span className="text-xs font-semibold text-foreground">
          {isEn ? 'Task execution' : '任务执行'}
        </span>
        {currentStep && (
          <span className="truncate text-xs text-muted-foreground">
            — {displayStepTitle(currentStep)}
          </span>
        )}
        {progress != null && (
          <span className="ml-auto shrink-0 text-[11px] tabular-nums text-muted-foreground">
            {doneCount}/{steps.length}
          </span>
        )}
        <ChevronDown
          className={`h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform ${collapsed ? '-rotate-90' : ''}`}
        />
      </button>

      {!collapsed && (
        <>
      {/* Compact progress indicator */}
      {steps.length > 0 && (
        <div className="mb-2 h-1 w-full overflow-hidden rounded-full bg-secondary">
          <div
            className="h-full rounded-full bg-indigo-500 transition-all duration-500"
            style={{ width: `${progress}%` }}
            data-testid="timeline-progress"
          />
        </div>
      )}

      {/* Vertical stepper with nested agent activity */}
      {steps.length > 0 ? (
        <ol className="space-y-1.5">
          {steps.map((s) => {
            const agents = agentsByStep.get(s.step_index) || [];
            return (
              <li
                key={s.step_index}
                data-testid={`timeline-step-${s.step_index}`}
                className="flex gap-2"
              >
                <div className="flex flex-col items-center pt-0.5">
                  <StatusDot status={s.status} />
                  {s.step_index < steps.length - 1 && (
                    <span className="mt-0.5 w-px flex-1 bg-border" />
                  )}
                </div>
                <div className="min-w-0 flex-1 pb-1">
                  <div className="flex items-baseline gap-2">
                    <span
                      className={`text-xs ${(s.status === 'done' || s.status === 'superseded') ? 'text-muted-foreground line-through decoration-border' : 'font-medium text-foreground'}`}
                    >
                      {displayStepTitle(s)}
                    </span>
                    {s.status !== 'done' && (
                      <span className={`text-[10px] ${STATUS_STYLES[s.status] || 'text-muted-foreground'}`}>
                        {statusLabel(s.status, isEn)}
                      </span>
                    )}
                  </div>
                  {/* Planner-provided user-friendly description (dynamic
                      plans only — never fabricated for template steps). */}
                  {s.description && s.status !== 'superseded' && (
                    <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground/80">
                      {s.description}
                    </div>
                  )}
                  {/* B. Nested agent activity inside the step */}
                  {agents.length > 0 && (
                    <div className="mt-1 space-y-1 border-l border-border/60 pl-2.5">
                      {agents.map((a) => <AgentRow key={a.id} node={a} isEn={isEn} />)}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        /* No plan steps — flat honest activity list */
        roots.length > 0 && (
          <div data-testid="timeline-flat-activity" className="space-y-1">
            {roots.map((a) => <AgentRow key={a.id} node={a} isEn={isEn} />)}
          </div>
        )
      )}

      {/* Merged-in live activity (2026-11-02): the LiveActivityStream feed
          renders here, inside the same card — ONE section, collapsible via
          the header — instead of a second stacked card below. */}
      {activityContent && (
        <div data-testid="timeline-activity" className="mt-2 border-t border-border/60 pt-2">
          {activityContent}
        </div>
      )}

      {/* Verification + evidence — real registered data only */}
      {(verification || evidenceCount > 0) && (
        <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border/60 pt-1.5 text-[11px]">
          {verification && (
            <span
              data-testid="timeline-verification"
              className={
                verification.status === 'passed'
                  ? 'font-medium text-emerald-600 dark:text-emerald-400'
                  : verification.status === 'failed'
                    ? 'font-medium text-red-600 dark:text-red-400'
                    : 'text-muted-foreground'
              }
            >
              {isEn ? 'Verification' : '验证'}: {statusLabel(verification.status, isEn)}
            </span>
          )}
          {evidenceCount > 0 && (
            <span data-testid="timeline-evidence-count" className="text-muted-foreground">
              {isEn ? `${evidenceCount} evidence` : `${evidenceCount} 条证据`}
            </span>
          )}
        </div>
      )}

      {/* C. Advanced Execution Details — collapsed by default, preserving
          the full AgentExecutionTree (names/roles, tool invocations,
          delegation relationships, durations, peer collaboration, evidence,
          errors). */}
      {detailsContent && roots.length > 0 && (
        <details
          data-testid="technical-execution-details"
          className="mt-2 border-t border-border/60 pt-1.5"
          open={defaultDetailsOpen}
        >
          <summary className="flex cursor-pointer select-none items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground">
            <ChevronDown className="h-3 w-3" />
            {isEn ? 'Technical Execution Details' : '技术执行详情'}
          </summary>
          <div className="mt-1.5">{detailsContent}</div>
        </details>
      )}
        </>
      )}
    </div>
    </TimelineCollapseContext.Provider>
  );
}
