import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import PptxArtifactPreview from './PptxArtifactPreview';

const HTML = `
<!DOCTYPE html><html><body>
<section class='zl-slide' data-slide='1'><h1>Cover</h1></section>
<section class='zl-slide' data-slide='2'><h1>Method</h1><p>Body.</p></section>
<section class='zl-slide' data-slide='3'><h1>Summary</h1></section>
</body></html>
`;

const OUTLINE = [
  { level: 1, text: 'Cover', id: 'slide-1' },
  { level: 1, text: 'Method', id: 'slide-2' },
  { level: 1, text: 'Summary', id: 'slide-3' },
];

// Captured ResizeObserver instances so tests can drive container sizes.
let roInstances = [];
const realRO = globalThis.ResizeObserver;

class MockResizeObserver {
  constructor(cb) {
    this.cb = cb;
    this.el = null;
    roInstances.push(this);
  }
  observe(el) { this.el = el; }
  unobserve() {}
  disconnect() {}
}

function mockStageSize(width, height) {
  const ro = roInstances[roInstances.length - 1];
  expect(ro).toBeTruthy();
  const el = ro.el;
  Object.defineProperty(el, 'clientWidth', { value: width, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: height, configurable: true });
  act(() => ro.cb());
}

beforeEach(() => {
  roInstances = [];
  globalThis.ResizeObserver = MockResizeObserver;
  global.fetch = vi.fn(async () => ({
    ok: true,
    text: async () => HTML,
  }));
});

afterEach(() => {
  globalThis.ResizeObserver = realRO;
});

