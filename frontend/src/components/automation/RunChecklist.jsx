import {
  AlertTriangle, CheckCircle2, Circle, CircleDashed, Loader2, Mail, MinusCircle, XCircle,
} from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * RunChecklist — Manus-style action checklist with progress, used on the run
 * detail surface.
 *
 * Why a separate component when ``ActivitySteps`` already exists?  ActivitySteps
 * is tuned to streaming chat turns (line-through on failure, inline phase
 * headline, etc.).  The run detail view needs a calmer checklist that
 *
 *   1. surfaces a real progress bar (bound to completed-step ratio),
 *   2. supports a distinct ``warning`` status (degraded but recoverable),
 *   3. exposes explicit accessible labels per status so screen-reader users
 *      hear "Failure on step 3" / "Warning on step 1" / "Running step 4" etc.
 *
 * The expected step shape is identical to ``ActivitySteps`` so the backend's
 * existing ``activity_steps`` payload can be reused without changes:
 *
 *   { number, description, status, detail? }
 *
 * Statuses:
 *   - pending   → CircleDashed icon, neutral color
 *   - running   → Loader2 icon, primary color, ``aria-current="step"``
 *   - done      → CheckCircle2 icon, success color
 *   - warning   → AlertTriangle icon, amber color, with warning aria-label
 *   - failed    → XCircle icon, danger color, with failure aria-label
 *   - skipped   → MinusCircle icon, muted color
 */
const STATUS_META = {
  pending: {
    Icon: CircleDashed,
    rowClass: 'text-muted-foreground',
    iconClass: 'text-muted-foreground/60',
    label: 'Pending',
    ariaLabel: (n, d) => `Pending step ${n}: ${d}`,
  },
  running: {
    Icon: Loader2,
    rowClass: 'text-foreground font-medium',
    iconClass: 'text-primary animate-spin',
    label: 'Running',
    ariaLabel: (n, d) => `Running step ${n}: ${d}`,
  },
  done: {
    Icon: CheckCircle2,
    rowClass: 'text-foreground',
    iconClass: 'text-emerald-600',
    label: 'Done',
    ariaLabel: (n, d) => `Completed step ${n}: ${d}`,
  },
  warning: {
    Icon: AlertTriangle,
    rowClass: 'text-amber-700 dark:text-amber-300',
    iconClass: 'text-amber-500',
    label: 'Warning',
    ariaLabel: (n, d) => `Warning on step ${n}: ${d}`,
  },
  failed: {
    Icon: XCircle,
    rowClass: 'text-red-700 dark:text-red-300',
    iconClass: 'text-red-500',
    label: 'Failed',
    ariaLabel: (n, d) => `Failure on step ${n}: ${d}`,
  },
  skipped: {
    Icon: MinusCircle,
    rowClass: 'text-muted-foreground/80',
    iconClass: 'text-muted-foreground/60',
    label: 'Skipped',
    ariaLabel: (n, d) => `Skipped step ${n}: ${d}`,
  },
};

function normalizeStatus(status) {
  if (!status) return 'pending';
  const s = String(status).toLowerCase();
  if (s === 'completed' || s === 'success' || s === 'succeeded') return 'done';
  if (s === 'in_progress' || s === 'active') return 'running';
  if (s === 'error' || s === 'failure' || s === 'errored') return 'failed';
  if (s === 'warn' || s === 'degraded') return 'warning';
  if (STATUS_META[s]) return s;
  return 'pending';
}

function computeProgress(steps) {
  if (!steps || steps.length === 0) return { value: 0, max: 100 };
  const completed = steps.filter(
    (s) => normalizeStatus(s.status) === 'done' || normalizeStatus(s.status) === 'skipped',
  ).length;
  return { value: Math.round((completed / steps.length) * 100), max: 100 };
}

