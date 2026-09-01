/**
 * LiveActivityStream — inline, per-message live activity feed.
 *
 * Replaces the legacy numbered `ActivitySteps` inside every assistant message
 * bubble with a continuous, typed stream of events that feels alive
 * (Kimi / Claude-style). The backend emits structured containers
 * ``{type, label_key, params, ts}`` over SSE; this component renders them
 * with per-type visuals, auto-scrolls while the run is live, and auto-collapses
 * to a one-line summary once all events are terminal.
 *
 * Richness additions (2026-08-22):
 *   - Sub-agent delegation badge (subagent_invoked/returned)
 *   - Inline 3-row × 5-col data preview attached to a finished data-fetching tool
 *   - Plan summary card with the structured step list
 *   - Live duration ticker on running rows (1s updates)
 *   - Retry lineage indent (children of a failed step, capped at depth 2)
 *   - Sticky running row highlight (most recent running row gets a left-border accent)
 *
 * Bug-fix history: this component used to be rendered conditionally on whether
 * `live_events` was populated, falling back to `ActivitySteps` for legacy turns.
 * The legacy adapter (`LiveActivityStream.legacyAdapter.js`) now synthesizes
 * `live_events` from `activity_steps` so every assistant message — old or new —
 * renders through this one component with one visual design.
 *
 * Props:
 *   events       — array of { type, label_key, params, ts } (preferred)
 *   legacySteps  — array of legacy `activity_step` objects; synthesized when
 *                  `events` is empty (one-way upgrade from legacy data)
 *   phase        — legacy { state, verb, title } headline fallback (optional)
 *   reasoning    — model chain-of-thought (optional)
 *   isStreaming  — true while the SSE turn is still live
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Sparkles, Loader2, Check, ShieldCheck, AlertTriangle, XCircle,
  ListChecks, ChevronDown, ChevronUp, Brain, Timer, Rows3, FileStack,
  Plug, PlugZap, Database, ClipboardList, Search, Table2,
} from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { pickEvents } from './LiveActivityStream.legacyAdapter';

// ── Helpers ────────────────────────────────────────────────────────────

/** Tool name → short action label for the live one-liner (hybrid mode). */
const TOOL_ACTION_LABELS = {
  execute_query: 'Running query…',
  execute_sql: 'Running query…',
  run_sql: 'Running query…',
  query_database: 'Running query…',
  create_artifact: 'Building artifact…',
  create_report: 'Building report…',
  generate_report: 'Building report…',
  create_dashboard: 'Building dashboard…',
  create_automation: 'Creating automation…',
  execute_code: 'Running code…',
  web_search: 'Searching…',
  _web_search: 'Searching…',
  tavily_search: 'Searching…',
  ask_rag_research: 'Researching…',
  rag_research: 'Researching…',
  create_cad: 'Building CAD model…',
  delegate_task: 'Delegating to sub-agent…',
};

/** Substitute {placeholders} from structured params into a template string. */
function formatTemplate(template, params = {}) {
  return String(template || '').replace(/\{(\w+)\}/g, (_, key) =>
    params[key] !== undefined && params[key] !== null ? String(params[key]) : '',
  );
}

/**
 * Merge tool_call_started/finished pairs into single rows so the feed shows
 * one row per tool call that flips spinner → checkmark instead of two rows.
 * Terminal started events that never got a finish stay "running".
 *
 * Also: pair subagent_invoked ↔ subagent_returned, and attach a data_offer
 * (inline preview) to the matching tool_call_finished row so the preview
 * toggle can render below the same row.
 */

// 2026-08-25: helper that pairs tool_call_started with its parent
// subagent_invoked. The backend emits subagent_id on the invoked
// event and parent_subagent_id on the tool event. This helper sets
// the same parent_subagent_id on the tool row by matching the open
// subagent's `target` to the tool's `tool_label`.
//
// This is exported so the rendering layer can read the field directly
// (parent_subagent_id) without re-running the matching logic.
export function attachSubagentParent(rows) {
  const out = Array.isArray(rows) ? [...rows] : [];
  const openSubagents = []; // stack of {id, target}
  for (const row of out) {
    if (!row || typeof row !== 'object') continue;
    if (row.type === 'subagent_invoked') {
      openSubagents.push({
        id: row.params?.subagent_id,
        target: row.params?.target,
      });
    } else if (row.type === 'subagent_returned') {
      // Pop matching subagent (most recent)
      const target = row.params?.agent_label;
      for (let i = openSubagents.length - 1; i >= 0; i--) {
        if (openSubagents[i].target === target) {
          openSubagents.splice(i, 1);
          break;
        }
      }
    } else if (
      row.type === 'tool_call_started' ||
      row.type === 'tool_call_finished' ||
      row.type === 'tool_call_failed'
    ) {
      const toolLabel = row.params?.tool_label;
      // Find the most recent open subagent with matching target
      for (let i = openSubagents.length - 1; i >= 0; i--) {
        if (openSubagents[i].target === toolLabel && openSubagents[i].id) {
          row.parent_subagent_id = openSubagents[i].id;
          break;
        }
      }
    }
  }
  return out;
}

// 2026-08-25: nestRows() computes the visual indent level for each row.
// Rows with a parent_subagent_id get indent 1 (rendered as a child of
// the matching subagent row). All other rows are top-level (indent 0).
// Returns a new array; does not mutate the input.
export function nestRows(rows) {
  const out = Array.isArray(rows) ? rows.map((r) => ({ ...r })) : [];
  for (const row of out) {
    if (!row || typeof row !== 'object') continue;
    row.indent = row.parent_subagent_id ? 1 : 0;
  }
  return out;
}

