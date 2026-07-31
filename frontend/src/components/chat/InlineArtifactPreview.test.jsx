import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
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

beforeEach(() => {
  // jsdom has no createObjectURL — stub it so we can assert the blob flow.
  URL.createObjectURL = vi.fn(() => 'blob:mock-preview-url');
  URL.revokeObjectURL = vi.fn();
  authFetch.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('InlineArtifactPreview', () => {
  it('renders title, type label, and size for the deliverable', () => {
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    expect(screen.getByText('monthly-report.html')).toBeTruthy();
    expect(screen.getByText(/4\.0 KB/)).toBeTruthy();
  });

  it('offers an Open action that calls the supplied handler', () => {
    const onOpen = vi.fn();
    render(<InlineArtifactPreview artifact={baseArtifact} onOpen={onOpen} />);
    fireEvent.click(screen.getByRole('button', { name: /Open deliverable in side preview/i }));
    expect(onOpen).toHaveBeenCalledWith(baseArtifact);
  });

  it('hides the inline Preview toggle when an onOpen (right-pane) handler is provided', () => {
    // The user can preview in the right-side pane (opened by the Open button),
    // so the inline expand-preview toggle is redundant in that context and is
    // hidden. Open + Download remain.
    const onOpen = vi.fn();
    render(<InlineArtifactPreview artifact={baseArtifact} onOpen={onOpen} />);
    expect(screen.queryByRole('button', { name: /Open preview|Hide preview/i })).toBeNull();
    expect(screen.getByRole('button', { name: /Open deliverable in side preview/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Download deliverable/i })).toBeTruthy();
  });

  it('still shows the inline Preview toggle when no onOpen handler is given (e.g. run logs)', () => {
    // ExecutionLogsList renders this card WITHOUT onOpen — there the inline
    // preview is the ONLY way to preview (no right pane), so the toggle stays.
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    expect(screen.getByRole('button', { name: /Open preview/i })).toBeTruthy();
  });

  it('downloads through authFetch as a blob (authenticated route)', async () => {
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

  it('mints a signed token and points the iframe at the signed url', async () => {
    // iframes cannot send Authorization headers; the component mints a
    // short-lived signed token and uses the returned ?token= url so the
    // backend CSP sandbox headers still apply (unlike a blob: iframe).
    authFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        token: 'signed-token',
        url: '/api/automations/files/file-1/preview?token=signed-token',
      }),
    });
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /Open preview/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalledWith(
        '/api/automations/files/file-1/preview-token',
        { method: 'POST' },
      );
      const iframe = document.querySelector('iframe');
      expect(iframe).toBeTruthy();
      expect(iframe.getAttribute('src')).toBe(
        '/api/automations/files/file-1/preview?token=signed-token',
      );
    });
  });

  it('shows an error state (not a 401 iframe) when token minting fails', async () => {
    authFetch.mockResolvedValue({ ok: false, status: 401 });
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /Open preview/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalled();
      expect(document.querySelector('iframe')).toBeNull();
      expect(screen.getByText(/could not load preview/i)).toBeTruthy();
    });
  });
});
