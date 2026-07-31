import { useState, useEffect } from 'react';
import { FileText, Presentation, Code, Download, FileType, AlertTriangle, Loader2, BookOpen, ExternalLink } from 'lucide-react';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from '@/components/ui/sheet';
import DocxArtifactPreview from './DocxArtifactPreview';
import PptxArtifactPreview from './PptxArtifactPreview';
import PdfFaithfulPreview from './PdfFaithfulPreview';

const TYPE_ICON = {
  docx: FileText,
  pdf: FileText,
  pptx: Presentation,
  html: Code,
};

const TYPE_LABEL = {
  docx: 'Word Document',
  pdf: 'PDF Document',
  pptx: 'PowerPoint',
  html: 'HTML Page',
};

function formatSize(bytes) {
  if (bytes == null) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ArtifactPreviewSheet({ artifact, open, onOpenChange }) {
  const [viewMode, setViewMode] = useState('document');
  const _artifactIdForReset = artifact?.id || artifact?.artifact_id;
  useEffect(() => { setViewMode('document'); }, [_artifactIdForReset]);

  if (!artifact) return null;

  const Icon = TYPE_ICON[artifact.type] || FileType;
  const label = TYPE_LABEL[artifact.type] || artifact.type;
  const size = formatSize(artifact.file_size);
  const title = artifact.title || artifact.file_name || 'Artifact';
  const previewUrl = artifact.preview_url;
  const fileUrl = artifact.file_url || artifact.download_url;
  const hasPreview = artifact.has_preview && previewUrl;
  const artifactId = artifact.id || artifact.artifact_id;

  // Sidecar rich-HTML summary (sibling HTML artifact). When present,
  // the user can toggle between "Document" (faithful PDF) and
  // "Summary" (rich HTML).
  const previewArtifactId = artifact.preview_artifact_id || artifact.previewArtifactId;
  const sidecarPreviewUrl = previewArtifactId
    ? `/api/artifacts/${previewArtifactId}/preview`
    : null;

  const isOfficeDoc = artifact.type === 'docx' || artifact.type === 'pptx' || artifact.type === 'xlsx';
  const showToggle = isOfficeDoc && !!sidecarPreviewUrl;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="flex flex-col p-0 sm:max-w-xl lg:max-w-2xl"
      >
        {/* Header strip */}
        <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
              <Icon className="h-4 w-4" />
            </div>
            <div className="min-w-0">
              <SheetTitle className="truncate text-sm font-medium">{title}</SheetTitle>
              <SheetDescription className="text-[11px]">
                {label}
                {size && <span className="opacity-60"> &middot; {size}</span>}
              </SheetDescription>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {showToggle && (
              <div className="flex items-center rounded-md border border-border bg-secondary/40 p-0.5">
                <button
                  onClick={() => setViewMode('document')}
                  className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
                    viewMode === 'document'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  Document
                </button>
                <button
                  onClick={() => setViewMode('summary')}
                  className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
                    viewMode === 'summary'
                      ? 'bg-background text-foreground shadow-sm'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  Summary
                </button>
              </div>
            )}
            {artifact.ms_word_open_url && (
              <a
                href={artifact.ms_word_open_url}
                target="_blank"
                rel="noreferrer"
                title="Open in Microsoft Word Online (sends the file to Microsoft)"
                className="inline-flex items-center gap-1 rounded-md border border-blue-500/40 bg-blue-500/10 px-2.5 py-1.5 text-xs font-medium text-blue-600 transition-colors hover:bg-blue-500/20 dark:text-blue-300"
              >
                <BookOpen className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Open in Word</span>
              </a>
            )}
            {fileUrl && (
              <a
                href={fileUrl}
                download={artifact.file_name}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
              >
                <Download className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Download</span>
              </a>
            )}
          </div>
        </div>

        {/* Preview body */}
        <div className="flex-1 overflow-hidden">
          {isOfficeDoc ? (
            /* Office-file artifacts: faithful PDF by default, with a
               toggle to the rich-HTML summary when a sidecar exists. */
            viewMode === 'summary' && sidecarPreviewUrl ? (
              <iframe
                src={sidecarPreviewUrl}
                title={`${title} — Summary`}
                className="h-full w-full border-0"
              />
            ) : (
              <PdfFaithfulPreview
                artifactId={artifactId}
                title={title}
                downloadUrl={fileUrl}
                fallback={
                  artifact.type === 'docx' ? (
                    <DocxArtifactPreview
                      artifactId={artifactId}
                      outline={artifact.preview_outline || []}
                      title={title}
                      downloadUrl={fileUrl}
                    />
                  ) : (
                    <PptxArtifactPreview
                      artifactId={artifactId}
                      outline={artifact.preview_outline || []}
                      title={title}
                      downloadUrl={fileUrl}
                    />
                  )
                }
              />
            )
          ) : hasPreview ? (
            <iframe
              src={previewUrl}
              title={title}
              className="h-full w-full border-0"
              sandbox={
                artifact.type === 'html'
                  ? 'allow-same-origin allow-scripts'
                  : undefined
              }
            />
          ) : (artifact.is_pending || (!previewUrl && !fileUrl)) ? (
            <PendingPreview title={title} label={label} />
          ) : (
            <NoPreviewFallback
              title={title}
              label={label}
              size={size}
              fileUrl={fileUrl}
              fileName={artifact.file_name}
            />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

/**
 * Shown when the file is still being generated by the sandbox skill.
 * Displays a centered spinner with "Generating preview…" instead of
 * a static error fallback so the user knows work is in progress.
 */
function PendingPreview({ title, label }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-blue-500/10">
        <Loader2 className="h-6 w-6 animate-spin text-blue-500" />
      </div>
      <div>
        <p className="text-sm font-medium text-foreground">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground">Generating preview…</p>
        <p className="mt-0.5 text-[11px] text-muted-foreground/60">{label}</p>
      </div>
    </div>
  );
}

/**
 * Fallback shown when LibreOffice (or another preview converter) is not
 * available and a visual preview cannot be generated. Still exposes the
 * file metadata and a forced-download button.
 */
function NoPreviewFallback({ title, label, size, fileUrl, fileName }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-amber-500/10">
        <AlertTriangle className="h-6 w-6 text-amber-500" />
      </div>
      <div>
        <p className="text-sm font-medium text-foreground">{title}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Preview unavailable for this file type.
        </p>
        <p className="mt-0.5 text-[11px] text-muted-foreground/60">
          {label}
          {size ? ` · ${size}` : ''}
        </p>
      </div>
      {fileUrl && (
        <a
          href={fileUrl}
          download={fileName}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
        >
          <Download className="h-4 w-4" />
          Download File
        </a>
      )}
    </div>
  );
}
