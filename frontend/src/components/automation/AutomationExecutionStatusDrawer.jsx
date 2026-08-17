import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  X,
  Loader2,
  Square,
  MessageSquare,
  ChevronUp,
  ChevronDown,
  CheckCircle2,
  XCircle,
  Clock,
} from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import { toast } from '@/components/ui/use-toast';
import { authFetch } from '@/api/authFetch';
import { useAutomationExecutionStatus } from '@/lib/useAutomationExecutionStatus';
import { partitionEntries, removeExecution } from '@/lib/automationStatusBus';
import {
  stepText,
  formatSkillCall,
  formatElapsed,
} from '@/lib/skillExecutionFormatter';

const STATUS_STYLE = {
  completed: 'text-emerald-600 dark:text-emerald-400',
  succeeded: 'text-emerald-600 dark:text-emerald-400',
  failed: 'text-red-600 dark:text-red-400',
  cancelled: 'text-amber-600 dark:text-amber-400',
  timed_out: 'text-amber-600 dark:text-amber-400',
  skipped: 'text-zinc-500',
  running: 'text-blue-600 dark:text-blue-400',
  queued: 'text-zinc-500',
};

function StatusIcon({ status }) {
  if (status === 'completed' || status === 'succeeded') return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
  if (status === 'failed') return <XCircle className="h-4 w-4 text-red-500" />;
  if (status === 'cancelled' || status === 'timed_out') return <Clock className="h-4 w-4 text-amber-500" />;
  if (status === 'queued') return <Clock className="h-4 w-4 text-zinc-400" />;
  return <Loader2 className="h-4 w-4 animate-spin text-blue-500" />;
}

function SkillCalls({ calls }) {
  if (!calls || !calls.length) return null;
  return (
    <div className="mt-2 space-y-1 border-t border-border pt-2">
      {calls.slice(-3).map((sc, i) => (
        <div key={i} className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <span className={STATUS_STYLE[sc.status] || 'text-zinc-500'}>
            {sc.status === 'failed' ? <XCircle className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
          </span>
          <span className="font-medium text-foreground">{formatSkillCall(sc)}</span>
        </div>
      ))}
    </div>
  );
}

function ExecutionCard({ entry, onStop, onOpen, onDismiss, onCollapse, collapsed }) {
  const { t, lang } = useLanguage();
  const status = entry.status || 'queued';
  const steps = entry.steps || [];
  const lastSteps = collapsed ? [] : steps.slice(-4);
  const stopping = entry._stopping;

  return (
    <div className="rounded-xl border border-border bg-card shadow-lg">
      <div className="flex items-center gap-2 border-b border-border px-4 py-3">
        <StatusIcon status={status} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-foreground">
            {entry.taskName || (lang === 'en' ? 'Automation run' : '自动化执行')}
          </div>
          <div className="text-[11px] text-muted-foreground">
            {t.automationStatus?.status?.[status] || status}
            {entry.elapsed_sec != null && (
              <span> · {formatElapsed(entry.elapsed_sec)}</span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-secondary"
          title={t.automationStatus?.openChat || 'Open chat'}
        >
          <MessageSquare className="h-3 w-3" />
        </button>
        <button
          type="button"
          onClick={onStop}
          disabled={stopping}
          className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-secondary disabled:opacity-50"
          title={t.automationStatus?.stop || 'Stop'}
        >
          <Square className="h-3 w-3" />
        </button>
        <button
          type="button"
          onClick={onCollapse}
          className="rounded-md p-1 text-muted-foreground hover:bg-secondary"
          title={collapsed ? 'Expand' : 'Collapse'}
        >
          {collapsed ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="rounded-md p-1 text-muted-foreground hover:bg-secondary"
          title={t.automationStatus?.close || 'Close'}
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {!collapsed && (
        <div className="px-4 py-3">
          {lastSteps.length ? (
            <ol className="space-y-1.5">
              {lastSteps.map((s, i) => (
                <li key={i} className="flex items-start gap-2 text-xs text-muted-foreground">
                  <span className="mt-0.5 inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-blue-400" />
                  <span>{stepText(s)}</span>
                </li>
              ))}
            </ol>
          ) : (
            <div className="text-xs text-muted-foreground">
              {t.automationStatus?.queued || (lang === 'en' ? 'Queued…' : '排队中…')}
            </div>
          )}
          <SkillCalls calls={entry.skill_calls} />
        </div>
      )}
    </div>
  );
}

export default function AutomationExecutionStatusDrawer() {
  const entries = useAutomationExecutionStatus();
  const { active, history } = partitionEntries(entries);
  const navigate = useNavigate();
  const { t, lang } = useLanguage();
  const [collapsed, setCollapsed] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  if (!active.length && !history.length) return null;

  const activeEntry = active[0];

  async function stop(entry) {
    try {
      const res = await authFetch(
        `/api/automations/executions/${entry.executionId}/cancel`,
        { method: 'POST' },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      toast({
        title: lang === 'en' ? 'Stop failed' : '停止失败',
        description: (e && e.message) || 'Network error',
        variant: 'destructive',
      });
    }
  }

  function openChat(entry) {
    const session = entry.sessionId;
    if (!session) return;
    navigate(`/chat?session=${encodeURIComponent(session)}&execution=${encodeURIComponent(entry.executionId)}`);
  }

  return (
    <div
      data-testid="automation-execution-drawer"
      className="fixed bottom-4 right-4 z-[60] w-[340px] max-w-[calc(100vw-2rem)]"
    >
      {activeEntry ? (
        <ExecutionCard
          entry={activeEntry}
          onStop={() => stop(activeEntry)}
          onOpen={() => openChat(activeEntry)}
          onDismiss={() => removeExecution(activeEntry.executionId)}
          onCollapse={() => setCollapsed((c) => !c)}
          collapsed={collapsed}
        />
      ) : (
        <button
          type="button"
          onClick={() => setShowHistory((s) => !s)}
          className="flex w-full items-center gap-2 rounded-xl border border-border bg-card px-4 py-3 text-sm text-foreground shadow-lg hover:bg-secondary"
        >
          <Clock className="h-4 w-4 text-muted-foreground" />
          {t.automationStatus?.recentRuns || (lang === 'en' ? 'Recent runs' : '最近运行')}
          <span className="ml-auto rounded-full bg-secondary px-2 py-0.5 text-xs text-muted-foreground">
            {history.length}
          </span>
        </button>
      )}

      {showHistory && history.length > 0 && (
        <div className="mt-2 space-y-2">
          {history.map((e) => (
            <div key={e.executionId} className="rounded-xl border border-border bg-card p-3 shadow-lg">
              <div className="flex items-center gap-2">
                <StatusIcon status={e.status} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium text-foreground">
                    {e.taskName || (lang === 'en' ? 'Automation run' : '自动化执行')}
                  </div>
                  <div className="text-[11px] text-muted-foreground">
                    {t.automationStatus?.status?.[e.status] || e.status}
                    {e.elapsed_sec != null && <span> · {formatElapsed(e.elapsed_sec)}</span>}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => openChat(e)}
                  className="rounded-md p-1 text-muted-foreground hover:bg-secondary"
                  title={t.automationStatus?.openChat || 'Open chat'}
                >
                  <MessageSquare className="h-3 w-3" />
                </button>
                <button
                  type="button"
                  onClick={() => removeExecution(e.executionId)}
                  className="rounded-md p-1 text-muted-foreground hover:bg-secondary"
                  title={t.automationStatus?.close || 'Close'}
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