describe('PptxArtifactPreview', () => {
  it('shows a loading indicator on first render', () => {
    render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    expect(screen.getByText(/loading/i)).toBeTruthy();
  });

  it('renders only the first slide with a 1 / total counter', async () => {
    render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    expect(screen.queryByText('Body.')).toBeNull();
    expect(screen.getByLabelText(/previous slide/i)).toBeDisabled();
    expect(screen.getByLabelText(/next slide/i)).not.toBeDisabled();
  });

  it('advances to the next slide via the next button', async () => {
    render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText(/next slide/i));
    expect(screen.getByText('2 / 3')).toBeInTheDocument();
    expect(screen.getByText('Body.')).toBeInTheDocument();
  });

  it('navigates with arrow keys', async () => {
    render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    const stage = screen.getByLabelText(/presentation stage/i);
    fireEvent.keyDown(stage, { key: 'ArrowRight' });
    expect(screen.getByText('2 / 3')).toBeInTheDocument();
    fireEvent.keyDown(stage, { key: 'ArrowLeft' });
    expect(screen.getByText('1 / 3')).toBeInTheDocument();
    fireEvent.keyDown(stage, { key: 'End' });
    expect(screen.getByText('3 / 3')).toBeInTheDocument();
    fireEvent.keyDown(stage, { key: 'Home' });
    expect(screen.getByText('1 / 3')).toBeInTheDocument();
  });

  it('jumps to a slide via the thumbnail rail', async () => {
    render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    const thumbs = screen.getAllByLabelText(/go to slide/i);
    expect(thumbs.length).toBe(3);
    fireEvent.click(thumbs[thumbs.length - 1]);
    expect(screen.getByText('3 / 3')).toBeInTheDocument();
  });

  it('renders an error state with a download fallback when fetch fails', async () => {
    global.fetch = vi.fn(async () => ({ ok: false, status: 500 }));
    render(<PptxArtifactPreview artifactId="a1" outline={[]} downloadUrl="/dl/a1" title="Deck" />);
    await waitFor(() => expect(screen.getByText(/preview unavailable/i)).toBeInTheDocument());
    const dl = screen.getByText(/download/i).closest('a');
    expect(dl).toBeTruthy();
    expect(dl.getAttribute('href')).toBe('/dl/a1');
  });

  it('sizes the slide at the deck\'s real aspect ratio (4:3), not 16:9', async () => {
    // Backend emits width/height in px on each section's inline style; the
    // component must read those and use them for the stage sizing. A 4:3
    // deck (960x720) would otherwise be clipped to 960x540 and content
    // below y=540 would be invisible.
    const FOUR_THREE_HTML = `
<!DOCTYPE html><html><body>
<section class='zl-slide' data-slide='1' style='position:relative;width:960px;height:720px;background:#fff'>
  <h1>Cover</h1>
</section>
<section class='zl-slide' data-slide='2' style='position:relative;width:960px;height:720px;background:#fff'>
  <h1>Method</h1><p>Body.</p>
</section>
</body></html>
`;
    global.fetch = vi.fn(async () => ({
      ok: true,
      text: async () => FOUR_THREE_HTML,
    }));
    const { container } = render(<PptxArtifactPreview artifactId="a1" />);
    await waitFor(() => expect(screen.getByText('1 / 2')).toBeInTheDocument());
    // The stage wrapper carries the transform: scale(...) style; its
    // width/height must match the real deck (960x720), not the 16:9
    // fallback (960x540).
    const stage = container.querySelector('div[style*="scale("]');
    expect(stage).toBeTruthy();
    const styleAttr = stage.getAttribute('style') || '';
    expect(styleAttr).toMatch(/width:\s*960px/);
    expect(styleAttr).toMatch(/height:\s*720px/);
    expect(styleAttr).not.toMatch(/height:\s*540px/);
  });

  it('keeps translate(-50%,-50%) centering in the same transform as the scale', async () => {
    // Regression: under Tailwind v3 the -translate-x/y-1/2 classes are
    // implemented via the `transform` property, so an inline
    // `transform: scale(...)` used to wipe the centering — the slide's
    // top-left corner sat at the stage center (jammed bottom-right).
    const { container } = render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    const slide = container.querySelector('div[style*="scale("]');
    expect(slide).toBeTruthy();
    expect(slide.getAttribute('style')).toMatch(/transform:\s*translate\(-50%,\s*-50%\)\s*scale\(/);
  });

  it('clips the slide canvas with overflow-hidden so no scrollbars appear', async () => {
    render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    const stage = screen.getByLabelText(/presentation stage/i);
    expect(stage.className).toContain('overflow-hidden');
  });

  it('fits the slide to the container with a 24px margin (contain scale)', async () => {
    const { container } = render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    // Container 500x400, fallback 16:9 slide 960x540:
    // scale = min((500-48)/960, (400-48)/540) = min(0.47083, 0.65185)
    mockStageSize(500, 400);
    const slide = container.querySelector('div[style*="scale("]');
    expect(slide.getAttribute('style')).toMatch(/scale\(0\.47083/);
    expect(slide.getAttribute('style')).not.toMatch(/scale\(1\)/);
  });

  it('recomputes the fitted scale when the panel is resized', async () => {
    const { container } = render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    mockStageSize(500, 400);
    mockStageSize(1000, 800);
    // scale = min((1000-48)/960, (800-48)/540) = min(0.99167, 1.39259)
    const slide = container.querySelector('div[style*="scale("]');
    expect(slide.getAttribute('style')).toMatch(/scale\(0\.99166/);
  });

  it('self-sizes from width when the host gives no definite height', async () => {
    // Regression: in a host whose height is content-driven (inline chat
    // card) the absolute-positioned slide collapses the stage to 0px and
    // the deck renders at scale 1, half off-canvas. The component must
    // fall back to a width-driven fit with an explicit stage height.
    const { container } = render(<PptxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => expect(screen.getByText('1 / 3')).toBeInTheDocument());
    mockStageSize(500, 0);
    const slide = container.querySelector('div[style*="scale("]');
    // Width-driven: scale = (500-48)/960 = 0.47083
    expect(slide.getAttribute('style')).toMatch(/scale\(0\.47083/);
    const stage = screen.getByLabelText(/presentation stage/i);
    // Explicit aspect-ratio height: ceil(452 * 540/960) + 48 = 303px
    expect(stage.getAttribute('style') || '').toMatch(/height:\s*303px/);
  });
});
