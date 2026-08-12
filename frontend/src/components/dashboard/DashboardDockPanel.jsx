import { useEffect, useRef, useState, useCallback } from 'react';
import { X, Undo2 } from 'lucide-react';
import {
  getDashboard,
  getDashboardByConversation,
  undoDashboardEdit,
} from '@/api/dashboards';
import DashboardViewer from './DashboardViewer';

/**
 * Persistent, resizable docked live-preview panel for the conversational
 * dashboard builder. Stays mounted across chat messages (unlike the old
 * per-message DashboardPopup). Auto-opens the conversation's bound dashboard
 * unless an explicit `dashboardId` is passed.
 *
 * The parent passes `viewerRef` so it can call `viewerRef.current.refresh()`
 * when an agent edit lands in the stream (see Chat.jsx watcher).
 */
export default function DashboardDockPanel({
  conversationId,
  dashboardId,
  onClose,
  viewerRef,
}) {
  const [dashboard, setDashboard] = useState(null);
  const [error, setError] = useState(null);
  const [width, setWidth] = useState(560);
  const localRef = useRef(null);
  const ref = viewerRef || localRef;

  const load = useCallback(async () => {
    setError(null);
    try {
      let d;
      if (dashboardId) {
        d = await getDashboard(dashboardId);
      } else if (conversationId) {
        d = await getDashboardByConversation(conversationId);
      }
      if (d) setDashboard(d);
      else setDashboard(null);
    } catch (e) {
      // 404 (no binding) is not fatal — panel simply stays empty.
      if (!String(e).includes('404')) setError(String(e));
      setDashboard(null);
    }
  }, [dashboardId, conversationId]);

  // Reload whenever the inputs change (initial mount + dashboardId switches).
  useEffect(() => {
    load();
  }, [load]);

  const handleUndo = useCallback(async () => {
    if (!dashboard?.id) return;
    try {
      const restored = await undoDashboardEdit(dashboard.id);
      setDashboard(restored);
      // Defer to give React a tick to mount the new dashboard before
      // requesting fresh data.
      setTimeout(() => ref.current?.refresh?.(), 0);
    } catch (e) {
      setError(String(e));
    }
  }, [dashboard, ref]);

  // Drag-to-resize handle (left edge — panel docks on the right). Min 360,
  // max 900. Mouse-up cleans up both window listeners.
  const onResizeStart = useCallback(
    (e) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = width;
      const move = (ev) => {
        const next = Math.max(360, Math.min(900, startW + (startX - ev.clientX)));
        setWidth(next);
      };
      const up = () => {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    },
    [width],
  );

  return (
    <div
      data-testid="dashboard-dock-panel"
      className="relative flex h-full flex-col border-l border-border bg-background"
      style={{ width }}
    >
      <div
        onMouseDown={onResizeStart}
        className="absolute left-0 top-0 z-10 h-full w-1 cursor-col-resize hover:bg-primary/30"
        aria-label="Resize panel"
        title="Drag to resize"
      />
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-medium">
            {dashboard?.name || 'Dashboard'}
          </span>
          {dashboard && (
            <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-[hsl(var(--chart-2))]/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[hsl(var(--chart-2))]">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
              </span>
              Live
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {dashboard?.can_edit && (
            <button
              onClick={handleUndo}
              disabled={!dashboard?.can_undo}
              aria-label="Undo last edit"
              title="Undo last edit"
              data-testid="dock-undo"
              className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-40"
            >
              <Undo2 className="h-3 w-3" />
              Undo
            </button>
          )}
          <button
            onClick={onClose}
            aria-label="Close panel"
            title="Close"
            className="rounded-md border border-border p-1 text-muted-foreground hover:bg-secondary hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1">
        {error && (
          <div className="p-3 text-xs text-destructive">{error}</div>
        )}
        {dashboard ? (
          <DashboardViewer
            ref={ref}
            dashboard={dashboard}
            canEdit={!!dashboard.can_edit}
            onSaved={setDashboard}
            onCopied={setDashboard}
          />
        ) : null}
      </div>
    </div>
  );
}
