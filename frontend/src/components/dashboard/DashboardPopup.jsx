import { useEffect, useState } from 'react';
import { X, Loader2 } from 'lucide-react';
import { getDashboard } from '@/api/dashboards';
import DashboardViewer from './DashboardViewer';

export default function DashboardPopup({ dashboardId, variant = 'chat', onClose }) {
  const [dashboard, setDashboard] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    getDashboard(dashboardId)
      .then((d) => { if (alive) setDashboard(d); })
      .catch((e) => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, [dashboardId]);

  const overlay = variant === 'chat'
    ? 'fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-border bg-background shadow-xl'
    : 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4';

  const inner = variant === 'chat'
    ? 'flex h-full flex-col'
    : 'flex max-h-[90vh] w-full max-w-4xl flex-col rounded-xl border border-border bg-background shadow-xl';

  return (
    <div
      data-testid="dashboard-popup"
      className={overlay}
      onClick={variant === 'myspace' ? onClose : undefined}
    >
      <div className={inner} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            {dashboard ? (
              <>
                <span className="truncate font-heading text-base font-semibold">
                  {dashboard.name}
                </span>
                <LiveBadge />
              </>
            ) : (
              <span className="text-xs text-muted-foreground">
                {variant === 'chat' ? 'Live Dashboard' : 'Dashboard'}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1">
          {error ? (
            <div className="p-4 text-sm text-destructive">{error}</div>
          ) : !dashboard ? (
            <div className="flex h-full items-center justify-center">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : (
            <DashboardViewer
              dashboard={dashboard}
              canEdit={!!dashboard.can_edit}
              polling
              onSaved={setDashboard}
              onCopied={setDashboard}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function LiveBadge() {
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-[hsl(var(--chart-2))]/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--chart-2))]">
      <span className="relative flex h-1.5 w-1.5">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
      </span>
      Live
    </span>
  );
}
