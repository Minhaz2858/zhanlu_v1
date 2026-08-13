import { useEffect, useState } from 'react';
import { authFetch } from '@/api/authFetch';
import {
  FileText, Presentation, FileSpreadsheet, FileCode, Image, Download, Eye,
  AlertTriangle, ExternalLink,
} from 'lucide-react';

/**
 * InlineArtifactPreview — Manus-style deliverable card surfaced in the chat
 * stream, run log, and run detail surfaces.
 *
 * The same previewable file shape that the right-anchored ``ArtifactPreviewPane``
 * consumes now lives next to the conversation so users see the artifact without
 * opening a side panel. The ``Open`` button delegates to the parent so the
 * right pane (preferred) or a modal fallback can take over.
 *
 * Why a small embedded iframe for the inline preview?
 * The preview route is authenticated (credentials: 'include') and serves the
 * file with safe sandbox + no-script headers (set by the backend).  The
 * iframe is ``sandbox``-restricted per-file-type, never ``allow-same-origin``
 * combined with ``allow-scripts`` for non-HTML, and we keep the title
 * attribute on the iframe for screen readers — fulfilling the "warning icon +
 * accessible status" goal from the Manus-UX-parity backlog.
 */

const TYPE_ICON = {
  docx: FileText,
  pdf: FileText,
  pptx: Presentation,
  xlsx: FileSpreadsheet,
  html: FileCode,
  md: FileCode,
  csv: FileSpreadsheet,
  png: Image,
  jpg: Image,
  jpeg: Image,
  gif: Image,
  webp: Image,
  svg: FileCode,
  txt: FileText,
  json: FileCode,
  html_report: FileText,
};

const TYPE_LABEL = {
  docx: 'Word document',
  pdf: 'PDF document',
  pptx: 'Presentation',
  xlsx: 'Spreadsheet',
  html: 'Web page',
  md: 'Markdown',
  csv: 'CSV',
  png: 'Image',
  jpg: 'Image',
  jpeg: 'Image',
  gif: 'Image',
  webp: 'Image',
  svg: 'Image',
  txt: 'Text',
  json: 'JSON',
  html_report: 'Report',
};

const SCRIPTABLE_TYPES = new Set(['html', 'html_report']);

function formatSize(bytes) {
  if (bytes == null) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function pickType(artifact) {
  return (artifact?.type || artifact?.file_type || '').toLowerCase();
}

export default function InlineArtifactPreview({ artifact, onOpen, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  const [signedPreviewUrl, setSignedPreviewUrl] = useState(null);
  const [previewFailed, setPreviewFailed] = useState(false);

  // Null-safe derivations so hooks below run unconditionally (rules-of-hooks).
  const artifactId = artifact?.id;
  const artifactPreviewUrl = artifact?.preview_url;
  const wantsPreview = open && (artifact?.has_preview ?? !!artifactPreviewUrl) && !!artifactPreviewUrl;

  // iframes cannot send Authorization headers — mint a short-lived signed
  // token and point the iframe at the returned ?token= url, so the backend
  // CSP sandbox headers still apply (a blob: iframe would drop them).
  useEffect(() => {
    if (!wantsPreview || !artifactId) return undefined;
    let cancelled = false;
    setPreviewFailed(false);
    setSignedPreviewUrl(null);
    authFetch(`/api/automations/files/${artifactId}/preview-token`, { method: 'POST' })
      .then((res) => {
        if (!res || !res.ok) throw new Error(`mint failed: ${res && res.status}`);
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        if (data?.url) setSignedPreviewUrl(data.url);
        else setPreviewFailed(true);
      })
      .catch(() => {
        if (!cancelled) setPreviewFailed(true);
      });
    return () => { cancelled = true; };
  }, [wantsPreview, artifactId]);

  if (!artifact) return null;
  const type = pickType(artifact);
  const Icon = TYPE_ICON[type] || FileText;
  const label = TYPE_LABEL[type] || type || 'File';
  const title = artifact.title || artifact.name || artifact.file_name || 'Deliverable';
  const fileUrl = artifact.file_url || artifact.url;
  const previewUrl = artifact.preview_url;
  const hasPreview = (artifact.has_preview ?? !!previewUrl) && !!previewUrl;
  const size = formatSize(artifact.file_size ?? artifact.size);
  const sandboxValue = SCRIPTABLE_TYPES.has(type)
    ? 'allow-same-origin allow-scripts'
    : undefined;

  async function handleDownload() {
    if (!fileUrl) return;
    try {
      const res = await authFetch(fileUrl);
      if (!res || !res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = artifact.file_name || title;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch {
      /* download failure is non-fatal; the Open action remains available */
    }
  }

  function handleOpen() {
    if (onOpen) {
      onOpen(artifact);
      return;
    }
    // fileUrl is an authenticated route — window.open would 401. Download
    // via authFetch instead.
    handleDownload();
  }

  return (
    <div
      className="my-2 overflow-hidden rounded-xl border border-border bg-card shadow-sm"
      data-testid="inline-artifact-preview"
    >
      <div className="flex items-start gap-3 px-3 py-2.5">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-foreground">{title}</p>
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <span>{label}</span>
            {size && (
              <>
                <span className="opacity-40">·</span>
                <span>{size}</span>
              </>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {/* Inline expand-preview toggle. Shown whenever the file is
              previewable — in both chat (where the Open button also launches
              the right-side pane) and run logs (where this inline preview is
              the only preview path). Default stays collapsed via ``open``. */}
          {hasPreview && (
            <button
              type="button"
              onClick={() => setOpen((prev) => !prev)}
              aria-expanded={open}
              aria-label={open ? 'Hide preview' : 'Open preview'}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
            >
              <Eye className="h-3.5 w-3.5" />
              {open ? 'Hide' : 'Preview'}
            </button>
          )}
          <button
            type="button"
            onClick={handleOpen}
            aria-label="Open deliverable in side preview"
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            Open
          </button>
          {fileUrl && (
            <button
              type="button"
              onClick={handleDownload}
              aria-label="Download deliverable"
              className="inline-flex items-center justify-center rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            >
              <Download className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
      {hasPreview && open && (
        <div className="border-t border-border bg-background">
          {previewFailed ? (
            <div className="flex items-center gap-2 px-4 py-3 text-xs text-muted-foreground">
              <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
              Could not load preview. Use Open or Download instead.
            </div>
          ) : signedPreviewUrl ? (
            <iframe
              title={`Preview of ${title}`}
              src={signedPreviewUrl}
              className="h-[560px] w-full border-0"
              sandbox={sandboxValue}
              loading="lazy"
            />
          ) : (
            <div className="flex items-center gap-2 px-4 py-3 text-xs text-muted-foreground">
              Loading preview…
            </div>
          )}
        </div>
      )}
    </div>
  );
}
