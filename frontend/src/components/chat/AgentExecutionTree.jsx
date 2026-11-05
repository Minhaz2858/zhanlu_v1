import { useEffect, useState } from 'react';
import {
  Loader2, Check, AlertTriangle, Clock, ShieldCheck, ShieldAlert, Lock,
  Eye, Users, ChevronRight, ChevronDown, GitBranch, Network, XCircle,
  Timer, Coins, FileSearch,
} from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { authFetch } from '@/api/authFetch';
import { attachSubagentParent, groupIntoBranches, dedupeAndSortEvents } from './LiveActivityStream';

/**
 * AgentExecutionTree — P2 Phase 3 multi-agent execution visualization.
 *
 * Reuses the existing AgentInvocation records (no new event infrastructure).
 * Renders the parent→child collaboration tree that the General Assistant
 * actually produced: supervisor delegation, parallel/sequential children,
 * specialist-to-specialist (peer) collaboration, reviewer nodes, and human
 * approval gates — all from real invocation + review + evidence data.
 *
 * Data sources (in priority order):
 *   1. `tree` prop (prebuilt — e.g. from live_events) — instant, no fetch.
 *   2. `conversationId` / `executionId` — fetched from the backend tree API
 *      (authoritative, historically restorable).
 *   3. `liveEvents` — synthesized from the streaming SSE feed when no
 *      persisted tree is available yet.
 *
 * Every displayed fact originates from backend data — never frontend guesses.
 */

// ── 12 visualization states (superset of AgentInvocation.status) ─────────────
const STATUS = {
  planning:            { icon: Clock,        color: 'text-slate-500 dark:text-slate-400',       dot: 'bg-slate-400',   label: { en: 'Planning',            zh: '规划中' } },
  queued:              { icon: Clock,        color: 'text-slate-500 dark:text-slate-400',       dot: 'bg-slate-400',   label: { en: 'Queued',              zh: '排队中' } },
  running:             { icon: Loader2,      color: 'text-blue-600 dark:text-blue-400',          dot: 'bg-blue-500',    label: { en: 'Running',             zh: '运行中' } },
  delegating:          { icon: Network,      color: 'text-indigo-600 dark:text-indigo-400',      dot: 'bg-indigo-500',  label: { en: 'Delegating',          zh: '委派中' } },
  waiting_for_child:   { icon: GitBranch,    color: 'text-cyan-600 dark:text-cyan-400',          dot: 'bg-cyan-500',    label: { en: 'Waiting for child',   zh: '等待子任务' } },
  reviewing:           { icon: Eye,          color: 'text-violet-600 dark:text-violet-400',      dot: 'bg-violet-500',  label: { en: 'Reviewing',           zh: '审查中' } },
  waiting_for_approval:{ icon: Lock,         color: 'text-amber-600 dark:text-amber-400',        dot: 'bg-amber-500',   label: { en: 'Awaiting approval',  zh: '等待审批' } },
  completed:           { icon: Check,        color: 'text-emerald-600 dark:text-emerald-400',    dot: 'bg-emerald-500', label: { en: 'Completed',           zh: '已完成' } },
  partial:             { icon: AlertTriangle,color: 'text-amber-600 dark:text-amber-400',        dot: 'bg-amber-500',   label: { en: 'Partial',             zh: '部分完成' } },
  failed:              { icon: XCircle,      color: 'text-red-600 dark:text-red-400',            dot: 'bg-red-500',     label: { en: 'Failed',              zh: '失败' } },
  blocked:             { icon: ShieldAlert,  color: 'text-red-700 dark:text-red-300',            dot: 'bg-red-700',     label: { en: 'Blocked',             zh: '已阻止' } },
  cancelled:           { icon: XCircle,      color: 'text-zinc-500 dark:text-zinc-400',          dot: 'bg-zinc-400',    label: { en: 'Cancelled',           zh: '已取消' } },
  timeout:             { icon: Timer,        color: 'text-orange-600 dark:text-orange-400',      dot: 'bg-orange-500',  label: { en: 'Timeout',             zh: '超时' } },
};

