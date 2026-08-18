import { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { authFetch } from '@/api/authFetch';
import {
  FileText, Presentation, FileSpreadsheet, FileCode, Image, Download, Eye,
  AlertTriangle, ExternalLink,
} from 'lucide-react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
  DialogFooter, DialogClose,
} from '@/components/ui/dialog';
import PptxArtifactPreview from './PptxArtifactPreview';
import DocxArtifactPreview from './DocxArtifactPreview';
import PdfFaithfulPreview from './PdfFaithfulPreview';

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

// Office formats browsers cannot render from raw bytes inside an iframe. When
// the modal is the only preview surface, these mount the same dedicated
// previewers the right pane uses (mirrors ArtifactPreviewPane's dispatch).
const OFFICE_TYPES_REQUIRING_DEDICATED_PREVIEWER = new Set(['pptx', 'docx', 'xlsx']);

function formatSize(bytes) {
  if (bytes == null) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function pickType(artifact) {
  return (artifact?.type || artifact?.file_type || '').toLowerCase();
}

export default function InlineArtifactPreview({ artifact, onOpen, defaultOpen = false, openInModal = true }) {
  const [open, setOpen] = useState(defaultOpen);
  const [modalOpen, setModalOpen] = useState(false);
  const [signedPreviewUrl, setSignedPreviewUrl] = useState(null);
  const [previewFailed, setPreviewFailed] = useState(false);

  // Null-safe derivations so hooks below run unconditionally (rules-of-hooks).
  const artifactId = artifact?.id;
  const artifactPreviewUrl = artifact?.preview_url;
  const artifactType = pickType(artifact);
  // Chat / message-bubble callers pass `onOpen` (wired to ArtifactPreviewPane
  // on the right side); automation-run logs pass neither and rely on the modal.
  // `openInModal === false` is the legacy opt-out meaning "the parent owns the
  // preview surface". Either way, an `onOpen` parent means the right pane wins.
  const hasRightPane = openInModal === false || !!onOpen;
  // Office formats can't render from raw bytes in an iframe — the modal mounts
  // the dedicated previewers (PptxArtifactPreview / DocxArtifactPreview /
  // PdfFaithfulPreview) instead, so no signed token is needed for them.
  const isOfficeRequiringDedicatedPreviewer = OFFICE_TYPES_REQUIRING_DEDICATED_PREVIEWER.has(artifactType);
  // Inline expand is only used when NOT in modal mode (opt-out path). In modal
  // mode the signed token is minted when the Dialog opens instead.
  const wantsPreview = !openInModal
    && open
    && (artifact?.has_preview ?? !!artifactPreviewUrl)
    && !!artifactPreviewUrl;
  const wantsModalPreview = openInModal
    && modalOpen
    && !isOfficeRequiringDedicatedPreviewer
    && (artifact?.has_preview ?? !!artifactPreviewUrl)
    && !!artifactPreviewUrl;
  const needsSignedToken = wantsPreview || wantsModalPreview;

  // iframes cannot send Authorization headers — mint a short-lived signed
  // token and point the iframe at the returned ?token= url, so the backend
  // CSP sandbox headers still apply (a blob: iframe would drop them).
  useEffect(() => {
    if (!needsSignedToken || !artifactId) return undefined;
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
  }, [needsSignedToken, artifactId]);

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
    // Right-pane path (chat / message bubble / any caller that hands us an
    // `onOpen`): the parent owns the surface — delegate, never pop a modal.
    if (hasRightPane) {
      onOpen?.(artifact);
      return;
    }
    // Modal-only context (e.g. automation run log): open the popup preview
    // when the file is previewable; otherwise fall back to download.
    if (hasPreview) {
      setModalOpen(true);
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
          {/* Inline expand-preview toggle. Only rendered in the opt-out path
              (openInModal=false); in modal mode the Open button opens the
              popup preview instead. */}
          {!openInModal && hasPreview && (
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
            aria-label={hasRightPane ? 'Open deliverable in side preview' : 'Open file in preview popup'}
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
      {!openInModal && hasPreview && open && (
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

      {/* Modal preview popup — the default UX. Open mounts this Dialog and
          mints the signed token via the shared effect; the footer offers a
          Download button for convenience and a Close button to dismiss. */}
      {openInModal && (
        <Dialog open={modalOpen} onOpenChange={setModalOpen}>
          <DialogContent className="max-w-5xl w-[95vw] h-[85vh] flex flex-col p-0 gap-0 sm:rounded-xl overflow-hidden">
            <DialogHeader className="px-5 py-4 border-b border-border">
              <DialogTitle className="truncate pr-8 text-base">{title}</DialogTitle>
              <DialogDescription className="sr-only">
                Preview of {title}
              </DialogDescription>
            </DialogHeader>
            <div className="flex-1 min-h-0 overflow-hidden bg-background">
              {isOfficeRequiringDedicatedPreviewer ? (
                // Browsers can't render office bytes inside an iframe — mount
                // the same dedicated previewers the right pane uses. No signed
                // token is minted for these (wantsModalPreview excludes them).
                artifactType === 'pptx' ? (
                  <PptxArtifactPreview
                    key={`modal-pptx-${artifactId}`}
                    artifactId={artifactId}
                    outline={artifact.preview_outline || []}
                    title={title}
                    downloadUrl={fileUrl}
                  />
                ) : artifactType === 'docx' ? (
                  <DocxArtifactPreview
                    key={`modal-docx-${artifactId}`}
                    artifactId={artifactId}
                    outline={artifact.preview_outline || []}
                    title={title}
                    downloadUrl={fileUrl}
                  />
                ) : (
                  <PdfFaithfulPreview
                    key={`modal-xlsx-${artifactId}`}
                    artifactId={artifactId}
                    previewUrl={fileUrl && fileUrl.includes('/api/automations/files/')
                      ? `/api/automations/files/${artifactId}/preview`
                      : null}
                    title={title}
                    downloadUrl={fileUrl}
                  />
                )
              ) : previewFailed ? (
                <div className="flex h-full items-center justify-center gap-2 px-4 py-3 text-sm text-muted-foreground">
                  <AlertTriangle className="h-4 w-4 text-amber-500" />
                  Could not load preview. Use Download instead.
                </div>
              ) : signedPreviewUrl ? (
                <iframe
                  title={`Preview of ${title}`}
                  src={signedPreviewUrl}
                  className="h-full w-full border-0"
                  sandbox={sandboxValue}
                  loading="lazy"
                />
              ) : (
                <div className="flex h-full items-center justify-center gap-2 px-4 py-3 text-sm text-muted-foreground">
                  Loading preview…
                </div>
              )}
            </div>
            <DialogFooter className="px-5 py-3 border-t border-border flex-row justify-end gap-2">
              <DialogClose asChild>
                <button
                  type="button"
                  aria-label="Close preview popup"
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
                >
                  Close
                </button>
              </DialogClose>
              <button
                type="button"
                onClick={handleDownload}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90"
              >
                <Download className="h-3.5 w-3.5" />
                Download
              </button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  );
}

InlineArtifactPreview.propTypes = {
  artifact: PropTypes.object,
  onOpen: PropTypes.func,
  defaultOpen: PropTypes.bool,
  openInModal: PropTypes.bool,
};
