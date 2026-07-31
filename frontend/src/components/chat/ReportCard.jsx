/**
 * ReportCard — premium in-chat report surface for Synexia FSM reports.
 *
 * Renders the `report_card_payload` that backend/app/services/synexia/finalize.py
 * attaches to assistant tool_calls.  Visual grammar matches the existing chat
 * cards (rounded-2xl, soft border, dark-mode aware) but elevates it with:
 *   - semi-transparent surface + backdrop blur (glassmorphism-lite)
 *   - shared ReportCardBody (chart + KPIs + insights + next_step + warnings)
 *   - shared ExportMenu for one-click PDF / PPTX / XLSX / CSV downloads
 *   - new "Open in side panel" button that pushes the report into the
 *     ReportSidePanel beside the chat (Manus-style)
 *
 * Usage in MessageBubble:
 *   <ReportCard
 *     payload={toolCall.results.report_card_payload}
 *     artifactId={toolCall.results.artifact_id}
 *     userSignal={toolCall.results.user_signal}
 *     onAction={(prompt) => onAction?.(prompt)}
 *     onOpenInPanel={(payload) => ...}            // NEW: payload-style opener
 *   />
 */

import {
  Sparkles, Database, Clock, Download, Loader2,
} from 'lucide-react';
import { motion } from 'framer-motion';
import { cn } from '@/lib/utils';
import ReportCardBody from '@/components/chat/ReportCardBody';

// ---------------------------------------------------------------------------
// Main ReportCard
// ---------------------------------------------------------------------------

export default function ReportCard({
  payload,
  artifactId,
  userSignal,
  fileExports,
  isPendingExport,
}) {
  if (!payload) return null;

  const {
    title = 'Report',
    source = '',
    generated_at = '',
  } = payload;

  const isExport = userSignal === 'export' || userSignal === 'download' || userSignal === 'save';

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, ease: 'easeOut' }}
      className={cn(
        'my-3 overflow-hidden rounded-2xl',
        'border border-border/70',
        'bg-gradient-to-br from-card/95 via-card/90 to-card/80',
        'shadow-[0_8px_30px_rgb(0_0_0/0.08)] backdrop-blur-md',
        'dark:shadow-[0_8px_30px_rgb(0_0_0/0.35)]'
      )}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3 border-b border-border/60 bg-gradient-to-r from-blue-500/5 via-indigo-500/5 to-transparent px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 shrink-0 text-blue-500" />
            <h3 className="truncate text-sm font-semibold text-foreground">{title}</h3>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
            {source && (
              <span className="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2 py-0.5 text-[10px] font-medium text-blue-600 dark:text-blue-300">
                <Database className="h-2.5 w-2.5" />
                {source}
              </span>
            )}
            {generated_at && (
              <span className="inline-flex items-center gap-1 text-[10px]">
                <Clock className="h-2.5 w-2.5" />
                {generated_at}
              </span>
            )}
            {userSignal && userSignal !== 'default' && (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium text-amber-600 dark:text-amber-300">
                {userSignal}
              </span>
            )}
          </div>
        </div>

        {/* ---- Action buttons in header ---- */}

        {/* Pending export indicator */}
        {isPendingExport && !fileExports && (
          <div className="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-blue-500/10 px-2.5 py-1">
            <Loader2 className="h-3 w-3 animate-spin text-blue-500" />
            <span className="text-[10px] font-medium text-blue-600 dark:text-blue-300">
              Generating {userSignal?.replace('export_', '').toUpperCase()}&hellip;
            </span>
          </div>
        )}
      </div>

      {/* ---- Shared body ---- */}
      <ReportCardBody payload={payload} />

      {/* Slim footer: a Download link only when the backend auto-produced a
          file export for this report (e.g. an export_* user signal). The
          old ExportMenu ("Export PDF" / "More" popover) and the header
          "View" button were removed — the report's chart body is the focus,
          and any DOCX/PPTX artifact gets its own Claude-style card with a
          Preview button elsewhere in the bubble. */}
      {isExport && artifactId && (
        <div className="flex items-center justify-end gap-2 border-t border-border/40 bg-secondary/10 px-4 py-2">
          <a
            href={`/api/artifacts/${artifactId}/download`}
            className="inline-flex items-center gap-1.5 rounded-lg border border-blue-500/40 bg-blue-500/10 px-2.5 py-1.5 text-xs font-medium text-blue-600 transition-colors hover:bg-blue-500/20 dark:text-blue-300"
          >
            <Download className="h-3.5 w-3.5" /> Download
          </a>
        </div>
      )}
    </motion.div>
  );
}
