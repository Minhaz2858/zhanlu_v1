/**
 * HtmlReportArtifactPreview — inline preview for html_report artifacts.
 *
 * Fetches the artifact's HTML via `/api/artifacts/{id}/preview` and renders
 * it in a sandboxed iframe.  An optional outline sidebar provides section
 * navigation extracted from the HTML headings (h1-h6).
 *
 * Format chips at the top let users download DOCX, PDF, or the original HTML.
 */
import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { Loader2, FileCode, AlertTriangle, Download, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

const API_BASE = '/api';

/**
 * Extract a flat heading outline from an HTML string.
 * Returns [{ level, text, id }] for h1-h6 elements.
 */
function extractOutline(html) {
  if (!html) return [];
  const div = document.createElement('div');
  div.innerHTML = html;
  const headings = div.querySelectorAll('h1, h2, h3, h4, h5, h6');
  const used = new Set();
  const outline = [];
  headings.forEach((h) => {
    const level = parseInt(h.tagName[1], 10);
    const text = (h.textContent || '').trim();
    if (!text) return;
    // Generate a stable id
    let slug = text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || 'section';
    let anchor = slug;
    let i = 2;
    while (used.has(anchor)) {
      anchor = `${slug}-${i}`;
      i++;
    }
    used.add(anchor);
    // Add id to the heading element if not present
    if (!h.id) h.id = anchor;
    outline.push({ level, text, id: anchor });
  });
  // Return both the outline and the modified HTML (with injected ids)
  return { outline, html: div.innerHTML };
}

export default function HtmlReportArtifactPreview({
  artifactId,
  className,
  downloadUrl,
  title,
}) {
  const [state, setState] = useState({ html: null, error: null, outline: [] });
  const [availableFormats, setAvailableFormats] = useState({});
  const [activeSection, setActiveSection] = useState(null);
  const bodyRef = useRef(null);

  // Fetch HTML content
  useEffect(() => {
    if (!artifactId) return;
    let active = true;
    setState({ html: null, error: null, outline: [] });

    fetch(`${API_BASE}/artifacts/${artifactId}/preview`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((text) => {
        if (!active) return;
        const { outline, html } = extractOutline(text);
        setState({ html, error: null, outline });
      })
      .catch((e) => {
        if (active) setState({ html: null, error: e.message || 'Failed to load', outline: [] });
      });

    return () => { active = false; };
  }, [artifactId]);

  // Fetch available export formats
  useEffect(() => {
    if (!artifactId) return;
    let active = true;
    fetch(`${API_BASE}/artifacts/${artifactId}/formats`)
      .then((r) => r.json())
      .then((data) => { if (active) setAvailableFormats(data.formats || {}); })
      .catch(() => {});
    return () => { active = false; };
  }, [artifactId]);

  // Scroll to section
  const handleJump = useCallback((id) => {
    setActiveSection(id);
    const root = bodyRef.current;
    if (!root) return;
    const iframe = root.querySelector('iframe');
    if (!iframe || !iframe.contentDocument) return;
    const target = iframe.contentDocument.getElementById(id);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, []);

  // Format chips
  const formats = useMemo(() => {
    const chips = [];
    const has = (fmt) => availableFormats[fmt];
    chips.push({
      key: 'html',
      label: 'HTML',
      ext: '.html',
      mime: 'text/html',
      available: true, // original is always available
      url: `${API_BASE}/artifacts/${artifactId}/download?format=html`,
    });
    chips.push({
      key: 'docx',
      label: 'DOCX',
      ext: '.docx',
      mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      available: !!has('docx'),
      url: `${API_BASE}/artifacts/${artifactId}/download?format=docx`,
    });
    chips.push({
      key: 'pdf',
      label: 'PDF',
      ext: '.pdf',
      mime: 'application/pdf',
      available: !!has('pdf'),
      url: `${API_BASE}/artifacts/${artifactId}/download?format=pdf`,
    });
    return chips;
  }, [availableFormats, artifactId]);

  const { html, error, outline } = state;

  return (
    <div className={cn('flex h-full min-h-0 w-full flex-col', className)}>
      {/* Format chips bar */}
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <span className="text-[11px] font-medium text-muted-foreground mr-1">
          Download:
        </span>
        {formats.map((fmt) => (
          <a
            key={fmt.key}
            href={fmt.available ? fmt.url : undefined}
            download
            className={cn(
              'inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors',
              fmt.available
                ? 'border border-border bg-background text-foreground hover:bg-primary hover:text-primary-foreground hover:border-primary'
                : 'border border-border bg-muted/30 text-muted-foreground cursor-not-allowed opacity-50',
            )}
            onClick={(e) => { if (!fmt.available) e.preventDefault(); }}
            title={fmt.available ? `Download as ${fmt.label}` : `${fmt.label} not yet rendered`}
          >
            <Download className="h-3 w-3" />
            {fmt.label}
          </a>
        ))}
      </div>

      {/* Body: outline sidebar + preview */}
      <div className="flex min-h-0 flex-1">
        {/* Outline sidebar */}
        {outline.length > 0 && (
          <nav className="hidden w-48 shrink-0 overflow-y-auto border-r border-border bg-muted/20 md:block">
            <div className="px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
              Contents
            </div>
            <ul className="space-y-0.5 px-2 pb-3">
              {outline.map((item, idx) => (
                <li key={item.id || idx}>
                  <button
                    onClick={() => handleJump(item.id)}
                    className={cn(
                      'flex w-full items-center gap-1 rounded px-2 py-1 text-left text-[11px] transition-colors hover:bg-secondary',
                      activeSection === item.id
                        ? 'bg-primary/10 text-primary font-medium'
                        : 'text-muted-foreground',
                    )}
                    style={{ paddingLeft: `${8 + (item.level - 1) * 12}px` }}
                  >
                    {item.level <= 2 && (
                      <ChevronRight className={cn(
                        'h-3 w-3 shrink-0 transition-transform',
                        activeSection === item.id && 'rotate-90',
                      )} />
                    )}
                    <span className="truncate">{item.text}</span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>
        )}

        {/* Main content area */}
        <div className="flex min-h-0 flex-1 flex-col">
          {error ? (
            <ErrorState message={error} downloadUrl={downloadUrl} title={title} />
          ) : html == null ? (
            <LoadingState />
          ) : (
            <iframe
              srcDoc={html}
              title={title || 'Report'}
              className="min-h-0 flex-1 border-0 bg-white"
              sandbox="allow-same-origin"
            />
          )}
        </div>
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      Loading report…
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
          <FileCode className="h-4 w-4" />
          {title || 'Report'}
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
          Download
        </a>
      )}
    </div>
  );
}
