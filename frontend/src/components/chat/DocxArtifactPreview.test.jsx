import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import DocxArtifactPreview from './DocxArtifactPreview';

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
  }));
});

describe('DocxArtifactPreview', () => {
  it('shows a loading indicator on first render', () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    expect(screen.getByText(/loading/i)).toBeTruthy();
  });

  it('fetches and renders the HTML', async () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() =>
      expect(screen.getByText('Top paragraph.')).toBeInTheDocument(),
    );
    const container = screen.getByText('Top paragraph.').closest('[data-docx-body]');
    expect(container).toBeTruthy();
  });

  it('renders an error message when the fetch fails', async () => {
    global.fetch = vi.fn(async () => ({ ok: false, status: 500 }));
    render(<DocxArtifactPreview artifactId="a1" outline={[]} />);
    await waitFor(() =>
      expect(screen.getByText(/preview unavailable/i)).toBeInTheDocument(),
    );
  });
});
