/**
 * ExportMenu — shared export toolbar for inline ReportCard and
 * ReportSidePanel. Extracted from ReportCard.jsx so both surfaces
 * share the same export pipeline.
 *
 * Props:
 *   artifactId    — optional; when missing, CSV falls back to client-side
 *   payload       — ReportCardPayload (for title + rows for sandbox exports)
 *   onAskFollowUp — () => void   (Ask follow-up button)
 *   onExported    — (result) => void
 *   busy          — boolean (from parent, if needed)
 */

import { useMemo, useState, useCallback } from 'react';
import {
  Download, FileText, Presentation, FileSpreadsheet, FileType,
  ArrowRight, Sparkles, Loader2,
} from 'lucide-react';
import { AnimatePresence, motion } from 'framer-motion';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import useExportArtifact from '@/lib/useExportArtifact';
import { authFetch } from '@/api/authFetch';

const EXPORT_FORMATS = [
  { key: 'pdf',  label: 'PDF',  icon: FileText,        color: 'text-red-500' },
  { key: 'docx', label: 'DOCX', icon: FileText,        color: 'text-blue-500' },
  { key: 'pptx', label: 'PPTX', icon: Presentation,    color: 'text-orange-500' },
  { key: 'xlsx', label: 'XLSX', icon: FileSpreadsheet, color: 'text-green-500' },
  { key: 'csv',  label: 'CSV',  icon: FileType,        color: 'text-blue-500' },
];

export default function ExportMenu({ artifactId, payload, onAskFollowUp, onExported }) {
  const [busy, setBusy] = useState(null);
  const [open, setOpen] = useState(false);

  const exportRows = useMemo(() => {
    if (payload?.chart?.data && Array.isArray(payload.chart.data) && payload.chart.data.length > 0) {
      return payload.chart.data;
    }
    if (Array.isArray(payload?.kpis) && payload.kpis.length > 0) {
      return payload.kpis.map((k) => ({
        label: k.label,
        value: k.value,
        ...(k.caption ? { caption: k.caption } : {}),
      }));
    }
    return [];
  }, [payload]);

  const { exportArtifact, busy: exportBusy } = useExportArtifact({
    onSuccess: (result) => {
      onExported?.(result);
      if (result?.download_url) {
        const a = document.createElement('a');
        a.href = result.download_url;
        a.download = `${(payload?.title || 'report').replace(/\s+/g, '_')}.${result.format}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        toast.success(`${result.format.toUpperCase()} ready`);
      }
    },
    onError: (e) => toast.error(`Export failed: ${e.message}`),
  });

  const handleExport = useCallback(async (format) => {
    const needsSandbox = ['docx', 'pptx', 'xlsx', 'md'].includes(format);

    if (!artifactId) {
      if (format === 'csv' && exportRows.length > 0) {
        const cols = Object.keys(exportRows[0] || {});
        const csv = [
          cols.join(','),
          ...exportRows.map((r) => cols.map((c) => JSON.stringify(r[c] ?? '')).join(',')),
        ].join('\n');
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${(payload?.title || 'report').replace(/\s+/g, '_')}.csv`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        toast.success('CSV downloaded');
        return;
      }
      toast.error('Export unavailable \u2014 artifact not yet built');
      return;
    }

    if (needsSandbox) {
      if (exportRows.length === 0) {
        toast.error('No data to export');
        return;
      }
      setBusy(format);
      try {
        await exportArtifact({
          artifactId,
          format,
          rows: exportRows,
          title: payload?.title || 'Report',
          instructions: payload?.summary || '',
        });
      } finally {
        setBusy(null);
        setOpen(false);
      }
      return;
    }

    setBusy(format);
    try {
      const url = `/api/artifacts/${artifactId}/download?format=${format}`;
      const res = await authFetch(url, { method: 'GET' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const dl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = dl;
      a.download = `${(payload?.title || 'report').replace(/\s+/g, '_')}.${format}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(dl);
      toast.success(`${format.toUpperCase()} downloaded`);
    } catch (e) {
      toast.error(`Export failed: ${e.message}`);
    } finally {
      setBusy(null);
      setOpen(false);
    }
  }, [artifactId, exportRows, exportArtifact, payload]);

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Primary Export (PDF) */}
      <button
        onClick={() => handleExport('pdf')}
        disabled={busy === 'pdf' || exportBusy}
        className={cn(
          'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium',
          'bg-gradient-to-r from-blue-600 to-indigo-600 text-white shadow-sm',
          'transition-all hover:shadow-md hover:from-blue-500 hover:to-indigo-500',
          'disabled:opacity-60'
        )}
      >
        {busy === 'pdf' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
        Export PDF
      </button>

      {/* Secondary exports — popover */}
      <div className="relative">
        <button
          onClick={() => setOpen((v) => !v)}
          className="inline-flex items-center gap-1 rounded-lg border border-border bg-card/60 px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary/80"
        >
          <ArrowRight className="h-3.5 w-3.5 -rotate-90" />
          More
        </button>
        <AnimatePresence>
          {open && (
            <motion.div
              initial={{ opacity: 0, y: -4, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.96 }}
              transition={{ duration: 0.12 }}
              className="absolute right-0 z-20 mt-1.5 w-44 overflow-hidden rounded-lg border border-border bg-card/95 shadow-lg backdrop-blur-md"
            >
              {EXPORT_FORMATS.map((f) => {
                const Icon = f.icon;
                const isBusy = busy === f.key || exportBusy;
                return (
                  <button
                    key={f.key}
                    onClick={() => handleExport(f.key)}
                    disabled={isBusy}
                    className="flex w-full items-center gap-2 px-3 py-2 text-xs text-foreground transition-colors hover:bg-secondary disabled:opacity-60"
                  >
                    {isBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Icon className={cn('h-3.5 w-3.5', f.color)} />}
                    Export as {f.label}
                  </button>
                );
              })}
              <div className="my-1 border-t border-border" />
              <button
                onClick={() => { setOpen(false); onAskFollowUp?.(); }}
                className="flex w-full items-center gap-2 px-3 py-2 text-xs text-foreground transition-colors hover:bg-secondary"
              >
                <Sparkles className="h-3.5 w-3.5 text-purple-500" />
                Ask follow-up
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