export default function RunChecklist({ steps, status, className = '' }) {
  const { t, lang } = useLanguage();
  const safeSteps = Array.isArray(steps) ? steps : [];
  const isEn = lang === 'en';
  const listLabel = isEn ? 'Run steps' : '运行步骤';
  const progressLabel = isEn ? 'Run progress' : '运行进度';

  if (safeSteps.length === 0) {
    return (
      <div
        className={`rounded-lg border border-dashed border-border bg-muted/40 px-3 py-3 text-xs text-muted-foreground ${className}`}
      >
        {isEn ? 'No step data available yet.' : '暂无步骤数据。'}
      </div>
    );
  }

  const progress = computeProgress(safeSteps);
  const completedCount = safeSteps.filter(
    (s) => normalizeStatus(s.status) === 'done' || normalizeStatus(s.status) === 'skipped',
  ).length;
  const failedCount = safeSteps.filter((s) => normalizeStatus(s.status) === 'failed').length;
  const warningCount = safeSteps.filter((s) => normalizeStatus(s.status) === 'warning').length;
  const statusLabel = isEn
    ? `${status === 'failed' ? 'Run failed' : status === 'completed' ? 'Run completed' : 'Run in progress'} · ${completedCount} of ${safeSteps.length} steps complete${warningCount ? ` · ${warningCount} warning${warningCount === 1 ? '' : 's'}` : ''}${failedCount ? ` · ${failedCount} failed` : ''}`
    : `${status === 'failed' ? '运行失败' : status === 'completed' ? '运行完成' : '运行中'} · ${completedCount}/${safeSteps.length} 步完成${warningCount ? ` · ${warningCount} 警告` : ''}${failedCount ? ` · ${failedCount} 失败` : ''}`;

  return (
    <section
      className={`rounded-xl border border-border bg-card/60 p-3 ${className}`}
      aria-label={listLabel}
    >
      <header className="mb-2 flex items-center justify-between gap-2 text-xs">
        <span className="font-medium text-foreground">{statusLabel}</span>
        <span className="text-[11px] text-muted-foreground">
          {isEn ? 'Progress' : '进度'}
        </span>
      </header>

      <div
        role="progressbar"
        aria-label={progressLabel}
        aria-valuenow={progress.value}
        aria-valuemin={0}
        aria-valuemax={progress.max}
        className="mb-3 h-1.5 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={`h-full rounded-full transition-all ${
            status === 'failed' ? 'bg-red-500' : 'bg-primary'
          }`}
          style={{ width: `${progress.value}%` }}
        />
      </div>

      <ol role="list" aria-label={listLabel} className="space-y-1.5">
        {safeSteps.map((step, idx) => {
          const norm = normalizeStatus(step.status);
          const meta = STATUS_META[norm];
          let Icon = meta.Icon;
          let iconClass = meta.iconClass;
          // Email-gateway steps (appended by the notification gateway worker) use
          // an envelope icon on success and a warning icon on failure.
          if (step.step_type === 'email_notification') {
            if (norm === 'done') {
              Icon = Mail;
              iconClass = 'text-emerald-600';
            } else if (norm === 'failed') {
              Icon = AlertTriangle;
              iconClass = 'text-amber-500';
            }
          }
          const stepNumber = step.number ?? idx + 1;
          const isCurrent = norm === 'running';
          return (
            <li
              key={stepNumber}
              role="listitem"
              aria-current={isCurrent ? 'step' : undefined}
              aria-label={meta.ariaLabel(stepNumber, step.description)}
              className={`flex items-start gap-2.5 rounded-md px-2 py-1.5 transition-colors ${
                isCurrent ? 'bg-primary/5 ring-1 ring-primary/20' : ''
              }`}
            >
              <span
                className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center ${iconClass}`}
                aria-hidden="true"
              >
                <Icon className="h-4 w-4" />
              </span>
              <div className="min-w-0 flex-1">
                <p className={`text-xs leading-5 ${meta.rowClass}`}>
                  <span className="mr-1.5 text-muted-foreground">{stepNumber}.</span>
                  {step.description}
                </p>
                {step.detail && (
                  <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
                    {step.detail}
                  </p>
                )}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
