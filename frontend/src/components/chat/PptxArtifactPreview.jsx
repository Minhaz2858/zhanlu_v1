/**
* PptxArtifactPreview - inline slide-deck viewer for the chat pane.
*
* Fetches /api/artifacts/{id}/preview?format=html (positioned slides at the
* deck's real aspect ratio), parses each <section class="zl-slide"
* data-slide="N"> into a slide, and shows one slide at a time on a dark
* stage that scales to fit ("contain": scale = min(cw/sw, ch/sh), centered,
* ~24px margin). The first slide's `width:Npx; height:Mpx` is read from the
* inline style so 4:3 / 16:10 / portrait decks all fit without clipping or
* postage-stamp content. The slide wrapper's translate(-50%,-50%)
* centering and the fit scale live in ONE inline `transform` — under
* Tailwind v3 the translate utilities are implemented via the `transform`
* property, so a separate inline `transform: scale(...)` would silently
* wipe the centering and park the slide's top-left corner at the stage
* center (the "slide jammed bottom-right, clipped" bug). Hosts that give
* no definite height (inline chat card) get a width-driven fallback with
* an explicit aspect-ratio stage height so the stage can't collapse to 0.
* Prev/next, arrow-key navigation, a thumbnail rail, and an "N / total"
* counter let the user move through the deck without leaving the chat.
*/
import { useEffect, useRef, useState, useCallback } from 'react';
import { Loader2, Presentation, AlertTriangle, Download, ChevronLeft, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';

const API_BASE = '/api';
const BASE_SLIDE_W = 960;
const BASE_SLIDE_H = 540;
const THUMB_CAP = 50;
// Uniform breathing room around the fitted slide inside the stage.
const STAGE_PAD = 24;
// Below this measured stage height the host clearly gave no definite
// height (a slide canvas is never legitimately shorter than this) — switch
// to the width-driven self-sizing fallback.
const MIN_REAL_STAGE_H = 120;

export default function PptxArtifactPreview({
  artifactId,
  outline = [],
  onAnchorJump,
  className,
  downloadUrl,
  title,
}) {
  const [slides, setSlides] = useState([]);
  const [slideDims, setSlideDims] = useState({ w: BASE_SLIDE_W, h: BASE_SLIDE_H });
  const [status, setStatus] = useState('loading');
  const [error, setError] = useState(null);
  const [current, setCurrent] = useState(0);
  const [scale, setScale] = useState(1);
  // Width-driven fallback: when the host gives no definite height, the
  // stage gets this explicit pixel height (derived from width + aspect).
  const [selfSizeH, setSelfSizeH] = useState(null);
  const selfSizeRef = useRef(false);
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
          // Image-fill PPTX decks (rendered via the new HTML design path)
          // don't expose an HTML preview — fall back to showing the first
          // stored thumbnail as a single static image.  The user can still
          // download the original PPTX and open it in PowerPoint for
          // editing.
          setStatus('image_fill_fallback');
          return;
        }
        // Read the deck's actual aspect ratio from the first slide's inline
        // style (backend always emits `width:Npx; height:Mpx` on the section).
        // Falling back to 16:9 keeps the legacy test fixtures and any
        // future hand-written HTML working.
        let actualW = BASE_SLIDE_W;
        let actualH = BASE_SLIDE_H;
        const firstStyle = sections[0].getAttribute('style') || '';
        const wMatch = firstStyle.match(/width:\s*([\d.]+)px/i);
        const hMatch = firstStyle.match(/height:\s*([\d.]+)px/i);
        if (wMatch && hMatch) {
          const w = parseFloat(wMatch[1]);
          const h = parseFloat(hMatch[1]);
          if (Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) {
            actualW = w;
            actualH = h;
          }
        }
        setSlideDims({ w: actualW, h: actualH });
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
      let h = el.clientHeight;
      if (!w) return;
      // Host gave no definite height (e.g. the inline chat card, whose
      // wrapper is only max-h-capped): the absolutely-positioned slide
      // contributes nothing to layout, so the stage would collapse to ~0px
      // and the deck would render at scale 1, jammed off-canvas. Fall back
      // to a width-driven fit and an explicit aspect-ratio stage height.
      // Sticky via selfSizeRef so the explicit height doesn't flip the
      // measurement back and oscillate, and so panel resizes keep
      // re-deriving the height from the (always definite) width.
      if (h < MIN_REAL_STAGE_H || selfSizeRef.current) {
        selfSizeRef.current = true;
        // ceil so the derived height never under-fits the width-driven
        // scale (a rounded-down height would shave the scale by a hair).
        h = Math.max(
          Math.ceil((w - STAGE_PAD * 2) * (slideDims.h / slideDims.w)) + STAGE_PAD * 2,
          MIN_REAL_STAGE_H,
        );
        setSelfSizeH((prev) => (prev === h ? prev : h));
      }
      // "Contain" fit: uniform scale, whole slide root, ~24px margin.
      const availW = Math.max(w - STAGE_PAD * 2, 40);
      const availH = Math.max(h - STAGE_PAD * 2, 40);
      // Use the deck's actual aspect ratio (parsed from the backend HTML),
      // not a hardcoded 16:9, so 4:3 / 16:10 / portrait decks fit correctly.
      const next = Math.min(availW / slideDims.w, availH / slideDims.h);
      setScale((prev) => (Math.abs(prev - next) < 0.0005 ? prev : next));
    };
    compute();
    const ro = new ResizeObserver(compute);
    ro.observe(el);
    return () => ro.disconnect();
  }, [status, slideDims.w, slideDims.h]);

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
        ) : status === 'image_fill_fallback' ? (
          <ImageFillFallback
            artifactId={artifactId}
            downloadUrl={downloadUrl}
            title={title}
          />
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
              style={selfSizeH ? { height: selfSizeH } : undefined}
              className={cn(
                // overflow-hidden: the 960px slide root must never leak
                // scrollable overflow into ancestors (no scrollbars in the
                // canvas) — only the scaled, centered result is visible.
                'relative flex min-h-0 w-full items-center justify-center overflow-hidden bg-[#0B1220] outline-none',
                // flex-1 (fill host) only when the host gives a definite
                // height; in self-size mode flex-basis:0% would override
                // the explicit pixel height and collapse the stage again.
                selfSizeH ? 'shrink-0' : 'flex-1'
              )}
            >
              <div
                style={{
                  width: slideDims.w,
                  height: slideDims.h,
                  // Centering + fit scale in ONE transform: under Tailwind
                  // v3 the -translate-x/y-1/2 classes use the `transform`
                  // property, so an inline `transform: scale(...)` without
                  // the translate would override (erase) the centering.
                  transform: `translate(-50%, -50%) scale(${scale})`,
                  transformOrigin: 'center center',
                }}
                className="pointer-events-none absolute left-1/2 top-1/2 overflow-hidden rounded-md bg-white shadow-2xl ring-1 ring-white/10 transition-opacity duration-150"
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

function ImageFillFallback({ artifactId, downloadUrl, title }) {
  // Renders a single thumbnail (slide 1) for image-fill PPTX decks.
  // The thumbnail endpoint reuses the existing _maybe_store_thumbnails
  // pipeline which converts the PPTX to PDF and then to per-page PNGs.
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-4 text-center">
      <img
        src={`${API_BASE}/artifacts/${artifactId}/preview?format=thumbnail&page=1`}
        alt={title || 'Slide 1 preview'}
        className="max-h-[60vh] max-w-full rounded border border-zinc-200 shadow-sm"
        loading="lazy"
      />
      <p className="text-xs text-muted-foreground">
        Image-fill deck — open in PowerPoint to edit. {downloadUrl && (
          <a
            href={downloadUrl}
            className="ml-2 inline-flex items-center gap-1 text-orange-600 hover:underline"
          >
            <Download className="h-3 w-3" /> Download
          </a>
        )}
      </p>
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
