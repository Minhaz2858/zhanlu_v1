import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';

vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
}));

vi.mock('./PptxArtifactPreview', () => ({
  default: () => <div data-testid="pptx-previewer">PptxArtifactPreview</div>,
}));

import InlineArtifactPreview from './InlineArtifactPreview';
import { authFetch } from '@/api/authFetch';

const baseArtifact = {
  id: 'file-1',
  type: 'html',
  file_name: 'monthly-report.html',
  name: 'monthly-report.html',
  title: 'monthly-report.html',
  file_size: 4096,
  file_url: '/api/automations/files/file-1/download',
  preview_url: '/api/automations/files/file-1/preview',
  has_preview: true,
  source: 'automation_file',
};

const signedPreviewUrl =
  '/api/automations/files/file-1/preview?token=signed-token';

beforeEach(() => {
  // jsdom/happy-dom createObjectURL stub so we can assert the blob flow.
  URL.createObjectURL = vi.fn(() => 'blob:mock-preview-url');
  URL.revokeObjectURL = vi.fn();
  authFetch.mockReset();
  // Default: successful signed-token mint.
  authFetch.mockResolvedValue({
    ok: true,
    json: () => Promise.resolve({ url: signedPreviewUrl }),
  });
});

afterEach(() => {
  cleanup();
});

describe('InlineArtifactPreview', () => {
  it('renders title, type label, and size for the deliverable', () => {
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    expect(screen.getByText('monthly-report.html')).toBeTruthy();
    expect(screen.getByText(/4\.0 KB/)).toBeTruthy();
  });

  it('hides the inline Preview button by default (modal mode)', () => {
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    expect(screen.queryByRole('button', { name: /open preview/i })).toBeNull();
  });

  it('delegates Open to onOpen when a parent right-pane handler is provided', async () => {
    // The chat composer / message-bubble path passes `onOpen` (which wires to
    // ArtifactPreviewPane) but leaves `openInModal` at its default. The Open
    // button must route to the right side pane, not the modal popup.
    const onOpen = vi.fn();
    render(<InlineArtifactPreview artifact={baseArtifact} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole('button', { name: /open deliverable in side preview/i }));
    expect(onOpen).toHaveBeenCalledWith(baseArtifact);
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(document.querySelector('iframe')).toBeNull();
      expect(authFetch).not.toHaveBeenCalled();
    });
  });

  it('mounts the dedicated PPTX previewer inside the modal when no onOpen exists', async () => {
    // The automation-run log path passes no `onOpen` — the modal is the only
    // surface, but it must render the real deck instead of an iframe pointed
    // at the raw PPTX bytes (which hung on "Loading preview…").
    const pptxArtifact = {
      ...baseArtifact,
      type: 'pptx',
      file_name: 'deck.pptx',
      name: 'deck.pptx',
      title: 'deck.pptx',
    };
    render(<InlineArtifactPreview artifact={pptxArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /open file in preview popup/i }));
    await waitFor(() => {
      expect(screen.getByTestId('pptx-previewer')).toBeTruthy();
    });
    expect(document.querySelector('iframe')).toBeNull();
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('opens a Dialog with the file name as title and Download + Close in the footer', async () => {
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /open file in preview popup/i }));
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeTruthy();
    });
    expect(screen.getByRole('heading', { name: 'monthly-report.html' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Close preview popup' })).toBeTruthy();
  });

  it('mints a signed token and mounts the iframe when Open is clicked', async () => {
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /open file in preview popup/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalledWith(
        '/api/automations/files/file-1/preview-token',
        { method: 'POST' },
      );
      const iframe = document.querySelector('iframe');
      expect(iframe).toBeTruthy();
      expect(iframe.getAttribute('src')).toBe(signedPreviewUrl);
    });
  });

  it('closes the dialog via the Close button and unmounts the iframe', async () => {
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /open file in preview popup/i }));
    await waitFor(() => {
      expect(document.querySelector('iframe')).toBeTruthy();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Close preview popup' }));
    await waitFor(() => {
      expect(document.querySelector('iframe')).toBeNull();
    });
  });

  it('falls back to download (no empty modal) when the file has no preview', async () => {
    const noPreview = { ...baseArtifact, has_preview: false, preview_url: null };
    authFetch.mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['x'], { type: 'text/html' })),
    });
    render(<InlineArtifactPreview artifact={noPreview} />);
    fireEvent.click(screen.getByRole('button', { name: /open file in preview popup/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalledWith('/api/automations/files/file-1/download');
      expect(URL.createObjectURL).toHaveBeenCalled();
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('downloads through authFetch as a blob via the card Download button', async () => {
    authFetch.mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['x'], { type: 'text/html' })),
    });
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /Download deliverable/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalledWith('/api/automations/files/file-1/download');
      expect(URL.createObjectURL).toHaveBeenCalled();
    });
  });

  it('shows an error state (not a 401 iframe) when token minting fails', async () => {
    authFetch.mockResolvedValue({ ok: false, status: 401 });
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /open file in preview popup/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalled();
      expect(document.querySelector('iframe')).toBeNull();
      expect(screen.getByText(/could not load preview/i)).toBeTruthy();
    });
  });

  it('restores inline behavior when openInModal is false (opt-out)', () => {
    const onOpen = vi.fn();
    render(
      <InlineArtifactPreview artifact={baseArtifact} onOpen={onOpen} openInModal={false} />,
    );
    // Inline Preview toggle is rendered again in the opt-out path.
    expect(screen.getByRole('button', { name: /open preview/i })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /open deliverable in side preview/i }));
    expect(onOpen).toHaveBeenCalledWith(baseArtifact);
  });
});
