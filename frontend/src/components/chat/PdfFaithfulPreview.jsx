/**
 * PdfFaithfulPreview — renders the *actual* downloaded file as a PDF.
 *
 * Default endpoint: `/api/artifacts/{id}/preview?format=pdf` (the backend
 * converts the real stored docx/pptx/xlsx bytes via LibreOffice for
 * artifacts registered in the Artifact table).
 *
 * Pass `previewUrl` to override the endpoint — used by automation-file
 * artifacts (registered in the AutomationFile table, not Artifact) which
 * are served by `/api/automations/files/{id}/preview`. Without this
 * override the component would 404 on the broken artifact endpoint and
 * the user would see "Preview unavailable (HTTP 404)" (the bug the user
 * reported with "Daily Sales Data Sync.docx").
 *
 * The PDF is fetched as a Blob and displayed via an object URL so the
 * browser's native PDF viewer handles rendering (zoom, scroll, print)
 * with zero client-side dependencies.  If the fetch fails (e.g.
 * LibreOffice unavailable on the server, or the original blob is
 * missing), the `fallback` React node is rendered instead — typically
 * the existing mammoth/pptx2html inline reader.
 */
import { useEffect, useState } from 'react';
import { Loader2, AlertTriangle, Download } from 'lucide-react';
import { authFetch } from '@/api/authFetch';

export default function PdfFaithfulPreview({
  artifactId,
  previewUrl,
  title,
  downloadUrl,
  fallback,
  className = '',
}) {
  const [pdfUrl, setPdfUrl] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!artifactId && !previewUrl) return;
    let cancelled = false;
    let objectUrl = null;
    setLoading(true);
    setError(false);
    setPdfUrl(null);

    const url =
      previewUrl || `/api/artifacts/${artifactId}/preview?format=pdf`;
    // authFetch: automation-file previews live behind a Bearer-authenticated
    // route; a plain fetch returns 401 → the component would silently fall
    // back and show "PDF preview unavailable".
    authFetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const ct = r.headers.get('content-type') || '';
        // The backend returns application/pdf on success.  If we get
        // text/html it means the conversion fell through and the
        // server returned an error-page fallback — treat as failure
        // so we render the inline HTML reader instead.
        if (!ct.includes('pdf')) throw new Error('Not a PDF response');
        return r.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setPdfUrl(objectUrl);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setError(true);
        setLoading(false);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [artifactId]);

  if (loading) {
    return (
      <div className={`flex h-full items-center justify-center ${className}`}>
        <div className="flex flex-col items-center gap-3 text-sm text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin text-blue-500" />
          <span>Rendering document…</span>
          <span className="text-[11px] text-muted-foreground/60">
            Converting to PDF for faithful preview
          </span>
        </div>
      </div>
    );
  }

  if (error || !pdfUrl) {
    // Graceful fallback: render the parent-provided alternative
    // (typically DocxArtifactPreview / PptxArtifactPreview).
    if (fallback) return fallback;
    return (
      <div className={`flex h-full flex-col items-center justify-center gap-4 px-6 text-center ${className}`}>
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-amber-500/10">
          <AlertTriangle className="h-6 w-6 text-amber-500" />
        </div>
        <div>
          <p className="text-sm font-medium text-foreground">{title || 'Document'}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            PDF preview unavailable. Try downloading the file instead.
          </p>
        </div>
        {downloadUrl && (
          <a
            href={downloadUrl}
            download
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

  return (
    <iframe
      src={pdfUrl}
      title={title || 'Document preview'}
      className={`h-full w-full border-0 ${className}`}
    />
  );
}