// 2026-08-25: groupIntoBranches() groups child rows under their parent
// subagent row. Returns a list of branches, each with a `parent` row and
// a `children` array. Standalone rows (no parent) become a branch with
// empty children. Preserves original order.
//
// Behavior:
//   - subagent_invoked: opens a new branch, registered by subagent_id
//   - tool_call_* with parent_subagent_id: child of the matching branch
//   - subagent_returned: attached as the LAST child of the matching
//     open subagent branch (closes the delegation visually)
//   - all other rows: standalone branches
export function groupIntoBranches(rows) {
  const out = [];
  const branchBySubagent = new Map(); // subagent_id → branch index
  // Most recent open subagent id per target, so we can close the right
  // branch on subagent_returned.
  const openByTarget = new Map();
  for (const row of rows || []) {
    if (!row || typeof row !== 'object') continue;
    // 1. Child of a registered branch (by parent_subagent_id)
    if (row.parent_subagent_id && branchBySubagent.has(row.parent_subagent_id)) {
      const idx = branchBySubagent.get(row.parent_subagent_id);
      out[idx].children.push(row);
      continue;
    }
    // 2. subagent_returned: close the matching open subagent branch
    if (row.type === 'subagent_returned') {
      const target = row.params?.agent_label;
      const subId = target ? openByTarget.get(target) : null;
      if (subId && branchBySubagent.has(subId)) {
        const idx = branchBySubagent.get(subId);
        out[idx].children.push(row);
        openByTarget.delete(target);
        continue;
      }
    }
    // 3. New top-level branch
    const branch = { parent: row, children: [] };
    out.push(branch);
    if (row.type === 'subagent_invoked' && row.params?.subagent_id) {
      branchBySubagent.set(row.params.subagent_id, out.length - 1);
      const target = row.params?.target || row.params?.agent_label;
      if (target) {
        openByTarget.set(target, row.params.subagent_id);
      }
    }
  }
  return out;
}

// 2026-08-25: computeProgress() derives the ProgressBar state from the
// event list. No new SSE events — everything is computed client-side.
//
// Returns:
//   {
//     status: 'idle' | 'running' | 'completed' | 'failed',
//     totalEvents: number,
//     completedEvents: number,
//     currentPhase: string,
//     estimatedRemainingMs: number,
//     fillPct: number,  // 0-100
//   }
export function computeProgress(events) {
  const list = Array.isArray(events) ? events : [];
  if (list.length === 0) {
    return {
      status: 'idle',
      totalEvents: 0,
      completedEvents: 0,
      currentPhase: '',
      estimatedRemainingMs: 0,
      fillPct: 0,
    };
  }

  const completed = list.filter((e) => e && (e.status === 'done' || e.status === 'completed'));
  const running = list.filter((e) => e && e.status === 'running');
  const failed = list.filter((e) => e && e.status === 'failed');

  let status = 'running';
  if (failed.length > 0) status = 'failed';
  else if (running.length === 0 && completed.length > 0) status = 'completed';
  else if (running.length === 0) status = 'idle';

  // Estimate remaining: average duration of completed events * running count
  let estimatedRemainingMs = 0;
  if (running.length > 0) {
    const durations = completed
      .map((e) => (typeof e.duration === 'number' ? e.duration : null))
      .filter((d) => d != null && d > 0);
    if (durations.length > 0) {
      const avg = durations.reduce((s, d) => s + d, 0) / durations.length;
      estimatedRemainingMs = Math.round(avg * running.length * 1000);
    } else {
      // Default 30s per running event
      estimatedRemainingMs = running.length * 30_000;
    }
  }

  // Current phase: most recent phase_enter event
  let currentPhase = '';
  for (let i = list.length - 1; i >= 0; i--) {
    const e = list[i];
    if (e && e.type && String(e.type).startsWith('phase_enter')) {
      currentPhase = e.label_key || e.params?.label || e.params?.label_key || e.type;
      break;
    }
  }

  const totalEvents = list.length;
  const completedEvents = completed.length;
  const fillPct = totalEvents > 0 ? Math.round((completedEvents / totalEvents) * 100) : 0;

  return {
    status,
    totalEvents,
    completedEvents,
    currentPhase,
    estimatedRemainingMs,
    fillPct,
  };
}

// 2026-08-25: computeCollapseStates() — auto-collapse done events when
// a newer event is running. When the turn ends (no running events),
// all events expand back so the user can review.
//
// Returns a new array; does not mutate the input.
//
// Rule: a row is collapsed when
//   - its status is 'done' or 'failed'
//   - AND at least one NEWER row has status 'running'
//
// Manual overrides (manuallyExpanded) are layered on top by the
// render layer.
export function computeCollapseStates(rows) {
  const out = Array.isArray(rows) ? rows.map((r) => ({ ...r })) : [];
  const hasRunning = out.some((r) => r && r.status === 'running');
  if (!hasRunning) {
    for (const r of out) {
      if (r && typeof r === 'object') r.collapsed = false;
    }
    return out;
  }
  // Find the index of the LAST running event
  let lastRunningIdx = -1;
  for (let i = out.length - 1; i >= 0; i--) {
    if (out[i] && out[i].status === 'running') {
      lastRunningIdx = i;
      break;
    }
  }
  // Collapse done/failed events that come BEFORE the last running event
  for (let i = 0; i < out.length; i++) {
    const r = out[i];
    if (!r) continue;
    if (i < lastRunningIdx && (r.status === 'done' || r.status === 'failed')) {
      r.collapsed = true;
    } else {
      r.collapsed = false;
    }
  }
  return out;
}

// 2026-08-25: collapsedChipSummary() — extract a single-line summary
// for the CollapsedChip view. Examples:
//   "✓ ask_data_agent · 131s · 12 rows"
//   "✗ ask_data_agent · failed"
//   "✓ Fathoming your request"
export function collapsedChipSummary(ev) {
  if (!ev || typeof ev !== 'object') return '';
  const isFail = ev.status === 'failed';
  const symbol = isFail ? '✗' : '✓';
  const type = ev.type;
  const params = ev.params || {};

  // Try the most informative label first
  let label = params.tool_label || params.agent_label || params.label || params.label_key || '';
  if (!label && type.startsWith('phase_enter')) {
    label = String(type).replace('phase_enter.', '');
  }
  if (!label) label = type;

  const parts = [`${symbol} ${label}`];
  const dur = params.duration;
  if (typeof dur === 'number' && dur > 0) {
    parts.push(`${Math.round(dur)}s`);
  }
  if (typeof params.row_count === 'number' && params.row_count > 0) {
    parts.push(`${params.row_count} rows`);
  }
  if (isFail && !parts.some((p) => /fail/i.test(p))) {
    parts.push('failed');
  }
  return parts.join(' · ');
}

