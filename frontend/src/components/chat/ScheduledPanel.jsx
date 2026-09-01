import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  X, ChevronDown, ChevronRight, Clock, AlertTriangle, CheckCircle2,
  Loader2, ExternalLink, CalendarDays, FileText, Image as ImageIcon,
  Code2, Download, MoreVertical, ListChecks, MessageSquare,
} from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { formatAbsoluteTime, formatRunDate, formatTimeOfDay } from '@/lib/time';
import { authFetch } from '@/api/authFetch';
import RunChecklist from '@/components/automation/RunChecklist';
import RunFailureActions from '@/components/automation/RunFailureActions';

/**
 * ScheduledPanel — Manus-style right-side drawer for scheduled automations.
 *
 * Architecture:
 *   - The panel is mounted at the Chat page level and slides in from the
 *     right when the user clicks the "Scheduled" button in the chat header.
 *   - On open, it fetches the AutomationTask linked to the current chat
 *     session (GET /api/automations/by-session/{id}) and renders the
 *     schedule config + past runs. No new backend endpoint is required.
 *   - When the parent passes a `focusExecutionId` (e.g. from an "Open run"
 *     button on a chat message), the matching run auto-expands.
 *
 * Why a custom drawer and not a Radix Dialog?  The chat composer needs to
 * stay usable while the panel is open, and we want the panel to take a
 * fixed pixel width with a real slide-in animation, not a centered modal.
 */

const RRULE_HOUR_REGEX = /BYHOUR=(\d+)/;
const RRULE_MIN_REGEX = /BYMINUTE=(\d+)/;
const RRULE_BYDAY_REGEX = /BYDAY=([A-Z,]+)/;
const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const RRULE_DAY_MAP = { SU: 0, MO: 1, TU: 2, WE: 3, TH: 4, FR: 5, SA: 6 };

// Phase headline labels — now sourced from translations.js (t.chat.phase)
// so the polled fallback matches the user's UI language. The SSE stream
// also ships English verb/title, but we always derive verb/title from the
// language-neutral FSM `state` key at render time.
function phaseHeadline(state, t) {
  if (!state) return null;
  const entry = t?.chat?.phase?.[state];
  if (entry) return entry;
  // Fall back to a generic "Working" label (also translated) when an
  // unknown state slips through, instead of leaking English.
  const working = t?.chat?.phase?.working || { verb: 'Working', title: state.replace(/_/g, ' ') };
  return working;
}

function formatRrule(rrule) {
  if (!rrule) return '';
  if (rrule.startsWith('FREQ=HOURLY')) {
    const intervalMatch = rrule.match(/INTERVAL=(\d+)/);
    const interval = intervalMatch ? parseInt(intervalMatch[1], 10) : 1;
    return interval === 1 ? 'Every hour' : `Every ${interval} hours`;
  }
  if (rrule.startsWith('FREQ=WEEKLY')) {
    const hourMatch = rrule.match(RRULE_HOUR_REGEX);
    const minMatch = rrule.match(RRULE_MIN_REGEX);
    const dayMatch = rrule.match(RRULE_BYDAY_REGEX);
    const days = dayMatch
      ? dayMatch[1].split(',').map((d) => DAY_NAMES[RRULE_DAY_MAP[d]] || d).join(', ')
      : '';
    const time = hourMatch
      ? `${String(parseInt(hourMatch[1], 10)).padStart(2, '0')}:${String(minMatch ? parseInt(minMatch[1], 10) : 0).padStart(2, '0')}`
      : '';
    if (days && time) return `${days} at ${time}`;
    if (time) return `Weekly at ${time}`;
    if (days) return `Weekly on ${days}`;
    return 'Weekly';
  }
  if (rrule.startsWith('FREQ=DAILY')) {
    const hourMatch = rrule.match(RRULE_HOUR_REGEX);
    const minMatch = rrule.match(RRULE_MIN_REGEX);
    if (hourMatch) {
      const hour = parseInt(hourMatch[1], 10);
      const min = minMatch ? parseInt(minMatch[1], 10) : 0;
      return `Daily at ${String(hour).padStart(2, '0')}:${String(min).padStart(2, '0')}`;
    }
    return 'Daily';
  }
  return rrule.length > 40 ? `${rrule.slice(0, 37)}…` : rrule;
}


