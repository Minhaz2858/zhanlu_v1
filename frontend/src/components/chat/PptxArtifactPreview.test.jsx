import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
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

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true,
    text: async () => HTML,
  }));
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
});
