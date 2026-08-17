/**
 * DocxArtifactPreview — inline DOCX reader for the chat card and side panel.
 *
 * Uses two-tier rendering:
 *   1. PRIMARY: DocxBrowserRenderer (lazy-loaded `docx-preview` lib) —
 *      renders the document with true Word-like fidelity (page layout,
 *      headings, tables, images, etc.).
 *   2. FALLBACK: mammoth HTML — server-side conversion via
 *      `/api/artifacts/{id}/preview?format=html`.  Activated automatically
 *      when docx-preview fails (blob fetch error, OOXML parse error, etc.).
 *
 * The optional `outline` (extracted server-side) drives the sidebar,
 * shared by both render paths.
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { Loader2, FileText, AlertTriangle, Download } from 'lucide-react';
import { cn } from '@/lib/utils';
import { authFetch } from '@/api/authFetch';
import DocxOutline from './DocxOutline';
import DocxBrowserRenderer from './DocxBrowserRenderer';

const API_BASE = '/api';

// Render modes
const MODE = {
  DOCX_PREVIEW: 'docx-preview',
  MAMMOTH: 'mammoth',
};

export default function DocxArtifactPreview({
  artifactId,
  outline = [],
  onAnchorJump,
  className,
  downloadUrl,
  title,
}) {
  const [mode, setMode] = useState(MODE.DOCX_PREVIEW);
  const [html, setHtml] = useState(null);
  const [mammothError, setMammothError] = useState(null);
  const bodyRef = useRef(null);

  // ---- Switch to mammoth when docx-preview fails ----------------------
  const handleDocxError = useCallback(() => {
    setMode(MODE.MAMMOTH);
  }, []);

  // ---- Derive the docx blob download URL ------------------------------
  // Prefer the explicit downloadUrl prop (e.g. automation_file artifacts
  // have file_url='/api/automations/files/{id}/download'). Fall back to
  // constructing from the artifact ID.
  const docxSrc = downloadUrl
    || (artifactId ? `${API_BASE}/artifacts/${artifactId}/download` : null);

  // Automation-run files are served from the automation router, not the
  // artifacts router, so the mammoth fallback must target the right table.
  const isAutomationFile = !!downloadUrl
    && downloadUrl.includes('/api/automations/files/');

  // ---- Fetch mammoth HTML when in fallback mode -----------------------
  useEffect(() => {
    if (mode !== MODE.MAMMOTH || !artifactId) return;
    let active = true;
    setMammothError(null);

    const url = isAutomationFile
      ? `${API_BASE}/automations/files/${artifactId}/preview?format=html`
      : `${API_BASE}/artifacts/${artifactId}/preview?format=html`;

    // authFetch (not plain fetch): the automation preview route is
    // Bearer-authenticated, and a raw fetch returns 401 → "HTTP 401"
    // instead of the correct mammoth HTML.
    authFetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((text) => { if (active) setHtml(text); })
      .catch((e) => { if (active) setMammothError(e.message || 'Failed to load'); });

    return () => { active = false; };
  }, [mode, artifactId, isAutomationFile]);

  // ---- Anchor-jump handler --------------------------------------------
  const handleJump = useCallback((id) => {
    if (onAnchorJump) return onAnchorJump(id);
    const root = bodyRef.current;
    if (!root) return;
    const target = root.querySelector(`#${CSS.escape(id)}`);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [onAnchorJump]);

  // ---- Render ---------------------------------------------------------
  if (!artifactId) {
    return (
      <div className={cn('flex h-full min-h-0 w-full', className)}>
        <div className="flex flex-1 items-center justify-center py-8 text-xs text-muted-foreground">
          No artifact ID provided.
        </div>
      </div>
    );
  }

  // Mode: docx-preview (primary)
  if (mode === MODE.DOCX_PREVIEW) {
    return (
      <DocxBrowserRenderer
        src={docxSrc}
        outline={outline}
        onAnchorJump={onAnchorJump}
        className={className}
        title={title}
        downloadUrl={downloadUrl}
        onError={handleDocxError}
      />
    );
  }

  // Mode: mammoth (fallback)
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
        {mammothError ? (
          <ErrorState message={mammothError} downloadUrl={downloadUrl} title={title} />
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
