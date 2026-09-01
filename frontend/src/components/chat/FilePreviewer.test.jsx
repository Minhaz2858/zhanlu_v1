import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';

vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
}));

vi.mock('./DocxBrowserRenderer', () => ({
  default: ({ src, title }) => (
    <div data-testid="docx-renderer" data-src={src}>{title}</div>
  ),
}));

import FilePreviewer from './FilePreviewer';
import { authFetch } from '@/api/authFetch';

beforeEach(() => {
  authFetch.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('FilePreviewer — automation files', () => {
  const automationDocxUrl = '/api/automations/files/file-1/download';

  it('mints a signed token and mounts an iframe for an automation-file docx', async () => {
    authFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          token: 'signed-token',
          url: '/api/automations/files/file-1/preview?token=signed-token',
        }),
    });
    render(
      <FilePreviewer url={automationDocxUrl} name="report.docx" fileType="docx" />,
    );

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

  it('never routes an automation-file docx through the MS Office Online viewer', async () => {
    authFetch.mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          token: 'signed-token',
          url: '/api/automations/files/file-1/preview?token=signed-token',
        }),
    });
    render(
      <FilePreviewer url={automationDocxUrl} name="report.docx" fileType="docx" />,
    );

    await waitFor(() => {
      expect(document.querySelector('iframe')).toBeTruthy();
    });
    const iframe = document.querySelector('iframe');
    expect(iframe.getAttribute('src')).not.toContain('view.officeapps.live.com');
  });

  it('shows a graceful fallback (no iframe) when token minting fails', async () => {
    authFetch.mockResolvedValue({ ok: false, status: 500 });
    render(
      <FilePreviewer url={automationDocxUrl} name="report.docx" fileType="docx" />,
    );

    await waitFor(() => {
      expect(screen.getByText(/could not load preview/i)).toBeTruthy();
      expect(document.querySelector('iframe')).toBeNull();
    });
  });

  it('keeps the local /api/uploads/ docx path (no preview token minted)', () => {
    render(
      <FilePreviewer url="/api/uploads/report.docx" name="report.docx" fileType="docx" />,
    );

    expect(screen.getByTestId('docx-renderer')).toBeTruthy();
    expect(authFetch).not.toHaveBeenCalled();
  });

  it('does not treat an external office URL as an automation file (stays on MS Live)', () => {
    render(
      <FilePreviewer
        url="https://example.com/report.docx"
        name="report.docx"
        fileType="docx"
      />,
    );

    expect(authFetch).not.toHaveBeenCalled();
    const iframe = document.querySelector('iframe');
    expect(iframe).toBeTruthy();
    expect(iframe.getAttribute('src')).toContain('view.officeapps.live.com');
  });
});
