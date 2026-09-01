import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/react';
import ArtifactPreviewPane from './ArtifactPreviewPane';

const getDashboard = vi.fn();
vi.mock('@/api/dashboards', () => ({
  getDashboard: (...args) => getDashboard(...args),
}));
vi.mock('@/components/dashboard/DashboardViewer', () => ({
  default: ({ dashboard }) => (
    <div data-testid="live-dashboard-viewer">{dashboard.name}</div>
  ),
}));

// Mock docx-preview so DocxBrowserRenderer's dynamic import resolves instantly
vi.mock('docx-preview', () => ({
  renderAsync: vi.fn(async (_blob, containerEl) => {
    containerEl.innerHTML = '<div class="docx-wrapper"><p>Mocked render</p></div>';
  }),
}));
vi.mock('@/docx-preview.css', () => ({}));

beforeEach(() => {
  localStorage.setItem('zhanlu_lang', 'en');
  globalThis.fetch = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mockFetchPdfOk() {
  // PdfFaithfulPreview needs the fetch to return an application/pdf blob
  // and the body to expose headers.get('content-type').
  const blob = new Blob(['%PDF-1.4 fake'], { type: 'application/pdf' });
  globalThis.fetch.mockResolvedValue({
    ok: true,
    status: 200,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? 'application/pdf' : null) },
    blob: async () => blob,
  });
}

describe('ArtifactPreviewPane (artifact_source routing)', () => {
  it('renders live dashboard artifacts with DashboardViewer', async () => {
    getDashboard.mockResolvedValue({
      id: 'dash-1',
      name: 'Weekly Sales Dashboard',
      definition: { widgets: [] },
      refresh_interval_seconds: 60,
      can_edit: true,
    });

    render(
      <ArtifactPreviewPane
        artifact={{
          type: 'dashboard',
          source: 'dashboard',
          dashboard_id: 'dash-1',
          title: 'Weekly Sales Dashboard',
          live: true,
          has_preview: true,
        }}
        onClose={() => { }}
      />,
    );

    expect(await screen.findByTestId('live-dashboard-viewer')).toHaveTextContent('Weekly Sales Dashboard');
    expect(getDashboard).toHaveBeenCalledWith('dash-1');
  });

  it('routes an automation_file docx to the correct download URL (via file_url)', async () => {
    const artifact = {
      id: 'auto-file-uuid-123',
      type: 'docx',
      title: 'Daily Sales Data Sync',
      file_name: 'Daily Sales Data Sync.docx',
      file_size: 56 * 1024,
      file_url: '/api/automations/files/auto-file-uuid-123/download',
      source: 'automation_file',
      has_preview: true,
    };
    mockFetchPdfOk();

    render(<ArtifactPreviewPane artifact={artifact} onClose={() => { }} />);

    await vi.waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    const calledUrls = globalThis.fetch.mock.calls.map((c) => c[0]);

    // DocxBrowserRenderer fetches the explicit downloadUrl (file_url) as the
    // docx blob source — not a constructed /api/artifacts/{id}/download URL.
    expect(calledUrls).toContain(
      '/api/automations/files/auto-file-uuid-123/download',
    );
  });

  it('routes an Artifact-table docx to /api/artifacts/{id}/download (backward compat)', async () => {
    const artifact = {
      id: 'artifact-uuid-456',
      artifact_id: 'artifact-uuid-456',
      type: 'docx',
      title: 'Internal Report',
      file_name: 'Internal Report.docx',
      file_size: 33 * 1024,
      file_url: '/api/artifacts/artifact-uuid-456/download',
      source: 'artifact',
      has_preview: true,
    };
    mockFetchPdfOk();

    render(<ArtifactPreviewPane artifact={artifact} onClose={() => { }} />);

    await vi.waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    const calledUrls = globalThis.fetch.mock.calls.map((c) => c[0]);

    // With viewMode='html' (default), DocxBrowserRenderer fetches the
    // downloadUrl (file_url) as the docx blob source.
    expect(calledUrls).toContain(
      '/api/artifacts/artifact-uuid-456/download',
    );
  });
});