function fileCategory(file) {
  const ext = (file.file_type || '').toLowerCase().replace(/^\./, '');
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'bmp'].includes(ext)) return 'images';
  if (['tsx', 'ts', 'js', 'jsx', 'py', 'json', 'html', 'css', 'go', 'rs', 'java', 'c', 'cpp', 'sh', 'yml', 'yaml'].includes(ext)) return 'code';
  return 'docs';
}

function FileIcon({ file }) {
  const cat = fileCategory(file);
  if (cat === 'images') return <ImageIcon className="h-3.5 w-3.5 text-primary" />;
  if (cat === 'code') return <Code2 className="h-3.5 w-3.5 text-primary" />;
  return <FileText className="h-3.5 w-3.5 text-primary" />;
}

function excerpt(text, max = 140) {
  if (!text) return '';
  const clean = text.replace(/\s+/g, ' ').trim();
  if (clean.length <= max) return clean;
  const truncated = clean.slice(0, max);
  const lastSpace = truncated.lastIndexOf(' ');
  return `${lastSpace > 0 ? truncated.slice(0, lastSpace) : truncated}…`;
}

function statusMeta(status) {
  switch (status) {
    case 'completed':
    case 'success':
      return { Icon: CheckCircle2, className: 'text-emerald-600', label: 'Completed' };
    case 'running':
    case 'in_progress':
      return { Icon: Loader2, className: 'text-primary animate-spin', label: 'Running' };
    case 'failed':
    case 'error':
      return { Icon: AlertTriangle, className: 'text-red-500', label: 'Failed' };
    default:
      return { Icon: Clock, className: 'text-muted-foreground', label: status || 'Pending' };
  }
}

