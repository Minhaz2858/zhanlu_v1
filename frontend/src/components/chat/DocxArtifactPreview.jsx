/**
 * DocxArtifactPreview — inline DOCX reader for the chat card and side panel.
 *
 * Fetches `/api/artifacts/{id}/preview?format=html` (sanitized server-side
 * by `mammoth`) and renders it inside a scrollable, sandboxed container.
 * The optional `outline` (extracted server-side) drives the sidebar.
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { Loader2, FileText, AlertTriangle, Download } from 'lucide-react';
import { cn } from '@/lib/utils';
import DocxOutline from './DocxOutline';

const API_BASE = '/api';

export default function DocxArtifactPreview({
  artifactId,
  outline = [],
  onAnchorJump,
  className,
  downloadUrl,
  title,
}) {
  const [html, setHtml] = useState(null);
  const [error, setError] = useState(null);
  const bodyRef = useRef(null);

  useEffect(() => {
    if (!artifactId) return;
    let active = true;
    setHtml(null);
    setError(null);
    fetch(`${API_BASE}/artifacts/${artifactId}/preview?format=html`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((text) => { if (active) setHtml(text); })
      .catch((e) => { if (active) setError(e.message || 'Failed to load'); });
    return () => { active = false; };
  }, [artifactId]);

  const handleJump = useCallback((id) => {
    if (onAnchorJump) return onAnchorJump(id);
    const root = bodyRef.current;
    if (!root) return;
    const target = root.querySelector(`#${CSS.escape(id)}`);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [onAnchorJump]);

  return (
    <div className={cn('flex h-full min-h-0 w-full', className)}>
      {outline.length > 0 && (
        <DocxOutline
          outline={outline}
          onJump={handleJump}
          className="hidden w-44 shrink-0 md:block"
        />
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        {error ? (
          <ErrorState message={error} downloadUrl={downloadUrl} title={title} />
        ) : html == null ? (
          <LoadingState />
        ) : (
          <article
            ref={bodyRef}
            data-docx-body
            className={cn(
              'flex-1 overflow-y-auto px-6 py-5',
              'prose prose-sm dark:prose-invert max-w-none',
              'leading-relaxed',
            )}
            // eslint-disable-next-line react/no-danger
            dangerouslySetInnerHTML={{ __html: html }}
          />
        )}
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      Loading document…
    </div>
  );
}

function ErrorState({ message, downloadUrl, title }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-8 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-amber-500/10">
        <AlertTriangle className="h-5 w-5 text-amber-500" />
      </div>
      <div>
        <p className="flex items-center gap-1 text-sm font-medium text-foreground">
          <FileText className="h-4 w-4" />
          {title || 'Document'}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Preview unavailable ({message}).
        </p>
      </div>
      {downloadUrl && (
        <a
          href={downloadUrl}
          download
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
        >
          <Download className="h-3.5 w-3.5" />
          Download .docx
        </a>
      )}
    </div>
  );
}
