import {
  FileText, Presentation, Code, Eye, Download, FileType, AlertCircle, Pencil,
} from 'lucide-react';

const TYPE_ICON = {
  docx: FileText,
  pdf: FileText,
  pptx: Presentation,
  html: Code,
  html_report: FileText,
};

const TYPE_LABEL = {
  docx: 'Document',
  pdf: 'PDF',
  pptx: 'Presentation',
  html: 'HTML',
  html_report: 'Report',
};

function formatSize(bytes) {
  if (bytes == null) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getArtifactId(a) {
  return a.artifact_id || a.id;
}

/**
 * ArtifactCardList — Claude-style artifact cards rendered inside assistant
 * bubbles.
 *
 * Each artifact is a single compact card row: file-type icon tile, title,
 * "Document · DOCX · 37 KB" meta, and a primary "Preview" button that opens
 * the document in a right-anchored preview pane (see ArtifactPreviewPane /
 * Chat.jsx) so the user reads it alongside the conversation. A quiet
 * Download icon is shown when a file_url exists.
 *
 * STRICT FORMAT MATCH (prior requirement): when the user explicitly
 * requested a file format (e.g. "make a docx"), artifacts whose `type` does
 * not match display a clear amber mismatch warning above the card. The
 * Preview button remains available, but the warning makes the mismatch
 * explicit instead of silently rendering the wrong format.
 */
export default function ArtifactCardList({ artifacts, onPreview, onEdit, userRequestedFormat }) {
  if (!artifacts || artifacts.length === 0) return null;

  return (
    <div className="mt-3 flex flex-col gap-2">
      {artifacts.map((artifact, idx) => {
        const Icon = TYPE_ICON[artifact.type] || FileType;
        const label = TYPE_LABEL[artifact.type] || artifact.type;
        const size = formatSize(artifact.file_size);
        const isMismatch = !!userRequestedFormat && artifact.type !== userRequestedFormat;

        return (
          <div
            key={getArtifactId(artifact) || idx}
            className={`overflow-hidden rounded-xl border transition-colors ${
              isMismatch
                ? 'border-amber-300 bg-amber-50/60 dark:border-amber-700 dark:bg-amber-950/30'
                : 'border-border bg-secondary/40 hover:bg-secondary'
            }`}
          >
            {/* Strict format-mismatch warning */}
            {isMismatch && (
              <div className="flex items-start gap-1.5 border-b border-amber-300/60 bg-amber-100/80 px-3 py-1.5 text-[11px] text-amber-900 dark:border-amber-700/60 dark:bg-amber-900/40 dark:text-amber-200">
                <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  You asked for <strong>{userRequestedFormat.toUpperCase()}</strong> but this
                  artifact is <strong>{label}</strong>.
                </span>
              </div>
            )}

            <div className="flex items-center gap-3 px-3 py-2.5">
              {/* Type icon tile */}
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Icon className="h-4 w-4" />
              </div>

              {/* Title + meta */}
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground">
                  {artifact.title || artifact.file_name || 'Artifact'}
                </p>
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

              {/* Actions */}
              <div className="flex shrink-0 items-center gap-1.5">
                <button
                  onClick={() => onPreview?.(artifact)}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90"
                  title="Open in preview pane"
                >
                  <Eye className="h-3.5 w-3.5" />
                  Preview
                </button>
                {onEdit && (artifact.type === 'html' || artifact.type === 'html_report' || artifact.artifact_type === 'html') && (
                  <button
                    onClick={() => onEdit?.(artifact)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
                    title="Edit in interactive canvas"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                    Edit
                  </button>
                )}
                {artifact.file_url && (
                  <a
                    href={artifact.file_url}
                    download={artifact.file_name}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center justify-center rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
                    title="Download"
                  >
                    <Download className="h-4 w-4" />
                  </a>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
