/**
 * ReportSidePanel — Manus-style side preview pane for inline reports.
 *
 * Opens beside the chat when the user clicks the "Open in side panel"
 * button on an inline ReportCard. Renders the same ReportCardBody and
 * ExportMenu as the inline card so both surfaces stay visually identical.
 *
 * Layout:
 *   1. Sticky blurred header (title + source badge + Last modified + X close)
 *   2. Scrollable body (ReportCardBody)
 *   3. Sticky blurred footer (action chips + ExportMenu + Save-to-space)
 *
 * Props:
 *   payload      — ReportCardPayload (same shape the inline card receives)
 *   artifactId   — optional; passed through to ExportMenu
 *   generatedAt  — ISO timestamp string for the "Last modified" header
 *   onClose      — () => void   (when the user clicks X or presses Escape)
 *   onAction     — (prompt: string) => void   (action chip / follow-up click)
 *   sessionId    — optional; session for "Save to space"
 */

import { useEffect } from 'react';
import { X, Database } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { cn } from '@/lib/utils';
import ReportCardBody from '@/components/chat/ReportCardBody';

function formatLastModified(timestamp) {
  if (!timestamp) return null;
  try {
    const d = new Date(timestamp);
    if (isNaN(d.getTime())) return null;
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(d);
  } catch {
    return null;
  }
}

export default function ReportSidePanel({
  payload,
  generatedAt,
  onClose,
  onAction,
  sessionId,
}) {
  // Close on Escape
  useEffect(() => {
    if (!onClose) return;
    const handler = (e) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  if (!payload) return null;

  const {
    title = 'Report',
    source = '',
    actions = [],
  } = payload;

  const lastModified = formatLastModified(generatedAt);

  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={title + (generatedAt || '')}
        initial={{ opacity: 0, x: 24 }}
        animate={{ opacity: 1, x: 0 }}
        exit={{ opacity: 0, x: 24 }}
        transition={{ duration: 0.2, ease: 'easeOut' }}
        className="flex h-full flex-col overflow-hidden rounded-2xl border border-border/70 bg-gradient-to-br from-card/95 via-card/90 to-card/80 shadow-[0_8px_30px_rgb(0_0_0/0.08)] backdrop-blur-md dark:shadow-[0_8px_30px_rgb(0_0_0/0.35)]"
      >
        {/* ── Header (sticky, blurred) ── */}
        <div className="sticky top-0 z-10 shrink-0 backdrop-blur-xl">
          <div className="flex items-start justify-between gap-3 border-b border-border/60 bg-gradient-to-r from-blue-500/5 via-indigo-500/5 to-transparent px-4 py-3">
            <div className="min-w-0 flex-1">
              <h3 className="truncate text-sm font-semibold text-foreground">
                {title}
              </h3>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                {source && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2 py-0.5 text-[10px] font-medium text-blue-600 dark:text-blue-300">
                    <Database className="h-2.5 w-2.5" />
                    {source}
                  </span>
                )}
                {lastModified && (
                  <span className="text-[10px] text-muted-foreground">
                    Last modified: {lastModified}
                  </span>
                )}
              </div>
            </div>

            {/* Close button */}
            <button
              onClick={onClose}
              className="inline-flex shrink-0 items-center justify-center rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              title="Close panel (Esc)"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* ── Body (scrollable) ── */}
        <div className="flex-1 overflow-y-auto">
          <ReportCardBody payload={payload} onAction={onAction} />
        </div>

        {/* ── Footer (sticky, blurred) ── */}
        <div className="sticky bottom-0 z-10 shrink-0 border-t border-border/40 bg-card/80 backdrop-blur-xl">
          <div className="flex flex-col gap-2 px-4 py-2.5">
            {/* Action chips */}
            {actions && actions.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {actions.slice(0, 4).map((a, i) => (
                  <button
                    key={i}
                    onClick={() => {
                      if (a?.prompt) onAction?.(a.prompt);
                    }}
                    className={cn(
                      'inline-flex items-center gap-1 rounded-full border border-border/60',
                      'bg-card/60 px-2.5 py-1 text-[11px] text-foreground/90',
                      'transition-colors hover:bg-secondary/80 hover:text-foreground'
                    )}
                  >
                    {a.label}
                  </button>
                ))}
              </div>
            )}

            {/* Save-to-space (ExportMenu removed — export affordances live on
                the artifact cards now). */}
            {sessionId && (
              <div className="flex items-center justify-end gap-2">
                <button
                  onClick={() => {
                    onAction?.('Save this report to my space');
                  }}
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-lg border border-blue-500/40',
                    'bg-blue-500/5 px-3 py-1.5 text-xs font-medium text-blue-600',
                    'transition-colors hover:bg-blue-500/10 dark:text-blue-300'
                  )}
                >
                  Save to space
                </button>
              </div>
            )}
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}