function mergeEvents(events) {
  const out = [];
  const openStarted = []; // stack of tool row indices awaiting finish
  const openInvoked = []; // stack of subagent row indices awaiting return
  for (const ev of events || []) {
    if (ev.type === 'tool_call_started') {
      out.push({ ...ev, status: 'running' });
      openStarted.push(out.length - 1);
    } else if (ev.type === 'tool_call_finished' || ev.type === 'tool_call_failed') {
      const idx = openStarted.pop();
      if (idx != null) {
        out[idx] = {
          ...out[idx],
          ...ev,
          status: ev.type === 'tool_call_finished' ? 'done' : 'failed',
        };
        // A data_offer may follow in the stream — wait until next iter to attach.
      } else {
        out.push({
          ...ev,
          status: ev.type === 'tool_call_finished' ? 'done' : 'failed',
        });
      }
    } else if (ev.type === 'subagent_invoked') {
      out.push({ ...ev, status: 'running' });
      openInvoked.push(out.length - 1);
    } else if (ev.type === 'subagent_returned') {
      const idx = openInvoked.pop();
      if (idx != null) {
        // Preserve the invoked row's params (target badge) and append the
        // returned row's metadata (duration, row_count) into them so the
        // merged row carries both halves of the delegation lifecycle.
        out[idx] = {
          ...out[idx],
          ...ev,
          params: { ...(out[idx].params || {}), ...(ev.params || {}) },
          status: 'done',
        };
      } else {
        out.push({ ...ev, status: 'done' });
      }
    } else if (ev.type === 'data_offer') {
      // Attach inline preview to the most recent matching tool row (last
      // finished/failed) — the offer always trails its tool_call_finished.
      for (let i = out.length - 1; i >= 0; i--) {
        const row = out[i];
        if (
          (row.type === 'tool_call_finished' || row.type === 'tool_call_failed') &&
          (row.params?.tool_label === ev.params?.tool_label || true) // best-effort match
        ) {
          out[i] = { ...row, data_offer: ev };
          break;
        }
      }
      // The offer is metadata for the previous row; do not push a new row.
    } else if (ev.type === 'finalize_started' || ev.type === 'artifact_progress') {
      out.push({ ...ev, status: 'running' });
    } else if (ev.type === 'plan_step_added' || ev.type === 'plan_step_completed') {
      // Plan checklist metadata — rendered by PlanChecklist, not as a row.
      // Skipped here so persisted live_events don't double-render the plan
      // as both a checklist and a generic feed row.
    } else if (ev.type === 'finalize_done') {
      // Completion marker for the finalize phase: flip EVERY still-running
      // finalize_started / artifact_progress row to done (the backend can
      // emit multiple finalize-phase rows). finalize_done is pure metadata —
      // do not push a new row.
      for (let i = out.length - 1; i >= 0; i--) {
        if (out[i].type === 'finalize_started' || out[i].type === 'artifact_progress') {
          out[i] = { ...out[i], status: 'done' };
        }
      }
    } else {
      out.push({ ...ev, status: 'done' });
    }
  }
  return out;
}

/**
 * Compute indent depth for retry lineage: a `retry` event opens a depth-N
 * indent for subsequent rows until the next non-retry terminal row closes
 * the lineage. Capped at depth 2 visually.
 */
function applyRetryIndent(rows) {
  const out = [];
  let depth = 0;
  for (const row of rows) {
    if (row.type === 'retry') {
      depth = Math.min(depth + 1, 2);
      out.push({ ...row, indent: depth });
    } else if (depth > 0 && (row.type === 'tool_call_started' || row.type === 'tool_call_finished' || row.type === 'tool_call_failed')) {
      out.push({ ...row, indent: depth });
      if (row.status !== 'running') depth = 0;
    } else if (row.type === 'tool_call_failed') {
      out.push({ ...row, indent: 0 });
    } else {
      out.push({ ...row, indent: 0 });
    }
  }
  return out;
}

/** Resolve a human label for an event from the localized template map. */
function resolveLabel(ev, le) {
  const p = ev.params || {};
  if (ev.type === 'phase_enter') {
    const state = String(ev.label_key || '').replace('phase_enter.', '') || 'working';
    return le.phase_enter?.[state] || ev.label_key || 'Working';
  }
  let tmpl = le[ev.label_key];
  if (ev.label_key === 'tool_call_finished' && !('row_count' in p)) {
    tmpl = le.tool_call_finished_short;
  }
  if (!tmpl) tmpl = ev.label_key || ev.type;
  return formatTemplate(tmpl, p);
}

/** Status metadata rendered on the right of each row. */
function eventMeta(ev, isEn) {
  const p = ev.params || {};
  const bits = [];
  if (ev.type === 'tool_call_finished' || ev.type === 'tool_call_failed' || ev.type === 'subagent_returned') {
    if (typeof p.duration === 'number') bits.push(`${p.duration}s`);
    if (typeof p.row_count === 'number') bits.push(`${p.row_count} ${isEn ? 'rows' : '行'}`);
  }
  if (ev.type === 'plan_preview' && typeof p.n === 'number') {
    bits.push(`${p.n} ${isEn ? 'steps' : '步'}`);
  }
  if (ev.type === 'plan_summary' && typeof p.n === 'number') {
    bits.push(`${p.n} ${isEn ? 'steps' : '步'}`);
  }
  return bits.join(' · ');
}

// ── Live duration ticker (mounted only on running rows) ────────────────

