import { render, screen, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import DocxArtifactPreview from './DocxArtifactPreview';

// ---------------------------------------------------------------------------
// Mock docx-preview to throw — this forces DocxArtifactPreview to fall back
// to mammoth HTML so we can test both paths cleanly.
// ---------------------------------------------------------------------------
vi.mock('docx-preview', () => ({
  renderAsync: vi.fn(async () => {
    throw new Error('Simulated docx-preview failure');
  }),
}));

// Mock CSS import
vi.mock('@/docx-preview.css', () => ({}));

// ---------------------------------------------------------------------------

const HTML = `
  <h1 id="executive-summary">Executive Summary</h1>
  <p>Top paragraph.</p>
  <h2 id="method">Method</h2>
  <p>Body.</p>
`;

const OUTLINE = [
  { level: 1, text: 'Executive Summary', id: 'executive-summary' },
  { level: 2, text: 'Method', id: 'method' },
];

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true,
    text: async () => HTML,
    blob: async () => new Blob(['fake'], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }),
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DocxArtifactPreview', () => {
  it('shows a loading indicator on first render', () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    // Initially tries DocxBrowserRenderer which shows "Rendering document…"
    expect(screen.getByText(/rendering document/i)).toBeTruthy();
  });

  it('falls back to mammoth HTML when docx-preview fails', async () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);

    // After docx-preview fails, it falls back to mammoth.
    // We need to wait for the error to trigger the fallback, then the mammoth fetch.
    await waitFor(() =>
      expect(screen.getByText('Top paragraph.')).toBeInTheDocument(),
      { timeout: 5000 },
    );

    const container = screen.getByText('Top paragraph.').closest('[data-docx-body]');
    expect(container).toBeTruthy();
    // "Executive Summary" appears in both the outline sidebar and the article
    expect(screen.getAllByText('Executive Summary').length).toBeGreaterThanOrEqual(2);
  });

  it('shows an error when mammoth fallback also fails', async () => {
    // First fetch: docx-preview tries to get blob (succeeds but renderAsync throws)
    // Second fetch: mammoth HTML fails
    let callCount = 0;
    global.fetch = vi.fn(async () => {
      callCount++;
      if (callCount <= 1) {
        // First call: blob fetch for docx-preview
        return { ok: true, blob: async () => new Blob(['fake']) };
      }
      // Second call: mammoth HTML fetch fails
      return { ok: false, status: 500 };
    });

    render(<DocxArtifactPreview artifactId="a1" outline={[]} />);

    await waitFor(() =>
      expect(screen.getByText(/preview unavailable/i)).toBeInTheDocument(),
      { timeout: 5000 },
    );
  });

  it('shows outline sidebar when outline is provided', async () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);

    await waitFor(() => {
      expect(screen.getByText('Executive Summary')).toBeInTheDocument();
    });
  });

  it('shows error when downloadUrl and title are provided', async () => {
    global.fetch = vi.fn(async () => ({ ok: false, status: 500 }));

    render(
      <DocxArtifactPreview
        artifactId="a1"
        outline={[]}
        title="Quarterly Report"
        downloadUrl="/api/artifacts/a1/download"
      />,
    );

    await waitFor(() =>
      expect(screen.getByText('Quarterly Report')).toBeInTheDocument(),
      { timeout: 5000 },
    );

    expect(screen.getByText('Download .docx')).toBeInTheDocument();
  });
});