export default function ScheduledPanel({ sessionId, focusExecutionId, onClose }) {
  const { lang, t } = useLanguage();
  const isEn = lang === 'en';
  const [loading, setLoading] = useState(false);
  const [task, setTask] = useState(null);
  const [executions, setExecutions] = useState([]);
  const [expanded, setExpanded] = useState(() => new Set(focusExecutionId ? [focusExecutionId] : []));
  const [error, setError] = useState(null);

  // Reset expanded state when focusExecutionId changes (parent can hand us a
  // new run to open). Strip the focus id from state if it was the only entry
  // and the prop goes back to null so future opens don't auto-expand stale ids.
  useEffect(() => {
    if (focusExecutionId) {
      setExpanded(new Set([focusExecutionId]));
    }
  }, [focusExecutionId]);

  // Live SSE overlay: per-execution phase + activity_steps pushed by the
  // backend's /events/stream while a run is in progress. Falls back to the
  // 15s polling below when SSE fails or no run is expanded. Keyed by exec id
  // so multiple runs can be overlaid independently.
  const [liveByExec, setLiveByExec] = useState({});
  const refreshRef = useRef(null);

  // Fetch the task + executions. Shared by the initial load, the 15s poll,
  // and the SSE terminal-event handler (so a 'done'/'error' immediately
  // refreshes the list to pick up final status + files).
  const refresh = useCallback(
    (isInitial = false) => {
      if (!sessionId) return;
      if (isInitial) {
        setLoading(true);
        setError(null);
      }
      authFetch(`/api/automations/by-session/${sessionId}`)
        .then((r) => (r.ok ? r.json() : (isInitial ? Promise.reject(new Error(`HTTP ${r.status}`)) : null)))
        .then((data) => {
          if (!data) return;
          setTask(data?.task || null);
          setExecutions(Array.isArray(data?.executions) ? data.executions : []);
        })
        .catch((e) => {
          if (!isInitial) return; // keep stale data on transient poll failure
          setError(e?.message || 'Failed to load');
          setTask(null);
          setExecutions([]);
        })
        .finally(() => {
          if (isInitial) setLoading(false);
        });
    },
    [sessionId],
  );
  refreshRef.current = refresh;

  // Fetch the task + executions when the panel mounts or the session changes.
  // In the docked model the component is only mounted when scheduledOpen, so
  // we no longer need an `open` guard — being mounted IS "open".
  useEffect(() => {
    refresh(true);
    return undefined;
  }, [sessionId, refresh]);

  // Auto-refresh while the panel is mounted: poll every 15s so a fresh run
  // shows up without the user closing and reopening the panel. (SSE handles
  // the expanded live run; this keeps the rest of the list current.)
  useEffect(() => {
    if (!sessionId) return undefined;
    const id = setInterval(() => refresh(false), 15000);
    return () => clearInterval(id);
  }, [sessionId, refresh]);

  // The single expanded running/queued execution to stream live. useMemo so
  // the SSE effect only re-runs when the TARGET changes — not on every 15s
  // poll (which would thrash the EventSource). Returns null when nothing is
  // both expanded and in-progress.
  const liveTargetId = useMemo(() => {
    for (const ex of executions) {
      if (expanded.has(ex.id) && (ex.status === 'running' || ex.status === 'queued')) {
        return ex.id;
      }
    }
    return null;
  }, [executions, expanded]);

  useEffect(() => {
    if (!liveTargetId) return undefined;
    const targetId = liveTargetId;
    const url = `/api/automations/executions/${targetId}/events/stream`;
    let closed = false;
    const source = new EventSource(url);
    source.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'phase') {
          // Backend emits friendly verb + title; store both plus the raw state.
          setLiveByExec((prev) => ({
            ...prev,
            [targetId]: {
              ...(prev[targetId] || {}),
              phaseState: data.state,
              phaseVerb: data.verb,
              phaseTitle: data.title,
            },
          }));
        } else if (data.type === 'activity_steps') {
          setLiveByExec((prev) => ({
            ...prev,
            [targetId]: { ...(prev[targetId] || {}), steps: data.steps },
          }));
        } else if (data.type === 'done' || data.type === 'error') {
          // Run reached a terminal state — close the stream and refresh the
          // list so the final status + files appear immediately.
          if (!closed) { closed = true; source.close(); }
          setLiveByExec((prev) => {
            if (!prev[targetId]) return prev;
            const next = { ...prev };
            delete next[targetId];
            return next;
          });
          refreshRef.current?.(false);
        }
      } catch {
        /* ignore malformed frames — SSE is best-effort */
      }
    };
    source.onerror = () => {
      // SSE failed (network/proxy/reconnect limit) — close and rely on polling.
      if (!closed) { closed = true; source.close(); }
    };
    return () => {
      if (!closed) { closed = true; source.close(); }
    };
  }, [liveTargetId]);

  const scheduleLabel = useMemo(() => formatRrule(task?.schedule), [task?.schedule]);
  const nextRun = task?.next_run_at;
  const skipConfirm = !!task?.skip_confirmation;

  const toggleRun = (id) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <aside
      role="complementary"
      aria-label={isEn ? 'Scheduled automation' : '定时任务'}
      data-testid="scheduled-panel"
      className="flex h-full w-full flex-col border-l border-border bg-card"
    >
      <div className="flex h-full flex-col">
        {/* Header */}
        <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-semibold text-foreground">
              {task?.name || (loading ? (isEn ? 'Loading…' : '加载中…') : (isEn ? 'Scheduled' : '定时任务'))}
            </p>
            {task?.status && (
              <p className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    task.status === 'active' ? 'bg-emerald-500' : 'bg-muted-foreground'
                  }`}
                  aria-hidden="true"
                />
                {task.status === 'active' ? (isEn ? 'Active' : '运行中') : task.status}
              </p>
            )}
          </div>
          <div className="flex items-center gap-1">
            {task?.id && (
              <Link
                to="/automation"
                className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
                title={isEn ? 'Open in Automation' : '在自动化页面打开'}
                aria-label={isEn ? 'Open automation in new tab' : '在自动化页面打开'}
              >
                <MoreVertical className="h-4 w-4" />
              </Link>
            )}
            <button
              type="button"
              onClick={() => onClose?.()}
              className="rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              aria-label={isEn ? 'Close scheduled panel' : '关闭定时任务面板'}
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        {/* Schedule config */}
        {task && (
          <div className="space-y-2 border-b border-border bg-muted/30 px-4 py-3 text-xs">
            <div className="flex items-center gap-2 text-foreground">
              <CalendarDays className="h-3.5 w-3.5 text-primary" aria-hidden="true" />
              <span className="font-medium">{scheduleLabel || (isEn ? 'Custom schedule' : '自定义时间表')}</span>
            </div>
            {nextRun && (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                <span>
                  {isEn ? 'Next run' : '下次运行'} · {formatAbsoluteTime(nextRun)}
                </span>
              </div>
            )}
            <div className="flex items-center gap-2 text-muted-foreground">
              <ListChecks className="h-3.5 w-3.5" aria-hidden="true" />
              <span>
                {isEn ? 'Skip confirmations' : '跳过确认'} · {skipConfirm ? (isEn ? 'Always skip' : '总是跳过') : (isEn ? 'Off' : '关闭')}
              </span>
            </div>
          </div>
        )}

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-2 py-2">
          {loading && executions.length === 0 ? (
            <div className="flex justify-center py-8">
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            </div>
          ) : error ? (
            <div className="px-3 py-6 text-center text-xs text-muted-foreground">
              <p>{isEn ? "Couldn't load runs." : '加载失败。'}</p>
              <p className="mt-1 break-all font-mono text-[10px] opacity-70">{error}</p>
              <div className="mt-3 flex justify-center gap-3">
                <button
                  type="button"
                  onClick={refresh}
                  className="text-primary hover:underline"
                >
                  {isEn ? 'Retry' : '重试'}
                </button>
                <button
                  type="button"
                  onClick={() => onClose?.()}
                  className="text-muted-foreground hover:underline"
                >
                  {isEn ? 'Close' : '关闭'}
                </button>
              </div>
            </div>
          ) : !task ? (
            <div className="px-3 py-8 text-center text-xs text-muted-foreground">
              {isEn
                ? "This chat isn't tied to a scheduled task."
                : '此对话未关联定时任务。'}
            </div>
          ) : executions.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs text-muted-foreground">
              {isEn ? 'No past runs yet.' : '暂无历史运行。'}
            </div>
          ) : (
            <ul role="list" aria-label={isEn ? 'Past runs' : '历史运行'} className="space-y-1.5">
              {executions.map((ex) => {
                const meta = statusMeta(ex.status);
                const Icon = meta.Icon;
                const isOpen = expanded.has(ex.id);
                const focus = focusExecutionId === ex.id;
                // Live overlay (SSE): prefer streamed phase/steps over the
                // polled row while a run is in progress.
                const live = liveByExec[ex.id];
                const isInProgress = ex.status === 'running' || ex.status === 'queued';
                const effSteps = (live?.steps) || ex.activity_steps;
                // Always derive verb/title from the language-neutral FSM state,
                // so the headline matches the user's UI language. Backend SSE
                // also ships English verb/title — we ignore them here.
                const state = live?.phaseState || ex.current_phase;
                const headline = state ? phaseHeadline(state, t) : null;
                return (
                  <li
                    key={ex.id}
                    className={`overflow-hidden rounded-lg border transition-colors ${
                      focus ? 'border-primary/40 bg-primary/5' : 'border-border bg-card'
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => toggleRun(ex.id)}
                      aria-expanded={isOpen}
                      aria-controls={`run-detail-${ex.id}`}
                      className="flex w-full items-start gap-2.5 px-3 py-2 text-left transition-colors hover:bg-secondary/40"
                    >
                      <span className={`mt-0.5 ${meta.className}`} aria-hidden="true">
                        <Icon className="h-3.5 w-3.5" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 text-[11px] font-medium text-foreground">
                          <span>{formatRunDate(ex.started_at || ex.completed_at, 'en')}</span>
                          <span className="text-muted-foreground">·</span>
                          <span className="text-muted-foreground">{formatTimeOfDay(ex.started_at || ex.completed_at)}</span>
                        </div>
                        {ex.output_text && (
                          <p className="mt-0.5 line-clamp-2 text-[11px] leading-4 text-muted-foreground">
                            {excerpt(ex.output_text)}
                          </p>
                        )}
                      </div>
                      <span className="mt-0.5 text-muted-foreground" aria-hidden="true">
                        {isOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                      </span>
                    </button>
                    {isOpen && (
                      <div
                        id={`run-detail-${ex.id}`}
                        className="space-y-2 border-t border-border bg-muted/20 px-3 py-2.5"
                      >
                        {ex.output_text && (
                          <p className="whitespace-pre-wrap text-[12px] leading-5 text-foreground">
                            {ex.output_text}
                          </p>
                        )}
                        {isInProgress && headline && (
                          <div className="flex items-center gap-1.5 text-[11px] font-medium text-primary">
                            <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                            <span>{headline.verb}</span>
                            <span className="text-muted-foreground">· {headline.title}</span>
                          </div>
                        )}
                        {Array.isArray(effSteps) && effSteps.length > 0 && (
                          <RunChecklist steps={effSteps} status={ex.status} />
                        )}
                        {ex.held_back && (
                          <div className="flex items-start gap-2 rounded-md border border-amber-300/60 bg-amber-50 px-2.5 py-2 text-[11px] leading-4 text-amber-800 dark:border-amber-500/30 dark:bg-amber-950/40 dark:text-amber-200">
                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                            <span>
                              {isEn
                                ? 'Deliverable held back by the quality gate — execution confidence was below the shipping threshold. Trigger the run manually to retry or refine the task.'
                                : '产出文件被质量门禁拦截——执行置信度低于发布阈值。请手动触发运行以重试或调整任务。'}
                            </span>
                          </div>
                        )}
                        {ex.status === 'failed' && ex.error && (
                          <RunFailureActions
                            error={ex.error}
                            reason={ex.error_code || 'unknown'}
                          />
                        )}
                        {Array.isArray(ex.files) && ex.files.length > 0 && (
                          <ul role="list" aria-label={isEn ? 'Deliverables' : '产出文件'} className="space-y-1">
                            {ex.files.map((f) => (
                              <li
                                key={f.id}
                                className="flex items-center gap-2 rounded-md border border-border bg-background px-2 py-1.5"
                              >
                                <FileIcon file={f} />
                                <span className="min-w-0 flex-1 truncate text-[11px] text-foreground">{f.name}</span>
                                {f.file_url && (
                                  <a
                                    href={f.file_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="text-muted-foreground transition-colors hover:text-primary"
                                    aria-label={isEn ? `Download ${f.name}` : `下载 ${f.name}`}
                                  >
                                    <Download className="h-3.5 w-3.5" />
                                  </a>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                        {ex.error && ex.status !== 'failed' && (
                          <p className="text-[11px] text-muted-foreground">
                            {isEn ? 'Note:' : '备注:'} {ex.error}
                          </p>
                        )}
                        {(ex.conversation_id || ex.output_data?.conversation_id) && (
                          <Link
                            to={`/?conv=${encodeURIComponent(ex.conversation_id || ex.output_data?.conversation_id)}${
                              ex.output_data?.agent_name
                                ? `&agentName=${encodeURIComponent(ex.output_data.agent_name)}`
                                : ''
                            }${task?.project_id ? `&project=${encodeURIComponent(task.project_id)}` : ''}${
                              task?.project && task.project !== 'global'
                                ? `&projectName=${encodeURIComponent(task.project)}`
                                : ''
                            }`}
                            className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
                          >
                            <MessageSquare className="h-3 w-3" />
                            {isEn ? 'Open in chat' : '在对话中打开'}
                          </Link>
                        )}
                        {task.id && (
                          <Link
                            to={`/automation`}
                            className="inline-flex items-center gap-1 text-[11px] font-medium text-primary hover:underline"
                          >
                            {isEn ? 'Open in Automation' : '在自动化页面打开'}
                            <ExternalLink className="h-3 w-3" />
                          </Link>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </aside>
  );
}