function RunningTimer({ startTs }) {
  const [, force] = useState(0);
  useEffect(() => {
    if (!startTs) return undefined;
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [startTs]);
  if (!startTs) return null;
  const seconds = Math.max(0, Math.round((Date.now() - new Date(startTs).getTime()) / 1000));
  return <span className="text-[10px] tabular-nums text-muted-foreground">{seconds}s</span>;
}

// ── Inline data preview pane ───────────────────────────────────────────

function DataPreviewPane({ offer, isEn }) {
  const [open, setOpen] = useState(false);
  const sampleRows = offer?.params?.sample_rows;
  const cols = offer?.params?.columns || [];
  if (!offer || !Array.isArray(sampleRows) || sampleRows.length === 0) return null;
  const rc = typeof offer?.params?.row_count === 'number' ? offer.params.row_count : sampleRows.length;
  return (
    <div className="ml-5 mr-1 mt-1 border-l-2 border-blue-400/40 pl-2" data-testid="data-preview">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-[10px] text-blue-600 transition-opacity hover:opacity-100 dark:text-blue-300"
        data-testid="data-preview-toggle"
      >
        <Database className="h-2.5 w-2.5" />
        {open
          ? (isEn ? 'Hide sample' : '收起样本')
          : (isEn ? `Show ${rc} rows` : `查看${rc}行样本`)}
      </button>
      {open && (
        <div className="mt-1 max-h-32 overflow-auto rounded border border-border/60 bg-background/60">
          <table className="w-full text-[10px]">
            {cols.length > 0 && (
              <thead className="bg-secondary/60 text-muted-foreground">
                <tr>
                  {cols.map((c) => (
                    <th key={c} className="px-1.5 py-0.5 text-left font-medium">{c}</th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody>
              {sampleRows.map((r, i) => (
                <tr key={i} className="border-t border-border/40">
                  {(cols.length ? cols : Object.keys(r || {})).map((c) => (
                    <td key={c} className="max-w-[12ch] truncate px-1.5 py-0.5" title={String(r?.[c] ?? '')}>
                      {String(r?.[c] ?? '')}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Plan summary row ───────────────────────────────────────────────────

// 2026-08-25: ProgressBar — horizontal bar showing event-level progress
// and time estimate. Hidden when status is 'idle' (no events yet).
function ProgressBar({ progress, isLive, isEn }) {
  if (!progress || progress.status === 'idle') return null;

  const fillColor =
    progress.status === 'failed'
      ? 'bg-red-500'
      : progress.status === 'completed'
        ? 'bg-emerald-500'
        : 'bg-blue-500';

  const labelStatus =
    progress.status === 'running'
      ? isEn ? 'Working' : '处理中'
      : progress.status === 'completed'
        ? isEn ? 'Done' : '完成'
        : progress.status === 'failed'
          ? isEn ? 'Failed' : '失败'
          : '';

  const elapsed = Math.round((progress.elapsedMs || 0) / 1000);
  const remaining = Math.round((progress.estimatedRemainingMs || 0) / 1000);

  return (
    <div
      data-testid="live-activity-progress"
      className="border-b border-border/60 px-3 py-2"
    >
      <div className="mb-1 flex items-center justify-between text-[10px] text-muted-foreground">
        <span className="font-mono uppercase tracking-wide opacity-70">
          {labelStatus}
          {progress.currentPhase ? ` · ${progress.currentPhase}` : ''}
        </span>
        <span className="opacity-50">
          {isEn ? 'Step' : '步骤'} {progress.completedEvents}/{progress.totalEvents}
          {elapsed > 0 && ` · ${elapsed}s ${isEn ? 'elapsed' : '已用'}`}
          {isLive && remaining > 0 && ` · ~${remaining}s ${isEn ? 'left' : '剩余'}`}
        </span>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-secondary/50">
        <div
          className={`h-full ${fillColor} transition-[width] duration-500 ease-out`}
          style={{ width: `${Math.max(2, Math.min(100, progress.fillPct))}%` }}
        />
      </div>
    </div>
  );
}

function PlanSummaryRow({ ev, le, isEn }) {
  const steps = Array.isArray(ev.params?.steps) ? ev.params.steps : [];
  return (
    <div
      className="ml-0 mr-1 rounded-lg border border-violet-400/30 bg-violet-50/40 px-2 py-1.5 text-xs dark:bg-violet-900/10"
      data-testid="plan-summary-row"
    >
      <div className="flex items-center gap-1.5">
        <ClipboardList className="h-3 w-3 shrink-0 text-violet-500" />
        <span className="flex-1 truncate font-medium text-foreground/80">
          {resolveLabel(ev, le)}
        </span>
        {typeof ev.params?.n === 'number' && (
          <span className="text-[10px] text-muted-foreground">
            {ev.params.n} {isEn ? 'steps' : '步'}
          </span>
        )}
      </div>
      {steps.length > 0 && (
        <ul className="mt-1 space-y-0.5 pl-5 text-[10px] text-muted-foreground">
          {steps.slice(0, 6).map((s, i) => (
            <li key={i} className="list-decimal marker:text-violet-400">{s}</li>
          ))}
          {steps.length > 6 && (
            <li className="list-none italic opacity-70">+ {steps.length - 6} more…</li>
          )}
        </ul>
      )}
    </div>
  );
}

// ── Sub-agent badge row ────────────────────────────────────────────────

function SubagentRow({ ev, le, isEn, t }) {
  const status = ev.status || 'done';
  const Icon = status === 'running' ? Plug : PlugZap;
  const colorCls = status === 'running' ? 'text-blue-500' : 'text-emerald-500';
  return (
    <div
      className={`flex items-center gap-2 rounded-md border border-dashed border-blue-300/60 bg-blue-50/40 px-2 py-1 text-[11px] dark:bg-blue-900/10`}
      data-testid="subagent-row"
    >
      <Icon className={`h-3 w-3 shrink-0 ${colorCls} ${status === 'running' ? 'animate-pulse' : ''}`} />
      <span className="min-w-0 flex-1 truncate text-foreground/80">{resolveLabel(ev, le)}</span>
      {ev.params?.target && (
        <span className="rounded bg-secondary px-1 font-mono text-[9px] text-muted-foreground">
          {ev.params.target}
        </span>
      )}
      <span className="shrink-0 text-[10px] text-muted-foreground">{eventMeta(ev, isEn)}</span>
    </div>
  );
}

// ── Generic typed row (tool_call_started, _finished, _failed, retry, verify, finalize) ──

function TypedRow({ ev, le, isLive, isEn, isStickyRunning, collapsed: collapsedProp }) {
  const status = ev.status || 'done';
  // A finalize_started / artifact_progress row left 'running' after the turn
  // ended (legacy turns predate the finalize_done event) must NOT keep
  // spinning + ticking forever. Once the stream is over, it's effectively done.
  const isRunning =
    status === 'running' &&
    !((ev.type === 'finalize_started' || ev.type === 'artifact_progress') && !isLive);
  // 2026-08-25: when collapsed, render a compact chip instead of the full
  // row. Activity-feed modernization task 9.
  // 2026-08-31 (bug): the chip rendered ``collapsedChipSummary`` which built
  // the label from raw params/type — phase_enter/plan_preview/verify_passed/
  // retry rows leaked raw event keys ("phase_enter", "verify_passed") into
  // the feed while the stream was live. Route through the same localized
  // ``resolveLabel`` the expanded row uses so collapsed chips never show raw
  // keys (parity with Kimi/Claude/MiniMax activity feeds).
  const collapsed = !!collapsedProp && (status === 'done' || status === 'failed');
  if (collapsed) {
    return (
      <div
        data-testid="live-activity-collapsed-chip"
        data-event-type={ev.type}
        className={`flex items-center gap-2 rounded-md border border-border/40 bg-secondary/20 px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-secondary/40 ${ev.indent ? 'ml-6' : ''}`}
      >
        <span className={status === 'failed' ? 'text-red-500' : 'text-emerald-600'}>
          {status === 'failed' ? '✗' : '✓'}
        </span>
        <span className="flex-1 truncate">{resolveLabel(ev, le)}</span>
      </div>
    );
  }
  const indent = ev.indent || 0;
  const label = resolveLabel(ev, le);
  const meta = eventMeta(ev, isEn);
  const p = ev.params || {};

  const base = 'flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs transition-colors';
  const styleByStatus = {
    running: isStickyRunning
      ? 'border-l-2 border-blue-500 bg-blue-50/70 shadow-[0_0_0_1px_rgba(59,130,246,0.15)] dark:border-blue-400 dark:bg-blue-900/30'
      : 'bg-blue-50/60 dark:bg-blue-900/20',
    done: '',
    failed: 'bg-red-50/60 dark:bg-red-900/20',
  };
  const indentCls =
    indent === 1 ? 'ml-4' : indent === 2 ? 'ml-8' : '';

  let icon = null;
  if (ev.type === 'phase_enter') {
    icon = <Sparkles className="h-3 w-3 shrink-0 text-amber-500" />;
  } else if (ev.type === 'plan_preview') {
    icon = <ListChecks className="h-3 w-3 shrink-0 text-violet-500" />;
  } else if (isRunning) {
    icon = <Loader2 className="h-3 w-3 shrink-0 animate-spin text-blue-500" />;
  } else if (ev.type === 'verify_passed') {
    icon = <ShieldCheck className="h-3 w-3 shrink-0 text-emerald-500" />;
  } else if (status === 'failed') {
    icon = <XCircle className="h-3 w-3 shrink-0 text-red-500" />;
  } else if (ev.type === 'retry') {
    icon = <AlertTriangle className="h-3 w-3 shrink-0 text-amber-500" />;
  } else if (ev.type === 'artifact_progress') {
    icon = <FileStack className="h-3 w-3 shrink-0 text-blue-500" />;
  } else {
    icon = <Check className="h-3 w-3 shrink-0 text-emerald-500" />;
  }

  const indentGuide = indent > 0 ? (
    <span
      className="-ml-3 mr-1 h-full w-2 border-l-2 border-dashed border-amber-400/50"
      aria-hidden="true"
    />
  ) : null;

  return (
    <div className={`${indentCls}`}>
      {indentGuide}
      <div className={`${base} ${styleByStatus[isRunning ? 'running' : status === 'running' ? 'done' : status] || ''}`}>
        {icon}
        <span
          className={`min-w-0 flex-1 truncate ${isRunning ? 'font-medium text-foreground' : 'text-foreground/80'}`}
          title={ev.ts ? new Date(ev.ts).toLocaleString() : undefined}
        >
          {label}
        </span>
        {isRunning && <RunningTimer startTs={ev.ts} />}
        {meta && (
          <span className="flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground">
            {(ev.type === 'tool_call_finished' || ev.type === 'subagent_returned') && typeof p.duration === 'number' && (
              <Timer className="h-2.5 w-2.5" />
            )}
            {(ev.type === 'tool_call_finished' || ev.type === 'subagent_returned') && typeof p.row_count === 'number' && (
              <Rows3 className="h-2.5 w-2.5" />
            )}
            {meta}
          </span>
        )}
        {ev.type === 'artifact_progress' && typeof p.total === 'number' && (
          <div className="h-1 w-16 shrink-0 overflow-hidden rounded-full bg-secondary">
            <div
              className="h-full rounded-full bg-blue-500 transition-all duration-300 ease-out"
              style={{ width: `${p.total > 0 ? Math.min(100, ((p.current || 0) / p.total) * 100) : 0}%` }}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────

/**
 * LiveStatusLine — P0-3 one-line live streaming status (hybrid mode).
 *
 * Renders a single compact line: pulsing dot + "Live" label + truncated
 * status text. While the model is reasoning it shows the raw
 * chain-of-thought (reasoning_delta tokens); when a tool fires it
 * switches to a short action label ("Running query…"). Only visible
 * while the turn is streaming — disappears on completion.
 */
function LiveStatusLine({ reasoning, action, isLive }) {
  if (!isLive) return null;
  let text = '';
  if (typeof reasoning === 'string' && reasoning.trim()) {
    text = reasoning.trim();
  } else if (typeof action === 'string' && action.trim()) {
    text = TOOL_ACTION_LABELS[action] || `Running ${action}…`;
  }
  if (!text) return null;
  return (
    <div
      data-testid="live-status-line"
      className="flex items-center gap-2 border-b border-border/60 px-3 py-1.5"
    >
      <span className="relative flex h-2 w-2 shrink-0">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
      </span>
      <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-emerald-600 dark:text-emerald-400">
        Live
      </span>
      <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground" dir="auto">
        {text}
      </span>
    </div>
  );
}

/**
 * LoadingPlaceholder — 2026-08-31: rich initial state for the live activity card.
 *
 * Before the first SSE event arrives the component used to render nothing,
 * so Chat.jsx fell back to the legacy ChatThinkingIndicator ("Understanding
 * your request…"). This placeholder keeps the Fabricating/Orchestrating card
 * visible from the first frame, matching the desired Kimi/Claude-style feed.
 */
function LoadingPlaceholder({
  phase,
  streamingPlanSteps,
  streamingReasoning,
  streamingAction,
  isEn,
  t,
  isLive,
  onCollapse,
}) {
  const fallbackState = 'init';
  const state = phase?.state || fallbackState;
  const verb = t.chat.phase?.[state]?.verb || t.chat.phase?.[fallbackState]?.verb || (isEn ? 'Orchestrating' : '正在编排');
  const title = t.chat.phase?.[state]?.title || t.chat.phase?.[fallbackState]?.title || (isEn ? 'Laying out the plan' : '正在制定执行方案');
  const hasPlan = Array.isArray(streamingPlanSteps) && streamingPlanSteps.length > 0;

  return (
    <>
      <LiveStatusLine
        reasoning={streamingReasoning}
        action={streamingAction}
        isLive={isLive}
      />
      <div
        data-testid="live-activity-headline"
        className="flex items-center gap-2 border-b border-border/60 px-3 py-2"
      >
        <Sparkles className="h-3.5 w-3.5 shrink-0 animate-pulse text-amber-500" />
        <span className="text-xs font-medium text-foreground">{verb}</span>
        <span className="truncate text-xs text-muted-foreground">{title}</span>
      </div>

      {hasPlan ? (
        <PlanChecklist steps={streamingPlanSteps} isEn={isEn} />
      ) : (
        <div className="border-b border-border/60 px-3 py-2">
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <ListChecks className="h-3 w-3" />
            <span>{isEn ? 'Plan' : '计划'}</span>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-blue-500" />
            <span>{isEn ? 'Preparing plan…' : '正在准备计划…'}</span>
          </div>
        </div>
      )}

      <div className="border-b border-border/60 px-3 py-2">
        <div className="h-1 w-full overflow-hidden rounded-full bg-secondary/50">
          <div className="h-full w-1/3 animate-pulse rounded-full bg-blue-400/60" />
        </div>
      </div>

      <div className="flex items-center justify-between border-t border-border/60 px-3 py-1.5">
        <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground/70">
          {isLive && (
            <span
              className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500"
              aria-hidden="true"
            />
          )}
          {isLive ? (isEn ? 'live' : '实时') : (isEn ? 'Working' : '处理中')}
        </span>
        <button
          type="button"
          onClick={onCollapse}
          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-secondary/60 hover:text-foreground"
        >
          <ChevronUp className="h-3 w-3" />
          {isEn ? 'Collapse' : (t.chat.liveEvent?.collapse || '收起')}
        </button>
      </div>
    </>
  );
}

/**
 * PlanChecklist — 2026-08-27: the agent's live todo list.
 *
 * The backend derives a deterministic plan from the user's input BEFORE the
 * loop runs (plan_step_added events) and ticks steps off as tool evidence
 * completes them (plan_step_completed events). This compact checklist makes
 * the "understand → plan → follow plan → respond" contract visible: the user
 * sees the todo list appear, then watches each step check off as the agent
 * executes it. Deliberately NOT the old 4-section panel — one slim row list
 * inside the existing activity box.
 */
function PlanChecklist({ steps, isEn }) {
  if (!Array.isArray(steps) || steps.length === 0) return null;
  const sorted = [...steps].sort((a, b) => a.step_index - b.step_index);
  const done = sorted.filter((s) => s.status === 'done').length;
  return (
    <div data-testid="plan-checklist" className="border-b border-border/60 px-3 py-2">
      <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        <ListChecks className="h-3 w-3" />
        <span>{isEn ? 'Plan' : '计划'}</span>
        <span className="ml-auto tabular-nums text-muted-foreground/70">
          {done}/{sorted.length}
        </span>
      </div>
      <ol className="space-y-1">
        {sorted.map((s) => {
          const isDone = s.status === 'done';
          return (
            <li
              key={s.step_index}
              data-testid={`plan-step-${s.step_index}`}
              className={`flex items-center gap-1.5 text-xs ${isDone ? 'text-muted-foreground' : 'text-foreground'}`}
            >
              {isDone ? (
                <Check className="h-3 w-3 shrink-0 text-emerald-500" />
              ) : (
                <span
                  className="h-3 w-3 shrink-0 rounded-full border border-muted-foreground/40"
                  aria-hidden="true"
                />
              )}
              <span className={`min-w-0 truncate ${isDone ? 'line-through opacity-70' : ''}`}>
                {s.title}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

export default function LiveActivityStream({ events, legacySteps, phase, reasoning, isStreaming, streamingSearchQueries, streamingPlanSteps, streamingDataPreviews, streamingReasoning, streamingAction }) {
  const { lang, t } = useLanguage();
  const isEn = lang === 'en';
  const le = t?.chat?.liveEvent || {};

  // Pick the best source: live_events wins; fall back to synthesized legacy.
  const effectiveEvents = useMemo(
    () => pickEvents(events, legacySteps),
    [events, legacySteps],
  );
  const hasEvents = Array.isArray(effectiveEvents) && effectiveEvents.length > 0;
  const merged = useMemo(() => (hasEvents ? mergeEvents(effectiveEvents) : []), [effectiveEvents, hasEvents]);
  // 2026-08-31: plan checklist from PERSISTED events. The backend now emits
  // plan_step_added / plan_step_completed as typed live events (previously
  // raw-SSE only), so a reloaded message can rebuild the exact checklist the
  // user saw live — instead of degrading to a generic "N events" chip.
  // streamingPlanSteps (live) wins when present; otherwise derive from events.
  const persistedPlanSteps = useMemo(() => {
    if (!hasEvents) return [];
    const byIndex = new Map();
    for (const ev of effectiveEvents) {
      if (ev.type === 'plan_step_added' || ev.type === 'plan_step_completed') {
        const idx = ev.params?.step_index;
        if (idx == null) continue;
        const cur = byIndex.get(idx) || {
          step_index: idx,
          title: ev.params?.title || '',
          status: 'pending',
        };
        if (ev.type === 'plan_step_completed') cur.status = 'done';
        byIndex.set(idx, cur);
      }
    }
    return [...byIndex.values()].sort((a, b) => a.step_index - b.step_index);
  }, [effectiveEvents, hasEvents]);
  const planSteps = useMemo(() => {
    if (Array.isArray(streamingPlanSteps) && streamingPlanSteps.length > 0) return streamingPlanSteps;
    return persistedPlanSteps;
  }, [streamingPlanSteps, persistedPlanSteps]);
  // 2026-08-25: activity-feed modernization pipeline
  // 1. attachSubagentParent: pair tool rows with their parent subagent
  // 2. nestRows: set indent=1 for child rows under a subagent
  // 3. computeCollapseStates: auto-collapse done rows when newer events are running
  // 4. applyRetryIndent: visual indent for retry lineage (was dead code
  //    until 2026-08-31 — retry rows never carried the ml-4/ml-8 indent).
  const mergedIndented = useMemo(() => {
    if (!hasEvents) return [];
    const withParents = attachSubagentParent(merged);
    const nested = nestRows(withParents);
    return applyRetryIndent(computeCollapseStates(nested));
  }, [merged, hasEvents]);

  // Headline derivation: latest `phase_enter.*` event wins; fall back to the
  // legacy `phase` prop only when no events exist. This is the single source
  // of truth — there used to be two (phase prop + first phase_enter row) which
  // caused the headline to desync from the event list.
  const headlineState = useMemo(() => {
    const phaseEnters = effectiveEvents.filter((e) => e.type === 'phase_enter');
    const last = phaseEnters[phaseEnters.length - 1];
    if (last) return String(last.label_key || '').replace('phase_enter.', '');
    return phase?.state || null;
  }, [effectiveEvents, phase]);
  // Defensive optional-chaining on t.chat.phase: some translation
  // variants / test mocks may not define the phase map, and a missing
  // key must never crash the activity stream (it degrades to the raw
  // headline state instead of throwing).
  const headlineVerb = t.chat.phase?.[headlineState]?.verb || phase?.verb || headlineState || '';
  const headlineTitle = t.chat.phase?.[headlineState]?.title || phase?.title || '';

  // Sticky running row: the most-recent running row gets the left-border
  // accent + pulsing background, so the user sees what's happening RIGHT NOW.
  // finalize_started / artifact_progress rows are excluded once the stream
  // has ended (they'd keep the highlight + timer alive forever on completed turns).
  const stickyRunningKey = useMemo(() => {
    for (let i = mergedIndented.length - 1; i >= 0; i--) {
      const r = mergedIndented[i];
      if (
        r.status === 'running' &&
        !((r.type === 'finalize_started' || r.type === 'artifact_progress') && !isStreaming)
      ) {
        return i;
      }
    }
    return -1;
  }, [mergedIndented, isStreaming]);

  // Stream still live (or never finished, e.g. mid-request) → expanded.
  const [expanded, setExpanded] = useState(() => Boolean(isStreaming));
  const [userToggled, setUserToggled] = useState(false);
  const [thinkingOpen, setThinkingOpen] = useState(false);
  // 2026-08-25: live-streaming spec — per-section collapse state. Default
  // open while streaming so the user sees the live content; collapses on done.
  const [planOpen, setPlanOpen] = useState(true);
  const [searchOpen, setSearchOpen] = useState(true);
  const [dataPreviewOpen, setDataPreviewOpen] = useState(true);
  const prevStreaming = useRef(isStreaming);

  // Auto-collapse ~1s after the stream finishes, unless the user manually
  // expanded (ephemeral choice, not persisted).
  useEffect(() => {
    const was = prevStreaming.current;
    prevStreaming.current = isStreaming;
    if (was && !isStreaming && !userToggled) {
      const tmr = setTimeout(() => setExpanded(false), 1000);
      return () => clearTimeout(tmr);
    }
    return undefined;
  }, [isStreaming, userToggled]);

  // Auto-scroll: follow new events unless the user has scrolled up.
  // 2026-08-25: live-streaming spec — also follow streaming content updates
  // (reasoning text, plan steps, search queries, data previews) so the user
  // sees the latest progress as it streams in.
  const scrollRef = useRef(null);
  const nearBottom = useRef(true);
  useEffect(() => {
    if (nearBottom.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [
    mergedIndented.length,
    typeof reasoning === 'string' ? reasoning.length : 0,
    streamingSearchQueries ? Object.keys(streamingSearchQueries).length : 0,
    Array.isArray(streamingPlanSteps) ? streamingPlanSteps.length : 0,
    streamingDataPreviews ? Object.keys(streamingDataPreviews).length : 0,
  ]);

  const isLive = Boolean(isStreaming);

  // 2026-08-31: show a rich placeholder while the stream is live but no events
  // have arrived yet. This eliminates the legacy "Understanding your request"
  // indicator and keeps the Fabricating/Orchestrating card visible from t=0.
  if (!hasEvents) {
    if (!isLive) return null;
    return (
      <div
        data-testid="live-activity-stream"
        className="mb-3 animate-slide-up overflow-hidden rounded-xl border border-border/80 bg-gradient-to-b from-slate-50 to-white text-foreground dark:from-slate-900 dark:to-slate-800"
      >
        <LoadingPlaceholder
          phase={phase}
          streamingPlanSteps={streamingPlanSteps}
          streamingReasoning={streamingReasoning}
          streamingAction={streamingAction}
          isEn={isEn}
          t={t}
          isLive={isLive}
          onCollapse={() => { setExpanded(false); setUserToggled(true); }}
        />
      </div>
    );
  }

  // A row is terminal once done/failed — or when the turn itself has ended
  // (collapsed summary implies finalize completed). Orphan running rows from
  // an interrupted turn are still excluded from the completed count.
  const isTerminalRow = (e) => e.status !== 'running' || (e.type === 'finalize_started' && !isLive);
  const doneCount = mergedIndented.filter(isTerminalRow).length;
  let totalSeconds = 0;
  if (mergedIndented.length) {
    const first = mergedIndented[0]?.ts ? new Date(mergedIndented[0].ts).getTime() : null;
    const last = mergedIndented[mergedIndented.length - 1]?.ts ? new Date(mergedIndented[mergedIndented.length - 1].ts).getTime() : null;
    if (first != null && last != null && last >= first) totalSeconds = Math.round((last - first) / 1000);
  }
  const summaryLabel = formatTemplate(le.summary || '{n} events · {m} completed · {s}s', {
    n: mergedIndented.length,
    m: doneCount,
    s: totalSeconds,
  });

  return (
    <div
      data-testid="live-activity-stream"
      className="mb-3 animate-slide-up overflow-hidden rounded-xl border border-border/80 bg-gradient-to-b from-slate-50 to-white text-foreground dark:from-slate-900 dark:to-slate-800"
    >
      {expanded ? (
        <>
          {/* P0-3 one-line live streaming status (hybrid reasoning/action). */}
          <LiveStatusLine
            reasoning={streamingReasoning}
            action={streamingAction}
            isLive={isLive}
          />
          {/* Phase headline bar — pulses amber while the turn is live. */}
          {headlineState && (
            <div
              data-testid="live-activity-headline"
              className="flex items-center gap-2 border-b border-border/60 px-3 py-2"
            >
              <Sparkles
                className={`h-3.5 w-3.5 shrink-0 text-amber-500 ${isLive ? 'animate-pulse' : 'opacity-60'}`}
              />
              <span className={`text-xs font-medium ${isLive ? 'text-foreground' : 'text-muted-foreground'}`}>
                {headlineVerb}
              </span>
              {headlineTitle && (
                <span className="truncate text-xs text-muted-foreground">{headlineTitle}</span>
              )}
            </div>
          )}

          {/* Plan checklist — 2026-08-27: the agent's live todo list
              (deterministic plan derived from the user's input, ticked off
              by tool evidence as the loop executes it). Live steps win;
              persisted plan_step events rebuild the same checklist after
              reload (2026-08-31). */}
          <PlanChecklist steps={planSteps} isEn={isEn} />

          {/* Progress bar — 2026-08-25 activity-feed modernization. */}
          <ProgressBar
            progress={computeProgress(mergedIndented)}
            isLive={isLive}
            isEn={isEn}
          />

          {/* Event list (max-height, auto-scroll while live). */}
          <div
            ref={scrollRef}
            onScroll={() => {
              const el = scrollRef.current;
              if (el) nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
            }}
            data-testid="live-activity-list"
            className="max-h-80 space-y-0.5 overflow-y-auto px-2 py-2 dark:max-h-60"
          >
            {mergedIndented.map((ev, i) => {
              if (ev.type === 'plan_summary') {
                return <PlanSummaryRow key={`${ev.ts}-${i}`} ev={ev} le={le} isEn={isEn} />;
              }
              if (ev.type === 'subagent_invoked' || ev.type === 'subagent_returned') {
                return <SubagentRow key={`${ev.ts}-${i}`} ev={ev} le={le} isEn={isEn} t={t} />;
              }
              const isSticky = isLive && i === stickyRunningKey;
              return (
                <div key={`${ev.ts}-${i}`}>
                  <TypedRow
                    ev={ev}
                    le={le}
                    isLive={isLive}
                    isEn={isEn}
                    isStickyRunning={isSticky}
                    collapsed={ev.collapsed}
                  />
                  {ev.data_offer && <DataPreviewPane offer={ev.data_offer} isEn={isEn} />}
                </div>
              );
            })}
          </div>

          {/* 2026-08-25 (Session 31): Live-streaming sections (Thinking, Search, Plan, Results) were removed per user request. Backend SSE events (reasoning_delta, search_query_delta, plan_step_added, data_preview) are still emitted but the frontend ignores them. To re-enable, see git history for commit 4a86311. */}
          {/* Footer: LIVE badge (only while streaming) + Collapse toggle. */}
          <div className="flex items-center justify-between border-t border-border/60 px-3 py-1.5">
            <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground/70">
              {isLive && (
                <span
                  className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500"
                  aria-hidden="true"
                />
              )}
              {isLive ? (isEn ? 'live' : '实时') : summaryLabel}
            </span>
            <button
              type="button"
              onClick={() => { setExpanded(false); setUserToggled(true); }}
              className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-secondary/60 hover:text-foreground"
            >
              <ChevronUp className="h-3 w-3" />
              {isEn ? 'Collapse' : le.collapse || '收起'}
            </button>
          </div>
        </>
      ) : (
        /* Summary bar — the per-turn audit trail after completion. */
        <button
          type="button"
          onClick={() => { setExpanded(true); setUserToggled(true); }}
          data-testid="live-activity-summary"
          className="flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-secondary/40"
        >
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
            <Check className="h-3 w-3" />
          </span>
          <span className="flex-1 truncate text-xs font-medium text-foreground/80">{summaryLabel}</span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        </button>
      )}
    </div>
  );
}