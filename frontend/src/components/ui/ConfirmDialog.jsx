import { Loader2 } from 'lucide-react';

/**
 * In-app confirmation dialog.
 *
 * Why not window.confirm? The native dialog can be blocked by some
 * embedded webviews, browser settings (autoplay / popups), or even
 * just delayed by the OS — and when it's blocked the click appears
 * to do nothing, which is the worst possible UX for a destructive
 * action. This modal renders in-DOM so the user always sees it.
 *
 * Props:
 *   - open:        boolean (controls visibility)
 *   - title:       string
 *   - message:     string (multi-line ok)
 *   - confirmLabel: string (button text)
 *   - destructive: boolean (red button if true)
 *   - onConfirm:   function
 *   - onCancel:    function
 *   - busy:        boolean (disables buttons while the parent is
 *                  doing async work — kept out of state to avoid
 *                  a second source of truth)
 */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  busy = false,
  onConfirm,
  onCancel,
}) {
  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={() => !busy && onCancel?.()}
    >
      <div
        className="w-full max-w-md rounded-lg border border-border bg-card p-5 shadow-lg"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        {message && (
          <p className="mt-2 whitespace-pre-line text-xs text-muted-foreground">{message}</p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={() => !busy && onCancel?.()}
            disabled={busy}
            className="rounded-md px-3 py-1.5 text-xs text-muted-foreground hover:bg-secondary disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={() => !busy && onConfirm?.()}
            disabled={busy}
            className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs text-white disabled:opacity-50 ${
              destructive
                ? 'bg-red-500 hover:bg-red-600'
                : 'bg-primary hover:bg-primary/90 text-primary-foreground'
            }`}
          >
            {busy && <Loader2 className="h-3 w-3 animate-spin" />}
            {busy ? 'Working...' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
