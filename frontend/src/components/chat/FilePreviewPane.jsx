import { ExternalLink, X, FileText } from 'lucide-react';
import { useLanguage } from '@/lib/LanguageProvider';
import FilePreviewer from './FilePreviewer';

/**
 * Right-side preview pane for a user-attached file (Kimi/ChatGPT-style).
 *
 * Clicking an attachment card in a chat message used to open the file in a
 * new browser tab; this pane renders the SAME FilePreviewer the draft-chip
 * modal uses, but anchored in a resizable panel on the right of the chat so
 * the conversation stays in view while the user inspects the file.
 *
 * Props:
 *   file: { name, file_url, ext, kind } — the persisted attachment descriptor
 *   onClose: () => void
 */
export default function FilePreviewPane({ file, onClose }) {
  const { t, lang } = useLanguage();
  if (!file) return null;
  const name = file.name || file.file_url || (t?.chat?.coreTitle || 'File');
  return (
    <div className="flex h-full flex-col bg-background" data-testid="file-preview-pane">
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="truncate text-sm font-medium text-foreground" title={name}>{name}</span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {file.file_url && (
            <a
              href={file.file_url}
              target="_blank"
              rel="noreferrer"
              title={lang === 'en' ? 'Open in new tab' : '在新标签页打开'}
              className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              data-testid="file-preview-open-tab"
            >
              <ExternalLink className="h-4 w-4" />
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            title={lang === 'en' ? 'Close preview' : '关闭预览'}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            data-testid="file-preview-close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-auto">
        {/* Pass className so FilePreviewer's iframe/img fill the pane
            instead of its modal-era fixed h-[70vh] sizing — without this
            the preview floats as a small framed box with a dead gap below
            (the "abnormal layout" bug). object-contain keeps images from
            distorting when stretched to the pane box. */}
        <FilePreviewer url={file.file_url} name={name} kind={file.kind} fileType={file.ext} className="h-full w-full object-contain" />
      </div>
    </div>
  );
}
