/**
 * PptxArtifactPreview - inline slide-deck viewer for the chat pane.
 *
 * Fetches /api/artifacts/{id}/preview?format=html (positioned 16:9 slides),
 * parses each <section class="zl-slide" data-slide="N"> into a slide, and
 * shows one slide at a time on a dark stage that scales to fit. Prev/next,
 * arrow-key navigation, a thumbnail rail, and an "N / total" counter let the
 * user move through the deck without leaving the chat. Props contract is
 * unchanged from the previous stacked-scroll reader.
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { Loader2, Presentation, AlertTriangle, Download, ChevronLeft, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

const API_BASE = '/api';
const BASE_SLIDE_W = 960;
const BASE_SLIDE_H = 540;
const THUMB_CAP = 50;

export default function PptxArtifactPreview({
  artifactId,
  outline = [],
  onAnchorJump,
  className,
  downloadUrl,
  title,
}) {
  const [slides, setSlides] = useState([]);
  const [status, setStatus] = useState('loading');
  const [error, setError] = useState(null);
  const [current, setCurrent] = useState(0);
  const [scale, setScale] = useState(1);
  const stageRef = useRef(null);

  useEffect(() => {
    if (!artifactId) return;
    let active = true;
    setStatus('loading');
    setError(null);
    setSlides([]);
    setCurrent(0);
    fetch(`${API_BASE}/artifacts/${artifactId}/preview?format=html`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((text) => {
        if (!active) return;
        const doc = new DOMParser().parseFromString(text, 'text/html');
        const sections = Array.from(doc.querySelectorAll('[data-slide]'));
        if (!sections.length) {
          setStatus('error');
          setError('No slides found');
          return;
        }
        setSlides(sections.map((s) => ({
          id: s.getAttribute('data-slide') || String(s),
          html: s.outerHTML,
        })));
        setStatus('ready');
      })
      .catch((e) => {
        if (active) {
          setStatus('error');
          setError(e.message || 'Failed to load');
        }
      });
    return () => { active = false; };
  }, [artifactId]);

  useEffect(() => {
    if (status !== 'ready') return undefined;
    const el = stageRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const compute = () => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      if (!w || !h) return;
      setScale(Math.min(w / BASE_SLIDE_W, h / BASE_SLIDE_H));
    };
    compute();
    const ro = new ResizeObserver(compute);
    ro.observe(el);
    return () => ro.disconnect();
  }, [status]);

  const total = slides.length;
  const go = useCallback((idx) => {
    setCurrent((prev) => Math.max(0, Math.min(idx, total - 1)));
  }, [total]);

  const onKey = useCallback((e) => {
    if (status !== 'ready' || total === 0) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); go(current + 1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(current - 1); }
    else if (e.key === 'Home') { e.preventDefault(); go(0); }
    else if (e.key === 'End') { e.preventDefault(); go(total - 1); }
  }, [current, total, status, go]);

  const labelFor = useCallback((i) => {
    if (outline && outline[i] && outline[i].text) return outline[i].text;
    return `Slide ${i + 1}`;
  }, [outline]);

  const handleJump = useCallback((id) => {
    if (onAnchorJump) return onAnchorJump(id);
    const m = String(id).match(/(\d+)/);
    if (m) go(parseInt(m[1], 10) - 1);
  }, [onAnchorJump, go]);
  void handleJump;

  const showThumbs = total > 0;
  const thumbCount = Math.min(total, THUMB_CAP);

  return (
    <div className={cn('flex h-full min-h-0 w-full', className)}>
      {showThumbs && (
        <nav
          aria-label="Slide navigation"
          className="hidden w-36 shrink-0 flex-col gap-1 overflow-y-auto border-r border-border bg-muted/30 p-3 text-xs md:flex"
        >
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            Slides
          </div>
          {slides.slice(0, thumbCount).map((s, i) => (
            <button
              key={s.id || i}
              type="button"
              aria-label={`Go to slide ${i + 1}: ${labelFor(i)}`}
              aria-current={i === current ? 'true' : undefined}
              onClick={() => go(i)}
              title={labelFor(i)}
              className={cn(
                'rounded px-2 py-1 text-left text-foreground/80 transition-colors hover:bg-accent hover:text-foreground',
                i === current && 'bg-primary/10 text-primary ring-1 ring-primary/40'
              )}
            >
              <span className="mr-1.5 text-muted-foreground">{i + 1}.</span>
              <span className="line-clamp-2">{labelFor(i)}</span>
            </button>
          ))}
          {total > THUMB_CAP && (
            <div className="px-2 py-1 text-[10px] text-muted-foreground">
              ...{total - THUMB_CAP} more
            </div>
          )}
        </nav>
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        {status === 'error' ? (
          <ErrorState message={error} downloadUrl={downloadUrl} title={title} />
        ) : status === 'loading' ? (
          <LoadingState />
        ) : total === 0 ? (
          <ErrorState message="No slides to show" downloadUrl={downloadUrl} title={title} />
        ) : (
          <>
            <div
              ref={stageRef}
              tabIndex={0}
              role="group"
              aria-label="Presentation stage"
              onKeyDown={onKey}
              className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-[#0B1220] outline-none"
            >
              <div
                style={{
                  width: BASE_SLIDE_W,
                  height: BASE_SLIDE_H,
                  transform: `scale(${scale})`,
                  transformOrigin: 'center center',
                }}
                className="pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-md bg-white shadow-2xl ring-1 ring-white/10 transition-opacity duration-150"
              >
                <div
                  // eslint-disable-next-line react/no-danger
                  dangerouslySetInnerHTML={{ __html: slides[current]?.html || '' }}
                />
              </div>
            </div>

            <div className="flex shrink-0 items-center justify-center gap-2 border-t border-border bg-secondary/40 px-3 py-2">
              <button
                type="button"
                aria-label="Previous slide"
                onClick={() => go(current - 1)}
                disabled={current === 0}
                className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-background text-foreground transition-colors hover:bg-secondary disabled:cursor-not-allowed disabled:opacity-40"
              >
                <ChevronLeft className="h-4 w-4" />
              </button>
              <span
                className="min-w-[64px] text-center text-xs font-medium tabular-nums text-foreground"
                aria-live="polite"
              >
                {current + 1} / {total}
              </span>
              <button
                type="button"
                aria-label="Next slide"
                onClick={() => go(current + 1)}
                disabled={current === total - 1}
                className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-background text-foreground transition-colors hover:bg-secondary disabled:cursor-not-allowed disabled:opacity-40"
              >
                <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      Loading presentation...
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
        <p className="flex items-center justify-center gap-1 text-sm font-medium text-foreground">
          <Presentation className="h-4 w-4" />
          {title || 'Presentation'}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Preview unavailable{message ? ` (${message})` : ''}.
        </p>
      </div>
      {downloadUrl && (
        <a
          href={downloadUrl}
          download
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
        >
          <Download className="h-3.5 w-3.5" />
          Download .pptx
        </a>
      )}
    </div>
  );
}
