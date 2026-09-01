/**
 * DocxBrowserRenderer — lazy-loaded browser-native DOCX renderer.
 *
 * Uses `docx-preview` (pure-JS OOXML renderer) to display .docx files with
 * true Word-like fidelity directly in the browser.  The ~180 KB library is
 * dynamically imported so it stays out of the main bundle and only loads
 * when a user actually opens a .docx preview.
 *
 * Props:
 *   src          – URL to fetch raw .docx bytes (e.g. /api/artifacts/{id}/download)
 *   outline      – server-extracted headings [{level, text, id}] for sidebar
 *   onAnchorJump – optional callback when user clicks a heading in the sidebar
 *   className    – outer wrapper class
 *   title        – document name (shown on error / download link)
 *   downloadUrl  – fallback download link when rendering fails
 *   onError      – optional callback invoked when renderAsync throws (parent
 *                  can use it to switch to a mammoth-HTML fallback)
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { Loader2, AlertTriangle, Download, FileText } from 'lucide-react';
import { cn } from '@/lib/utils';
import { authFetch } from '@/api/authFetch';
import DocxOutline from './DocxOutline';
import '@/docx-preview.css';

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------
const STATE = { LOADING: 'loading', RENDERED: 'rendered', ERROR: 'error' };

// ---------------------------------------------------------------------------
// Default options passed to docx-preview.renderAsync.  These mirror what a
// desktop Word session would show — page breaks, headers, footers, footnotes,
// but no tracked changes (clean reading view).
// ---------------------------------------------------------------------------
const DEFAULT_OPTIONS = {
  breakPages: true,
  renderHeaders: true,
  renderFooters: true,
  renderFootnotes: true,
  renderEndnotes: true,
  renderChanges: false,        // clean reading view — no revision marks
  renderComments: false,
  className: 'docx',
  inWrapper: true,
};

export default function DocxBrowserRenderer({
  src,
  outline = [],
  onAnchorJump,
  className,
  title,
  downloadUrl,
  onError,
}) {
  const [state, setState] = useState(STATE.LOADING);
  const [errorMsg, setErrorMsg] = useState(null);
  const containerRef = useRef(null);

  // ---- Render the document into the container -------------------------
  useEffect(() => {
    if (!src) {
      setState(STATE.ERROR);
      setErrorMsg('No document source');
      return;
    }

    let cancelled = false;
    setState(STATE.LOADING);
    setErrorMsg(null);

    (async () => {
      try {
        // 1. Dynamically import docx-preview (lazy chunk, tree-shaken).
        const { renderAsync } = await import('docx-preview');

        // 2. Fetch the raw .docx bytes.  Automation-file artifacts are served
        //    from `/api/automations/files/{id}/download`, a Bearer-authenticated
        //    route a plain `fetch` can't reach (it returns 401), which previously
        //    made the parent fall through to a mammoth fetch against the wrong
        //    endpoint → "Preview unavailable (HTTP 404)".  authFetch attaches
        //    the JWT (and refreshes on 401) so both the artifact and automation
        //    download routes resolve.
        const res = await authFetch(src);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();

        if (cancelled) return;

        // 3. Render into the container element.
        if (containerRef.current) {
          containerRef.current.innerHTML = '';
          await renderAsync(blob, containerRef.current, null, DEFAULT_OPTIONS);
        }

        if (!cancelled) setState(STATE.RENDERED);
      } catch (err) {
        if (cancelled) return;
        const msg = err.message || 'docx-preview failed';
        setErrorMsg(msg);
        setState(STATE.ERROR);
        onError?.(msg);
      }
    })();

    // Cleanup: clear DOM and cancel any in-flight work.
    return () => {
      cancelled = true;
      if (containerRef.current) containerRef.current.innerHTML = '';
    };
  }, [src]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Anchor-jump handler (for outline sidebar) ----------------------
  const handleJump = useCallback(
    (id) => {
      if (onAnchorJump) return onAnchorJump(id);
      const root = containerRef.current;
      if (!root) return;
      const target = root.querySelector(`#${CSS.escape(id)}`);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    },
    [onAnchorJump],
  );

  // ---- Render ---------------------------------------------------------
  return (
    <div className={cn('flex h-full min-h-0 w-full', className)}>
      {/* Outline sidebar */}
      {outline.length > 0 && (
        <DocxOutline
          outline={outline}
          onJump={handleJump}
          className="hidden w-44 shrink-0 md:block"
        />
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        {/* Loading state */}
        {state === STATE.LOADING && <LoadingBanner />}

        {/* Error state */}
        {state === STATE.ERROR && (
          <ErrorBanner
            message={errorMsg || 'Preview unavailable'}
            downloadUrl={downloadUrl}
            title={title}
          />
        )}

        {/* Rendered container (always mounted so unmount cleanup works) */}
        <div
          ref={containerRef}
          className={cn(
            'flex-1 overflow-y-auto',
            state === STATE.LOADING && 'hidden',
          )}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Internal sub-components
// ---------------------------------------------------------------------------

function LoadingBanner() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 py-8 text-xs text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      Rendering document…
    </div>
  );
}

function ErrorBanner({ message, downloadUrl, title }) {
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
        <p className="mt-1 max-w-sm text-xs text-muted-foreground">
          {message}
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
