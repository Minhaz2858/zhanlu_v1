import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { CheckCircle2, XCircle, Loader2, MinusCircle, Calendar, Clock, AlertTriangle, MessageSquare } from 'lucide-react';
import { useTranslate } from '@/lib/useTranslate';
import { useLanguage } from '@/lib/LanguageProvider';
import { Button } from '@/components/ui/button';
import InlineArtifactPreview from '@/components/chat/InlineArtifactPreview';
import RunChecklist from './RunChecklist';
import RunFailureActions from './RunFailureActions';
import { authFetch } from '@/api/authFetch';
import { formatShortDateTime } from '@/lib/time';

// Build the in-app chat URL for a run's "Open in chat" action. Navigates
// within the SPA (no new tab) and carries the agent/project context so the
// chat page can re-bind the right agent + KB scope.
function buildChatUrl(entry, task) {
  const convId = entry?.conversation_id || entry?.output_data?.conversation_id;
  if (!convId) return null;
  const params = new URLSearchParams({ conv: convId });
  const agentName = entry?.output_data?.agent_name;
  if (agentName) params.set('agentName', agentName);
  if (task?.project_id) params.set('project', task.project_id);
  if (task?.project && task.project !== 'global') params.set('projectName', task.project);
  return `/?${params.toString()}`;
}

function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

// Phase headline labels — mirrors the backend PHASE_HEADLINES map. The polled
// execution row stores only the raw FSM state (e.g. "act"); this maps it to a
// friendly verb + title for the headline shown while a run is in progress.
const PHASE_LABELS = {
  init: { verb: 'Fathoming', title: 'Reading your request' },
  goal: { verb: 'Fathoming', title: 'Understanding what you need' },
  context: { verb: 'Orienting', title: 'Gathering the right context' },
  plan: { verb: 'Orchestrating', title: 'Laying out the plan' },
  gate: { verb: 'Checking', title: 'Running policy checks' },
  act: { verb: 'Fabricating', title: 'Building your deliverable' },
  observe: { verb: 'Watching', title: 'Recording the results' },
  verify: { verb: 'Validating', title: 'Checking the outputs' },
  finalize: { verb: 'Crystallizing', title: 'Wrapping everything up' },
  done: { verb: 'Done', title: 'All finished' },
  fail: { verb: 'Stopped', title: 'Something went wrong' },
};

function phaseHeadline(state) {
  if (!state) return null;
  return PHASE_LABELS[state] || { verb: 'Working', title: state.replace(/_/g, ' ') };
}

