import { Link } from 'react-router-dom';
import { AlertTriangle, Wallet, ListChecks, LifeBuoy } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';

/**
 * RunFailureActions — Manus-style recovery card surfaced on failed runs.
 *
 * The run surface can already display the raw error text; this card adds the
 * one piece users want most on a failure: a clear next step.  Each ``reason``
 * maps to a specific destination, so quota failures link to the cost tab,
 * approval pauses link to the run history (so the user can resume), and
 * unknown errors fall back to the run history with a support hint.
 *
 * Why not a CTA that opens a real billing flow?  Zhanlu's billing backend isn't
 * built yet (only the ``cost`` settings tab is real).  The CTA points to the
 * cost settings tab today; the day billing lands, swap ``link`` here and the
 * rest of the surface keeps working.
 */

const REASON_META = {
  quota: {
    icon: Wallet,
    accent: 'border-amber-200 bg-amber-50/80 dark:border-amber-900/50 dark:bg-amber-950/40',
    iconClass: 'text-amber-600',
    titleEn: 'Run was stopped because the upstream quota or credit ran out.',
    titleZh: '运行因上游配额或信用额度用尽而中止。',
    ctaEn: 'Open cost settings',
    ctaZh: '打开成本设置',
    to: '/settings#cost',
  },
  approval: {
    icon: ListChecks,
    accent: 'border-sky-200 bg-sky-50/80 dark:border-sky-900/50 dark:bg-sky-950/40',
    iconClass: 'text-sky-600',
    titleEn: 'This run is paused waiting for your approval.',
    titleZh: '运行正在等待你的批准。',
    ctaEn: 'Open the run history',
    ctaZh: '打开运行历史',
    to: '/automation',
  },
  network: {
    icon: AlertTriangle,
    accent: 'border-orange-200 bg-orange-50/80 dark:border-orange-900/50 dark:bg-orange-950/40',
    iconClass: 'text-orange-600',
    titleEn: 'Network or upstream timeout — the run will retry automatically.',
    titleZh: '网络或上游超时 — 运行将自动重试。',
    ctaEn: 'View run history',
    ctaZh: '查看运行历史',
    to: '/automation',
  },
};

const FALLBACK = {
  icon: LifeBuoy,
  accent: 'border-red-200 bg-red-50/80 dark:border-red-900/50 dark:bg-red-950/40',
  iconClass: 'text-red-600',
  titleEn: 'Run failed. See the error below and review the run history for next steps.',
  titleZh: '运行失败。请查看下方错误并检查运行历史以获取下一步。',
  ctaEn: 'View run history',
  ctaZh: '查看运行历史',
  to: '/automation',
};

export default function RunFailureActions({ error, reason, error_code }) {
  const { lang } = useLanguage();
  const isEn = lang === 'en';
  const meta = REASON_META[reason] || FALLBACK;
  const Icon = meta.icon;
  const title = isEn ? meta.titleEn : meta.titleZh;
  const cta = isEn ? meta.ctaEn : meta.ctaZh;
  const errorLabel = isEn ? 'Error' : '错误';

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="run-failure-actions"
      data-reason={reason || 'unknown'}
      className={`mt-2 flex items-start gap-2.5 rounded-lg border p-2.5 text-xs ${meta.accent}`}
    >
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${meta.iconClass}`} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="text-[12px] font-medium text-foreground">{title}</p>
        {error && (
          <p className="mt-1 text-[11px] text-muted-foreground">
            <span className="font-medium text-foreground">{errorLabel}:</span> {error}
          </p>
        )}
        {error_code && (
          <p className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            {error_code}
          </p>
        )}
        <div className="mt-2 flex items-center gap-2">
          <Link
            to={meta.to}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            {cta}
          </Link>
        </div>
      </div>
    </div>
  );
}
