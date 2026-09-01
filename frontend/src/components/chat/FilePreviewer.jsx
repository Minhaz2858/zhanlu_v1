import { useEffect, useState } from 'react';
import { Download, FileText, Eye, ExternalLink } from 'lucide-react';
import DocxBrowserRenderer from './DocxBrowserRenderer';
import { authFetch } from '@/api/authFetch';

const IMG = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
const OFFICE = ['doc', 'docx', 'ppt', 'pptx', 'pps', 'ppsx', 'xls', 'xlsx', 'odt', 'ods', 'odp'];
const AUDIO = ['mp3', 'm4a', 'wav', 'ogg', 'flac', 'aac'];
const VIDEO = ['mp4', 'mov', 'webm', 'ogv', 'mkv'];
const TEXT = ['txt', 'md', 'csv', 'json', 'log', 'tsv', 'yaml', 'yml', 'xml', 'ini', 'conf'];

export function extOf(name) {
  // First try to extract from the name itself (e.g. "report.docx")
  let m = /\.([a-z0-9]+)$/i.exec(name || '');
  if (m) return m[1].toLowerCase();
  // Fall back to extracting from a URL (e.g. "/api/uploads/report.docx")
  // so Report entities whose `name` is just a human title like "Annual
  // Summary" still get the correct extension.
  m = /\.([a-z0-9]+)(?:\?|#|$)/i.exec(name || '');
  return m ? m[1].toLowerCase() : '';
}

/**
 * Returns true for Office files (docx, pptx, xlsx, …) that browsers
 * cannot render natively and that the backend can convert to HTML via
 * the ``/api/files/preview`` endpoint.
 */
function canLocalPreview(ext) {
  return ['docx', 'pptx', 'xlsx', 'md', 'markdown'].includes(ext);
}

/**
 * Resolve a file URL to the path component only (drop host). Used to
 * decide whether a URL points at our own /api/uploads/ directory.
 */
function pathOf(url) {
  if (!url) return '';
  if (/^https?:\/\//i.test(url)) {
    try { return new URL(url).pathname; } catch { return url; }
  }
  return url;
}

function isLocalUpload(url) {
  return pathOf(url).startsWith('/api/uploads/');
}

/**
 * Convert a local /api/uploads/<file> URL to the corresponding
 * /api/files/preview URL.  Returns null if the URL is not local.
 */
function toPreviewUrl(url) {
  if (!url) return null;
  const p = pathOf(url);
  if (!p.startsWith('/api/uploads/')) return null;
  return `/api/files/preview?url=${encodeURIComponent(p)}`;
}

/**
 * Extract an AutomationFile id from an automation-file URL
 * (e.g. "/api/automations/files/<id>/download" or ".../preview").
 * Returns null when the URL is not an automation-file URL.
 */
const AUTOMATION_FILE_RE = /\/api\/automations\/files\/([^/?#]+)/i;
function getAutomationFileId(url) {
  if (typeof url !== 'string') return null;
  // Only same-origin relative paths are automation-file URLs; an absolute
  // external URL must never be routed to the signed-token mint.
  if (/^https?:\/\//i.test(url)) return null;
  const m = AUTOMATION_FILE_RE.exec(url);
  return m ? m[1] : null;
}

/**
 * Mint a short-lived signed preview token so that iframe / img / audio /
 * video tags — none of which can send the Authorization header — can
 * render auth-protected AutomationFile URLs
 * (e.g. "/api/automations/files/<id>/download") inline.
 *
 * The mint endpoint at POST /api/automations/files/<id>/preview-token
 * returns { url: "/api/automations/files/<id>/preview?token=..." }
 * which the backend /preview route serves with the correct Content-Type
 * for inline display (image/png, application/pdf, text/html, …). Mirrors
 * the same flow used by InlineArtifactPreview / ArtifactPreviewPane.
 *
 * Returns:
 *   - tokenUrl: the signed URL once minted (null while minting or on error)
 *   - failed:   true when minting failed or returned no url
 *   - isAutomation: true when the input URL was recognised as an
 *     automation-file URL (caller should fall back to tokenUrl-or-raw)
 */
function useAutomationFileToken(url) {
  const fileId = getAutomationFileId(url);
  const isAutomation = !!fileId;
  const [tokenUrl, setTokenUrl] = useState(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!fileId) {
      setTokenUrl(null);
      setFailed(false);
      return undefined;
    }
    let cancelled = false;
    setTokenUrl(null);
    setFailed(false);
    authFetch(`/api/automations/files/${fileId}/preview-token`, { method: 'POST' })
      .then((res) => {
        if (!res || !res.ok) throw new Error(`preview-token failed (${res && res.status})`);
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        if (data && data.url) setTokenUrl(data.url);
        else setFailed(true);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => { cancelled = true; };
  }, [fileId]);
  return { tokenUrl, failed, isAutomation };
}

function PreviewLoading() {
  return (
    <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
      Loading preview…
    </div>
  );
}

function PreviewFailed() {
  return (
    <div className="flex flex-col items-center gap-3 py-12 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
        <FileText className="h-6 w-6 text-muted-foreground" />
      </div>
      <p className="text-sm text-muted-foreground">Could not load preview. Download the file to view it.</p>
    </div>
  );
}

export default function FilePreviewer({ url, name, kind, fileType, className }) {
  // Derive extension from (in order):
  //   1. explicit fileType prop (e.g. "pdf" from FileCard metadata)
  //   2. file name (e.g. "report.docx")
  //   3. file URL  (e.g. "/api/uploads/test.pdf") — handles Report
  //      entities whose `name` is a human title like "Annual Summary"
  const ext =
    (fileType && fileType.toLowerCase()) ||
    extOf(name) ||
    extOf(url);

  // AutomationFile URLs are auth-protected and can't be rendered by
  // <iframe>/<img>/<audio>/<video> tags (which can't send the
  // Authorization header). Mint a short-lived signed token once and
  // route every media tag through the tokenized URL. Loading/failed
  // state is handled up front for the simple branches; the OFFICE /
  // Markdown branch falls through to the existing local-upload logic.
  // The hook is called unconditionally (even when url is empty) so the
  // hook order is stable across renders — the early-return below
  // ignores its result.
  const { tokenUrl, failed: tokenFailed, isAutomation } = useAutomationFileToken(url);
  const effectiveUrl = isAutomation ? (tokenUrl || url) : url;

  // Empty URL: show a helpful "no preview available" empty state with
  // the file metadata + download button (was previously a blank modal).
  if (!url) {
    return (
      <div className="flex h-full min-h-[40vh] flex-col items-center justify-center gap-3 py-12 text-center" data-testid="file-preview-empty">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-secondary">
          <FileText className="h-7 w-7 text-muted-foreground" />
        </div>
        <p className="text-sm font-medium text-foreground">{name || 'Untitled file'}</p>
        {ext && <p className="text-xs uppercase tracking-wide text-muted-foreground">.{ext}</p>}
        <p className="mt-1 max-w-sm text-xs text-muted-foreground">
          No preview is available for this file yet — it may still be generating, or the source
          didn't return a downloadable URL. Open the file in chat once it's ready, or ask the
          agent to regenerate it.
        </p>
        <a
          href={url || '#'}
          target="_blank"
          rel="noreferrer"
          download={name}
          aria-disabled={!url}
          className={
            'mt-2 inline-flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground transition-colors ' +
            (url ? 'hover:bg-secondary' : 'pointer-events-none opacity-50')
          }
        >
          <Download className="h-4 w-4 text-primary" /> Download
        </a>
      </div>
    );
  }

  const iframeCls = `block w-full ${className || 'h-[70vh] rounded-lg border border-border bg-card'}`;

  if (isAutomation) {
    if (tokenFailed) return <PreviewFailed />;
    if (!tokenUrl) return <PreviewLoading />;
    // Office / Markdown automation files: render the tokenized URL directly.
    // 2026-08-31 (bug): the previous code fell through to the MS Office
    // Online viewer (view.officeapps.live.com) with the RAW download URL —
    // the token was minted but ignored, so the preview was broken (MS
    // servers can't authenticate) AND the unauthenticated file URL leaked
    // to a third-party service. The viewer cannot reach the tokenized local
    // URL, so it stays out of the automation path entirely.
    if (OFFICE.includes(ext) || ['md', 'markdown'].includes(ext)) {
      return <iframe src={tokenUrl} title={name || 'preview'} className={iframeCls} />;
    }
  }

  if (IMG.includes(ext)) {
    return <img src={effectiveUrl} alt={name || 'file'} className={className || 'max-h-[70vh] w-full rounded-lg border border-border bg-secondary/30 object-contain'} />;
  }

  if (ext === 'pdf') {
    return <iframe src={effectiveUrl} title={name || 'preview'} className={iframeCls} />;
  }

  // HTML, dashboards, web apps → render in sandboxed iframe
  if (['html', 'htm'].includes(ext) || ['dashboard', 'html_file'].includes(kind)) {
    return <iframe src={effectiveUrl} title={name || 'preview'} className={iframeCls} sandbox="allow-same-origin allow-scripts allow-popups allow-forms" />;
  }

  // Audio files → native <audio> element (browser-decodable formats only).
  if (AUDIO.includes(ext)) {
    return (
      <div className="flex items-center justify-center py-12">
        <audio controls src={effectiveUrl} className="w-full max-w-md">
          Your browser does not support audio playback.
        </audio>
      </div>
    );
  }

  // Video files → native <video> element.
  if (VIDEO.includes(ext)) {
    return (
      <div className="flex items-center justify-center py-4">
        <video controls src={effectiveUrl} className="max-h-[70vh] w-full rounded-lg border border-border bg-black">
          Your browser does not support video playback.
        </video>
      </div>
    );
  }

  // Plain text / data files → iframe (browser renders natively). Falls
  // back to the download link below if the iframe is blocked by CSP.
  if (TEXT.includes(ext)) {
    return <iframe src={effectiveUrl} title={name || 'preview'} className={iframeCls} />;
  }

  // Office documents (docx, pptx, xlsx) and Markdown.
  //
  // NOTE: for automation files (isAutomation=true) we've already routed
  // through effectiveUrl above and the iframe receives the tokenized URL
  // directly, so this branch only handles non-automation OFFICE /
  // Markdown files — typically local uploads under /api/uploads/ or
  // /api/artifacts/. The MS Office Online viewer (view.officeapps.live.com)
  // cannot reach the tokenized local URL, so it MUST stay out of the
  // automation path; that is guaranteed by the top-level isAutomation
  // short-circuit above.
  //
  // DOCX → use DocxBrowserRenderer (lazy-loaded docx-preview — true
  //   Word-like fidelity with page layout, headings, tables, images).
  //   Falls back to mammoth HTML if docx-preview fails.
  // Other Office / Markdown → use the /api/files/preview endpoint
  //   (mammoth / python-pptx / openpyxl / markdown lib).
  // External URLs → legacy view.officeapps.live.com viewer.
  if (OFFICE.includes(ext) || ['md', 'markdown'].includes(ext)) {
    // .docx with local upload → browser-native renderer
    if (ext === 'docx' && isLocalUpload(url)) {
      return (
        <DocxBrowserRenderer
          src={url}
          title={name}
          downloadUrl={url}
          className="h-full"
        />
      );
    }
    if (isLocalUpload(url) && canLocalPreview(ext)) {
      const previewUrl = toPreviewUrl(url);
      return (
        <div className="flex h-full flex-col gap-2">
          <iframe
            src={previewUrl}
            title={name || 'preview'}
            className={iframeCls}
            sandbox="allow-same-origin allow-scripts allow-popups allow-forms"
          />
          <div className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
            <Eye className="h-3.5 w-3.5" />
            <span>In-browser preview (mammoth / openpyxl / python-pptx)</span>
          </div>
        </div>
      );
    }
    if (typeof url === 'string' && url.includes('/api/artifacts/')) {
      return <iframe src={url} title={name || 'preview'} className={iframeCls} />;
    }
    const viewer = `https://view.officeapps.live.com/op/embed.aspx?src=${encodeURIComponent(url)}&wdStartOn=1`;
    return <iframe src={viewer} title={name || 'preview'} className={iframeCls} allowFullScreen />;
  }

  // Fallback: download link
  return (
    <div className="flex flex-col items-center gap-3 py-12">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
        <FileText className="h-6 w-6 text-muted-foreground" />
      </div>
      <p className="max-w-xs truncate text-sm text-foreground">{name || 'file'}</p>
      {ext && <p className="text-xs uppercase text-muted-foreground">{ext}</p>}
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        download={name}
        className="inline-flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground transition-colors hover:bg-secondary"
      >
        <Download className="h-4 w-4 text-primary" /> Download
      </a>
    </div>
  );
}