export default function ExecutionLogsList({ task, noLogsText, refreshKey }) {
  const { t, lang } = useLanguage();
  const [executions, setExecutions] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!task?.id) return;
    let cancelled = false;
    async function fetchExecutions() {
      setLoading(true);
      try {
        const res = await authFetch(`/api/automations/${task.id}/executions?limit=20`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) setExecutions(data.executions || []);
      } catch (e) {
        if (!cancelled) {
          // Fall back to task's execution_history if API is unreachable
          const history = task.execution_history || [];
          setExecutions(history.map((h, i) => ({
            id: h.execution_id || `legacy-${i}`,
            status: h.status || 'completed',
            started_at: h.completed_at,
            duration_seconds: h.duration_seconds,
            output_text: h.result || '',
            files: [],
            file_count: h.file_count || 0,
          })));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    fetchExecutions();
    // Refresh every 30s so the user sees running → completed transitions live
    const interval = setInterval(fetchExecutions, 30000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [task?.id, refreshKey]);

  const texts = executions
    .map((e) => e.output_text)
    .filter(Boolean)
    .slice(0, 5);
  const translate = useTranslate(texts, lang);

  if (loading && executions.length === 0) {
    return (
      <div className="flex items-center justify-center py-6 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {lang === 'en' ? 'Loading executions…' : '加载执行记录…'}
      </div>
    );
  }

  if (executions.length === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">{noLogsText}</p>;
  }

  const statusCfg = {
    completed: { icon: CheckCircle2, color: 'text-green-600', bg: 'bg-green-50', border: 'border-green-200', label: t.detail.execStatuses?.success || 'Completed' },
    running: { icon: Loader2, color: 'text-blue-600', bg: 'bg-blue-50', border: 'border-blue-200', label: t.detail.execStatuses?.running || 'Running', animate: true },
    queued: { icon: Clock, color: 'text-amber-600', bg: 'bg-amber-50', border: 'border-amber-200', label: 'Queued' },
    failed: { icon: XCircle, color: 'text-red-600', bg: 'bg-red-50', border: 'border-red-200', label: t.detail.execStatuses?.failed || 'Failed' },
    skipped: { icon: MinusCircle, color: 'text-amber-600', bg: 'bg-amber-50', border: 'border-amber-200', label: t.detail.execStatuses?.skipped || 'Skipped' },
  };

  return (
    <div className="max-h-[28rem] space-y-2 overflow-y-auto pr-1">
      {executions.map((entry) => {
        const cfg = statusCfg[entry.status] || statusCfg.completed;
        const Icon = cfg.icon;
        const outputIdx = executions
          .map((e) => e.output_text)
          .filter(Boolean)
          .indexOf(entry.output_text);
        return (
          <div key={entry.id} className={`rounded-lg border px-4 py-3 ${cfg.bg} ${cfg.border}`}>
            <div className="flex items-start gap-3">
              <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${cfg.color} ${cfg.animate ? 'animate-spin' : ''}`} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className={`text-xs font-medium ${cfg.color}`}>{cfg.label}</span>
                    {entry.attempt > 0 && (
                      <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        {lang === 'en' ? `retry ${entry.attempt}` : `重试 ${entry.attempt}`}
                      </span>
                    )}
                    {entry.duration_seconds !== null && entry.duration_seconds !== undefined && (
                      <span className="text-[11px] text-muted-foreground">· {fmtDuration(entry.duration_seconds)}</span>
                    )}
                  </div>
                  <span className="text-xs text-muted-foreground">{formatShortDateTime(entry.started_at || entry.created_date)}</span>
                </div>
                {entry.error && (
                  <p className="mt-1 text-xs text-red-700">{entry.error}</p>
                )}
                {/*
                 * Manus-style recovery card (M4): when an execution failed,
                 * surface a small card with a clear next step.  The backend
                 * tags the failure with an ``error_code`` reason so the
                 * right destination (cost settings / run history / etc.) is
                 * chosen automatically.
                 */}
                {entry.status === 'failed' && entry.error && (
                  <RunFailureActions
                    error={entry.error}
                    reason={entry.error_code || 'unknown'}
                  />
                )}
                {/*
                 * Manus-style action checklist (M3): when an execution's
                 * activity_steps payload is present, render a progressive
                 * checklist with a real progress bar and accessible
                 * status/warning semantics, instead of just text rows.
                 */}
                {entry.status === 'running' && phaseHeadline(entry.current_phase) && (
                  <div className="mt-2 flex items-center gap-1.5 text-[11px] font-medium text-primary">
                    <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                    <span>{phaseHeadline(entry.current_phase).verb}</span>
                    <span className="text-muted-foreground">· {phaseHeadline(entry.current_phase).title}</span>
                  </div>
                )}
                {Array.isArray(entry.activity_steps) && entry.activity_steps.length > 0 && (
                  <div className="mt-2">
                    <RunChecklist steps={entry.activity_steps} status={entry.status} />
                  </div>
                )}
                {entry.held_back && (
                  <div className="mt-2 flex items-start gap-2 rounded-md border border-amber-300/60 bg-amber-50 px-2.5 py-2 text-[11px] leading-4 text-amber-800 dark:border-amber-500/30 dark:bg-amber-950/40 dark:text-amber-200">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span>
                      {lang === 'en'
                        ? 'Deliverable held back by the quality gate — execution confidence was below the shipping threshold. Trigger the run manually to retry or refine the task.'
                        : '产出文件被质量门禁拦截——执行置信度低于发布阈值。请手动触发运行以重试或调整任务。'}
                    </span>
                  </div>
                )}
                {entry.output_text && (
                  <p className="mt-1 line-clamp-3 break-words text-sm text-foreground">
                    {outputIdx >= 0 ? translate(entry.output_text) : entry.output_text}
                  </p>
                )}
                {/*
                 * Inline deliverable previews — same Manus-style cards the chat
                 * surface uses. We surface one card per file below so the
                 * execution card stays scannable. Files without a preview URL
                 * still render as cards (Open falls back to download).
                 */}
                {entry.files && entry.files.length > 0 && (
                  <div className="mt-2 space-y-2">
                    {entry.files
                      .filter((f) => f)
                      .map((f) => (
                        <InlineArtifactPreview key={f.id || f.automation_file_id} artifact={f} />
                      ))}
                  </div>
                )}
                {buildChatUrl(entry, task) && (
                  <Link
                    to={buildChatUrl(entry, task)}
                    className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-xs text-foreground transition-colors hover:bg-secondary"
                  >
                    <MessageSquare className="h-3 w-3" />
                    {lang === 'en' ? 'Open in chat' : '在对话中打开'}
                  </Link>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