const DEFAULT_STATUS = STATUS.queued;

function statusMeta(phase) {
  return STATUS[phase] || DEFAULT_STATUS;
}

function StatusBadge({ phase, lang }) {
  const m = statusMeta(phase);
  const Icon = m.icon;
  const spinning = phase === 'running' || phase === 'delegating' || phase === 'reviewing';
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border border-border bg-secondary/40 px-1.5 py-0.5 text-[10px] font-semibold ${m.color}`}>
      <Icon className={`h-3 w-3 ${spinning ? 'animate-spin' : ''}`} />
      {m.label[lang] || m.label.en}
    </span>
  );
}

function fmtDuration(ms) {
  if (ms == null) return null;
  if (ms < 1000) return `${ms}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function fmtTokens(n) {
  if (!n) return null;
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// ── Reviewer sub-panel ───────────────────────────────────────────────────────
function ReviewerBadge({ overall, lang }) {
  const map = {
    approved:           { c: 'text-emerald-600 dark:text-emerald-400', t: { en: 'Accepted', zh: '已采纳' } },
    needs_revision:     { c: 'text-amber-600 dark:text-amber-400',    t: { en: 'Needs more evidence', zh: '需补充证据' } },
    rejected:           { c: 'text-red-600 dark:text-red-400',        t: { en: 'Rejected', zh: '已驳回' } },
  };
  const m = map[overall] || { c: 'text-slate-500', t: { en: 'Reviewed', zh: '已审查' } };
  return <span className={`text-[10px] font-medium ${m.c}`}>· {m.t[lang] || m.t.en}</span>;
}

function ReviewerPanel({ review, lang }) {
  if (!review) return null;
  const dims = review.dimensions || {};
  const dimensionLabels = {
    numerical_accuracy:   { en: 'Numerical accuracy',  zh: '数值准确性' },
    evidence_completeness:{ en: 'Evidence completeness', zh: '证据完整性' },
    source_attribution:   { en: 'Source attribution',  zh: '来源归属' },
    contradictory_findings:{ en: 'Contradictions',     zh: '矛盾发现' },
    unsupported_conclusions:{ en: 'Unsupported conclusions', zh: '无支撑结论' },
    assumptions_uncertainty:{ en: 'Assumptions',       zh: '假设与不确定性' },
    question_answered:    { en: 'Question answered',   zh: '问题回答' },
  };
  const statusColor = { ok: 'text-emerald-600 dark:text-emerald-400', warn: 'text-amber-600 dark:text-amber-400', fail: 'text-red-600 dark:text-red-400' };
  return (
    <div className="mt-2 rounded-md border border-border bg-secondary/20 p-2 text-[11px]">
      <div className="mb-1 flex items-center gap-1 font-semibold text-foreground">
        <Eye className="h-3 w-3 text-violet-500" />
        {lang === 'en' ? 'Critic / Reviewer' : '审查员 / 评审'}
        <ReviewerBadge overall={review.overall} lang={lang} />
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        {Object.entries(dims).map(([k, v]) => (
          <div key={k} className="flex items-center justify-between gap-2">
            <span className="truncate text-muted-foreground">{dimensionLabels[k]?.[lang] || k}</span>
            <span className={`font-medium ${statusColor[v] || 'text-muted-foreground'}`}>{v}</span>
          </div>
        ))}
      </div>
      {Array.isArray(review.issues) && review.issues.length > 0 && (
        <div className="mt-1 text-red-600 dark:text-red-400">{lang === 'en' ? 'Issues: ' : '问题：'}{review.issues.join('; ')}</div>
      )}
      {Array.isArray(review.needs_evidence) && review.needs_evidence.length > 0 && (
        <div className="mt-1 text-amber-600 dark:text-amber-400">{lang === 'en' ? 'Missing evidence: ' : '缺失证据：'}{review.needs_evidence.join('; ')}</div>
      )}
      {review.follow_up_count ? (
        <div className="mt-1 text-violet-600 dark:text-violet-400">{lang === 'en' ? `Requested ${review.follow_up_count} targeted investigation(s)` : `请求了 ${review.follow_up_count} 项针对性调查`}</div>
      ) : null}
    </div>
  );
}

// ── Evidence reference chip + panel ──────────────────────────────────────────
function EvidenceChip({ conversationId, evidenceId, label, onOpen }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(false);
  const { lang } = useLanguage();

  async function load() {
    setOpen((o) => !o);
    if (data || err) return;
    try {
      const res = await authFetch(`/api/executions/evidence?conversation_id=${encodeURIComponent(conversationId)}&evidence_id=${encodeURIComponent(evidenceId)}`);
      if (!res.ok) throw new Error('404');
      setData(await res.json());
    } catch {
      setErr(true);
    }
  }

  const v = data?.validation_status;
  const vColor = v === 'validated' ? 'text-emerald-600 dark:text-emerald-400'
    : v === 'contradicted' ? 'text-red-600 dark:text-red-400'
    : 'text-amber-600 dark:text-amber-400';

  return (
    <span className="inline-block align-top">
      <button
        type="button"
        onClick={load}
        className="m-0.5 inline-flex items-center gap-1 rounded border border-border bg-secondary/40 px-1.5 py-0.5 text-[10px] text-foreground hover:bg-secondary"
      >
        <FileSearch className="h-3 w-3 text-sky-500" />
        {label || evidenceId}
      </button>
      {open && (
        <span className="mt-1 block rounded-md border border-border bg-card p-2 text-[11px] leading-relaxed">
          {err ? (
            <span className="text-muted-foreground">{lang === 'en' ? 'Evidence unavailable' : '证据不可用'}</span>
          ) : data ? (
            <span className="space-y-1">
              <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Source: ' : '来源：'}</span>{data.source_id || data.datasource_id || '—'}</span>
              <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Producer: ' : '提供方：'}</span>{data.producer_agent}</span>
              <span className="block"><span className="text-muted-foreground">Validation: </span><span className={vColor}>{v || '—'}</span></span>
              {data.findings != null && <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Findings: ' : '发现：'}</span>{typeof data.findings === 'string' ? data.findings : JSON.stringify(data.findings)}</span>}
              {data.freshness?.as_of && <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'As of: ' : '时间：'}</span>{data.freshness.as_of}{data.freshness?.state ? ` (${data.freshness.state})` : ''}</span>}
              {data.query_provenance?.sql && <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Query: ' : '查询：'}</span><code className="break-all text-[10px]">{data.query_provenance.sql}</code></span>}
              {data.query_provenance?.nl && <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Intent: ' : '意图：'}</span>{data.query_provenance.nl}</span>}
              {Array.isArray(data.metric_definition_ids) && data.metric_definition_ids.length > 0 && (
                <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Metric definitions: ' : '指标定义：'}</span>{data.metric_definition_ids.join(', ')}</span>
              )}
              {Array.isArray(data.references) && data.references.length > 0 && (
                <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Built on evidence: ' : '依据证据：'}</span>{data.references.join(', ')}</span>
              )}
              {data.assumptions != null && data.assumptions !== '' && (
                <span className="block"><span className="text-muted-foreground">{lang === 'en' ? 'Assumptions: ' : '假设：'}</span>{typeof data.assumptions === 'string' ? data.assumptions : JSON.stringify(data.assumptions)}</span>
              )}
              {data.limitations != null && data.limitations !== '' && (
                <span className="block text-amber-600 dark:text-amber-400"><span className="text-muted-foreground">{lang === 'en' ? 'Limitations / missing evidence: ' : '局限/缺失证据：'}</span>{typeof data.limitations === 'string' ? data.limitations : JSON.stringify(data.limitations)}</span>
              )}
            </span>
          ) : null}
        </span>
      )}
    </span>
  );
}

// ── Recursion-safe collapsed-tree renderer (guards against bad parent links) ─
function NodeBody({ node, parentName, depth, conversationId, lang }) {
  const meta = statusMeta(node.phase || node.status);
  const Icon = meta.icon;
  const spinning = ['running', 'delegating', 'reviewing'].includes(node.phase || node.status);
  const isPeer = node.delegation_mode === 'peer';
  const isReviewer = node.role === 'supervisor' && (node.review || node.invocation_type === 'reviewer');
  const awaiting = node.awaiting_approval;

  const [expanded, setExpanded] = useState(depth < 1);
  const hasChildren = Array.isArray(node.children) && node.children.length > 0;

  return (
    <div className="relative">
      <div className="flex items-start gap-2 py-1">
        {/* connector + node */}
        <div className="flex flex-col items-center pt-1">
          <span className={`h-2.5 w-2.5 rounded-full ${meta.dot}`} />
          {hasChildren && <span className={`w-px flex-1 ${isPeer ? 'border-l border-dashed border-indigo-400/60' : 'border-l border-border'}`} />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium text-foreground">{node.agent_name}</span>
            {node.role === 'supervisor' && <span className="rounded bg-secondary px-1 text-[9px] text-muted-foreground">{lang === 'en' ? 'Supervisor' : '主管'}</span>}
            {isPeer && (
              <span className="inline-flex items-center gap-0.5 rounded bg-indigo-500/10 px-1 text-[9px] text-indigo-600 dark:text-indigo-300">
                <Users className="h-2.5 w-2.5" />{lang === 'en' ? 'Peer' : '协作'}
              </span>
            )}
            <StatusBadge phase={node.phase || node.status} lang={lang} />
            {isReviewer && <span className="inline-flex items-center gap-0.5 rounded bg-violet-500/10 px-1 text-[9px] text-violet-600 dark:text-violet-300"><Eye className="h-2.5 w-2.5" />{lang === 'en' ? 'Review' : '审查'}</span>}
            {awaiting && <span className="inline-flex items-center gap-0.5 rounded bg-amber-500/10 px-1 text-[9px] text-amber-600 dark:text-amber-300"><Lock className="h-2.5 w-2.5" />{lang === 'en' ? 'Approval' : '审批'}</span>}
            {node.block_reason && <span className="inline-flex items-center gap-0.5 rounded bg-red-500/10 px-1 text-[9px] text-red-600 dark:text-red-300"><ShieldAlert className="h-2.5 w-2.5" />{lang === 'en' ? 'Blocked' : '已阻止'}</span>}
          </div>
          {node.task && <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{node.task}</div>}
          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
            {fmtDuration(node.duration_ms) && <span className="inline-flex items-center gap-0.5"><Timer className="h-2.5 w-2.5" />{fmtDuration(node.duration_ms)}</span>}
            {fmtTokens(node.token_usage?.total_tokens) && <span className="inline-flex items-center gap-0.5"><Coins className="h-2.5 w-2.5" />{fmtTokens(node.token_usage?.total_tokens)} tok</span>}
            {node.cost_amount != null && node.cost_amount > 0 && <span>${Number(node.cost_amount).toFixed(4)}</span>}
          </div>

          {/* Collapsible detail */}
          <button type="button" onClick={() => setExpanded((e) => !e)} className="mt-1 inline-flex items-center gap-0.5 text-[10px] text-sky-600 dark:text-sky-400 hover:underline">
            {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {lang === 'en' ? 'Details' : '详情'}
          </button>
          {expanded && (
            <div className="mt-1 space-y-1 rounded-md border border-border bg-secondary/20 p-2 text-[11px]">
              {parentName && <div><span className="text-muted-foreground">{lang === 'en' ? 'Parent: ' : '上级：'}</span>{parentName}</div>}
              {hasChildren && <div><span className="text-muted-foreground">{lang === 'en' ? 'Children: ' : '子任务：'}</span>{node.children.length}</div>}
              {Array.isArray(node.tools) && node.tools.length > 0 && (
                <div><span className="text-muted-foreground">{lang === 'en' ? 'Tools: ' : '工具：'}</span>{node.tools.join(', ')}</div>
              )}
              {node.error_message && <div className="text-red-600 dark:text-red-400">{lang === 'en' ? 'Error: ' : '错误：'}{node.error_message}</div>}
              {node.block_reason && <div className="text-red-600 dark:text-red-400">{lang === 'en' ? 'Blocked: ' : '阻止原因：'}{node.block_reason}</div>}
              {Array.isArray(node.evidence_ids) && node.evidence_ids.length > 0 && (
                <div>
                  <div className="mb-0.5 text-muted-foreground">{lang === 'en' ? 'Evidence:' : '证据：'}</div>
                  {node.evidence_ids.map((eid) => (
                    <EvidenceChip key={eid} conversationId={conversationId} evidenceId={eid} label={eid.slice(0, 8)} />
                  ))}
                </div>
              )}
              <ReviewerPanel review={node.review} lang={lang} />
            </div>
          )}
        </div>
      </div>
      {hasChildren && (
        <div className="ml-3 border-l border-border pl-2">
          {node.children.map((child, i) => (
            <NodeBody key={child.id || i} node={child} parentName={node.agent_name} depth={depth + 1} conversationId={conversationId} lang={lang} />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Compact summary (by default) ─────────────────────────────────────────────
function ExecutionSummary({ summary, lang }) {
  if (!summary) return null;
  const items = [
    [lang === 'en' ? 'Agents' : 'Agent 数', summary.total],
    [lang === 'en' ? 'Completed' : '完成', summary.completed],
    ...(summary.running > 0 ? [[lang === 'en' ? 'Running' : '运行中', summary.running]] : []),
    [lang === 'en' ? 'Partial' : '部分', summary.partial],
    [lang === 'en' ? 'Failed' : '失败', summary.failed],
    [lang === 'en' ? 'Peer collab' : '协作', summary.peer_collaborations],
    [lang === 'en' ? 'Evidence' : '证据', summary.evidence_count],
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-secondary/20 px-2 py-1 text-[11px]">
      {items.map(([k, v]) => (
        <span key={k} className="text-muted-foreground">{k}: <span className="font-semibold text-foreground">{v ?? 0}</span></span>
      ))}
      {summary.duration_ms != null && <span className="text-muted-foreground">{lang === 'en' ? 'Duration' : '时长'}: <span className="font-semibold text-foreground">{fmtDuration(summary.duration_ms)}</span></span>}
      {summary.total_tokens ? <span className="text-muted-foreground">{lang === 'en' ? 'Tokens' : 'Token'}: <span className="font-semibold text-foreground">{fmtTokens(summary.total_tokens)}</span></span> : null}
      {summary.estimated_cost ? <span className="text-muted-foreground">{lang === 'en' ? 'Cost' : '成本'}: <span className="font-semibold text-foreground">${Number(summary.estimated_cost).toFixed(4)}</span></span> : null}
      {summary.reviewer_status && <span className="text-muted-foreground">{lang === 'en' ? 'Review' : '审查'}: <span className="font-semibold"><ReviewerBadge overall={summary.reviewer_status} lang={lang} /></span></span>}
    </div>
  );
}

/**
 * Summarize a node list the same way the backend `_summarize` does, so the
 * live-synthesized tree and the authoritative REST tree report identical
 * counters FROM THE SAME DATASET (chat-experience repair Part C rule 7).
 */
export function summarizeNodes(roots) {
  const s = {
    total: 0, completed: 0, partial: 0, failed: 0,
    peer_collaborations: 0, evidence_count: 0,
  };
  const evidence = new Set();
  const walk = (nodes) => {
    for (const n of nodes || []) {
      // Multi-agent milestone (2026-11-01): tool rows under a specialist
      // are NOT agents — never counted in the agent metrics (a tool call
      // is not an agent execution). They stay fully visible in the tree.
      if (n.is_tool) continue;
      s.total += 1;
      const st = n.status || n.phase;
      if (st === 'completed') s.completed += 1;
      else if (st === 'running') s.running = (s.running || 0) + 1;
      else if (st === 'partial') s.partial += 1;
      else if (['failed', 'timeout', 'cancelled', 'blocked'].includes(st)) s.failed += 1;
      if (n.delegation_mode === 'peer') s.peer_collaborations += 1;
      for (const e of n.evidence_ids || []) evidence.add(e);
      walk(n.children);
    }
  };
  walk(roots);
  s.evidence_count = evidence.size;
  return s;
}

/**
 * Synthesize a tree from a streaming SSE feed when no persisted tree exists.
 *
 * Chat-experience repair (Part C) — rules enforced here:
 *   1. A node exists ONLY for a real `subagent_invoked` event. Standalone
 *      branches (phase_enter, tool rows, plan metadata) are NEVER turned
 *      into synthetic "sub-agent" nodes (the old code rendered one Running
 *      node per unrelated event — the "4 sub-agents, 3 events" defect).
 *   2. Real identity whenever available: agent_name (from the backend call
 *      args) → agent_label → target. The generic "sub-agent" literal is
 *      gone; the delegation tool name is the honest last resort.
 *   3. A terminal state is guaranteed: matched `subagent_returned` →
 *      completed (or cancelled when outcome=interrupted); an unmatched
 *      invoked event at stream end → cancelled, NEVER Running.
 *   4. The summary is computed from the same normalized nodes.
 */
export function buildTreeFromLiveEvents(liveEvents, opts = {}) {
  if (!Array.isArray(liveEvents) || liveEvents.length === 0) return null;
  const deduped = dedupeAndSortEvents(liveEvents);

  // ── Structured agent-execution events (multi-agent milestone) ──────────
  // agent_started / agent_tool_started / agent_tool_completed /
  // agent_completed / agent_failed — emitted by the backend when a REAL
  // specialist runs (delegate_task children, the Data Agent). Nodes are
  // keyed by agent_execution_id (the same id the persisted tree uses), so
  // live cards and the post-turn REST tree dedupe onto one identity.
  // Peer collaboration is set from the backend's delegation_mode — a REAL
  // peer exchange, never inferred. Tools render under the agent that
  // invoked them and are excluded from the agent metrics.
  const agentNodes = new Map();
  // ── Supervisor verification stage (milestone §3.2) ────────────────────
  // The supervisor's verification verdict uses the EXISTING live events:
  // started = `phase_enter.verify`, completed = `verify_passed` /
  // `verify_failed` (the honest per-tool verdict gate). Surfaced here so
  // the panel shows the verification stage live — no duplicate event
  // system.
  let verification = null;
  for (const e of deduped) {
    const kw = e.keyword || e.label_key || '';
    if (kw === 'phase_enter.verify') {
      verification = { status: 'running' };
    } else if (e.type === 'verify_passed') {
      verification = {
        status: 'passed',
        grounded_by_data: e.params?.grounded_by_data,
        failed_steps: e.params?.failed_steps || [],
      };
    } else if (e.type === 'verify_failed') {
      verification = {
        status: 'failed',
        grounded_by_data: e.params?.grounded_by_data,
        failed_steps: e.params?.failed_steps || [],
      };
    }
  }
  for (const e of deduped) {
    if (e.type !== 'agent_started') continue;
    const p = e.params || {};
    const aex = p.agent_execution_id;
    if (!aex || agentNodes.has(aex)) continue;
    agentNodes.set(aex, {
      id: aex,
      agent_name: p.agent_label || p.agent_name || 'specialist',
      role: 'specialist',
      delegation_mode: p.delegation_mode || 'supervisor',
      status: 'running',
      phase: 'running',
      task: p.task || '',
      children: [],
      evidence_ids: [],
      started_at: e.ts,
    });
  }
  if (agentNodes.size > 0) {
    const toolRows = new Map();
    for (const e of deduped) {
      const p = e.params || {};
      const aex = p.agent_execution_id;
      const node = aex && agentNodes.get(aex);
      if (!node) continue;
      if (e.type === 'agent_completed' || e.type === 'agent_failed') {
        const st = e.type === 'agent_failed'
          ? 'failed'
          : (p.status === 'partial' ? 'partial' : 'completed');
        node.status = st;
        node.phase = st;
        if (p.duration_ms != null) node.duration_ms = p.duration_ms;
        if (p.error) node.error_message = p.error;
        if (Array.isArray(p.evidence_ids) && p.evidence_ids.length) {
          node.evidence_ids = p.evidence_ids;
        } else if (p.evidence_count > 0) {
          node.evidence_ids = Array.from(
            { length: p.evidence_count }, (_, i) => `${aex}-ev-${i}`);
        }
      } else if (e.type === 'agent_tool_started') {
        const key = `${aex}::${p.tool_name}`;
        const row = {
          id: `tool-${key}`,
          agent_name: p.tool_name,
          role: 'specialist',
          is_tool: true,
          status: 'running',
          phase: 'running',
          task: p.tool_name,
          children: [],
        };
        toolRows.set(key, row);
        node.children.push(row);
      } else if (e.type === 'agent_tool_completed') {
        const key = `${aex}::${p.tool_name}`;
        const st = p.status || 'completed';
        const row = toolRows.get(key);
        if (row) {
          row.status = st;
          row.phase = st;
          if (p.duration_ms != null) row.duration_ms = p.duration_ms;
        } else {
          // Terminal without a seen start (dropped frame) — still render.
          node.children.push({
            id: `tool-${key}`,
            agent_name: p.tool_name,
            role: 'specialist',
            is_tool: true,
            status: st,
            phase: st,
            task: p.tool_name,
            children: [],
            duration_ms: p.duration_ms,
          });
        }
      }
    }
    // Nest specialists under their parents (nested delegation); assemble roots.
    const roots = [];
    for (const [aex, node] of agentNodes) {
      const startEv = deduped.find(
        (e) => e.type === 'agent_started' && e.params?.agent_execution_id === aex);
      const pid = startEv?.params?.parent_agent_execution_id;
      if (pid && agentNodes.has(pid)) agentNodes.get(pid).children.push(node);
      else roots.push(node);
    }
    // Stream ended without a terminal event → honest cancelled state,
    // never a permanent Running indicator (acceptance §5.11).
    if (opts.finalize) {
      for (const node of agentNodes.values()) {
        if (node.status === 'running') { node.status = 'cancelled'; node.phase = 'cancelled'; }
        for (const c of node.children) {
          if (c.status === 'running') { c.status = 'cancelled'; c.phase = 'cancelled'; }
        }
      }
      // Verification event consistency: a stream that saw
      // `phase_enter.verify` but died before a verdict must NOT show a
      // permanent "running" verification — it honestly never completed.
      if (verification && verification.status === 'running') {
        verification = { ...verification, status: 'not_run' };
      }
    }
    return { roots, summary: summarizeNodes(roots), verification };
  }

  const withParent = attachSubagentParent(deduped);
  const branches = groupIntoBranches(withParent);
  const nodesBySubagent = new Map();
  const roots = [];
  for (const b of branches) {
    const parent = b.parent || {};
    // RULE 1: only real delegations become nodes.
    if (parent.type !== 'subagent_invoked') continue;
    const sub = parent.params || {};
    const subId = sub.subagent_id;
    const identity = sub.agent_name || sub.agent_label || sub.target || 'delegation';
    const children = (b.children || [])
      .filter((c) => c.type !== 'subagent_returned')
      .map((c) => ({
        id: c.id || `${c.type}-${subId || identity}`,
        agent_name: c.params?.tool_label || c.params?.tool_name || c.type,
        role: 'specialist',
        is_tool: true,
        status: c.type === 'tool_call_failed' ? 'failed' : 'completed',
        phase: c.type === 'tool_call_failed' ? 'failed' : 'completed',
        task: c.params?.tool_label || '',
        children: [],
      }));
    // RULE 3: deterministic terminal reconciliation.
    const returned = deduped.find(
      (e) => e.type === 'subagent_returned'
        && (subId
          ? e.params?.subagent_id === subId
          : (e.params?.agent_label === sub.agent_label
              || e.params?.target === sub.target)),
    );
    const interrupted = returned?.params?.outcome === 'interrupted';
    const status = returned ? (interrupted ? 'cancelled' : 'completed') : 'cancelled';
    const node = {
      id: subId || `sub-${identity}`,
      agent_name: identity,
      role: 'specialist',
      delegation_mode: 'supervisor',
      status,
      phase: status,
      task: sub.task || sub.agent_label || '',
      children,
    };
    if (subId && !nodesBySubagent.has(subId)) {
      nodesBySubagent.set(subId, node);
      roots.push(node);
    } else if (!subId) {
      roots.push(node);
    }
  }
  if (roots.length === 0) return null;
  // RULE 4: summary derived from the same nodes that are rendered.
  return { roots, summary: summarizeNodes(roots) };
}

export default function AgentExecutionTree({ conversationId, executionId, liveEvents, turnStartedAt, tree: treeProp, streaming = false }) {
  const { lang } = useLanguage();
  const [fetched, setFetched] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // Multi-agent milestone: no REST fetch while the turn is streaming —
    // the live feed drives the panel; a premature (empty) fetched tree must
    // not shadow it. The authoritative fetch runs once streaming is over.
    if (streaming || treeProp || (!conversationId && !executionId)) return;
    let cancelled = false;
    setLoading(true);
    // Consistency audit D1: anchor the tree to THIS turn. The anchor is the
    // message's first persisted live-event timestamp (SERVER clock — the
    // same clock the invocation rows use), with a small slack so the turn's
    // own supervisor row (started microseconds before the first event) is
    // always included. Without an anchor the view stays conversation-wide.
    let anchor = null;
    const t = turnStartedAt ? new Date(turnStartedAt).getTime() : NaN;
    if (!Number.isNaN(t)) {
      anchor = new Date(t - 5000).toISOString();
    }
    const url = executionId
      ? `/api/executions/${executionId}/agent-tree`
      : `/api/executions/tree?conversation_id=${encodeURIComponent(conversationId)}${anchor ? `&started_after=${encodeURIComponent(anchor)}` : ''}`;
    authFetch(url)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => { if (!cancelled) setFetched(data); })
      .catch(() => { if (!cancelled) setFetched(null); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [conversationId, executionId, treeProp, turnStartedAt, streaming]);

  // Multi-agent milestone: while streaming, the live feed wins — the panel
  // must reflect in-flight reality, not a stale or empty persisted tree.
  const liveTree = liveEvents
    ? buildTreeFromLiveEvents(liveEvents, { finalize: !streaming })
    : null;
  const tree = streaming
    ? (treeProp || liveTree)
    : (treeProp || fetched || liveTree);
  if (!tree || !Array.isArray(tree.roots) || tree.roots.length === 0) return null;

  return (
    <div data-testid="agent-execution-tree" className="mt-2 rounded-lg border border-border bg-card p-2">
      <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold text-foreground">
        <Network className="h-3.5 w-3.5 text-indigo-500" />
        {lang === 'en' ? 'Agent Execution' : 'Agent 执行'}
      </div>
      <ExecutionSummary summary={tree.summary} lang={lang} />
      {tree.verification && (
        <div
          data-testid="agent-verification-badge"
          className={
            'mt-1 inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] ' +
            (tree.verification.status === 'passed'
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600'
              : tree.verification.status === 'failed'
                ? 'border-red-500/30 bg-red-500/10 text-red-600'
                : 'border-border bg-secondary/20 text-muted-foreground')
          }
        >
          <ShieldCheck className="h-3 w-3" />
          {lang === 'en' ? 'Verification' : '验证'}:{' '}
          {tree.verification.status === 'passed'
            ? (lang === 'en' ? 'passed' : '通过')
            : tree.verification.status === 'failed'
              ? (lang === 'en' ? 'failed' : '未通过')
              : tree.verification.status === 'not_run'
                ? (lang === 'en' ? 'not completed' : '未完成')
                : (lang === 'en' ? 'running' : '进行中')}
          {tree.verification.status === 'failed' && Array.isArray(tree.verification.failed_steps) && tree.verification.failed_steps.length > 0 && (
            <span className="opacity-80"> — {tree.verification.failed_steps.slice(0, 3).join(', ')}</span>
          )}
        </div>
      )}
      <div className="mt-1">
        {tree.roots.map((root, i) => (
          <NodeBody key={root.id || i} node={root} parentName={null} depth={0} conversationId={conversationId} lang={lang} />
        ))}
      </div>
    </div>
  );
}
