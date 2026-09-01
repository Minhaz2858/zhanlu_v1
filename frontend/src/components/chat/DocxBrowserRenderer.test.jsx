import { render, screen, waitFor, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import DocxBrowserRenderer from './DocxBrowserRenderer';

// ---------------------------------------------------------------------------
// Mock docx-preview — renderAsync simply injects a marker into the container
// so we can verify the component wired everything together.
// ---------------------------------------------------------------------------
vi.mock('docx-preview', () => ({
  renderAsync: vi.fn(async (_blob, containerEl) => {
    containerEl.innerHTML =
      '<div class="docx-wrapper"><div class="docx-page"><h1>Rendered</h1><p>Hello docx-preview</p></div></div>';
  }),
}));

// Mock CSS import
vi.mock('@/docx-preview.css', () => ({}));

// ---------------------------------------------------------------------------

const TEST_SRC = '/api/uploads/test.docx';

beforeEach(() => {
  // Default: successful blob fetch
  global.fetch = vi.fn(async () => ({
    ok: true,
    blob: async () => new Blob(['fake-docx-bytes'], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }),
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------

describe('DocxBrowserRenderer', () => {
  it('shows a loading indicator while fetching', () => {
    render(<DocxBrowserRenderer src={TEST_SRC} />);
    expect(screen.getByText(/rendering document/i)).toBeTruthy();
  });

  it('fetches the blob and renders via docx-preview', async () => {
    render(<DocxBrowserRenderer src={TEST_SRC} />);

    await waitFor(() => {
      expect(screen.getByText('Hello docx-preview')).toBeInTheDocument();
    });

    expect(screen.getByText('Rendered')).toBeInTheDocument();
    // The renderer now fetches through authFetch, which forwards to the global
    // fetch with an options object (headers); assert on the URL only.
    expect(global.fetch).toHaveBeenCalled();
    expect(global.fetch.mock.calls[0][0]).toBe(TEST_SRC);
  });

  it('shows an error banner when the blob fetch fails', async () => {
    global.fetch = vi.fn(async () => ({ ok: false, status: 404 }));

    render(<DocxBrowserRenderer src="/api/uploads/missing.docx" title="Report" downloadUrl="/download" />);

    await waitFor(() => {
      expect(screen.getByText(/HTTP 404/)).toBeInTheDocument();
    });

    expect(screen.getByText('Report')).toBeInTheDocument();
    expect(screen.getByText('Download .docx')).toBeInTheDocument();
  });

  it('calls onError when docx-preview renderAsync throws', async () => {
    const { renderAsync } = await import('docx-preview');
    renderAsync.mockRejectedValueOnce(new Error('Parse failure'));

    const onError = vi.fn();
    render(
      <DocxBrowserRenderer
        src={TEST_SRC}
        title="Bad Doc"
        downloadUrl="/download"
        onError={onError}
      />,
    );

    await waitFor(() => {
      expect(onError).toHaveBeenCalledWith('Parse failure');
    });

    // Should show error banner
    expect(screen.getByText('Parse failure')).toBeInTheDocument();
  });

  it('clears the container on unmount', async () => {
    const { unmount } = render(<DocxBrowserRenderer src={TEST_SRC} />);

    await waitFor(() => {
      expect(screen.getByText('Hello docx-preview')).toBeInTheDocument();
    });

    // Unmount should clear innerHTML
    const container = document.querySelector('.docx-wrapper');
    expect(container).toBeTruthy();

    unmount();
    // After unmount, the DOM node should have been cleared
    expect(document.querySelector('.docx-wrapper')).toBeFalsy();
  });

  it('handles missing src gracefully', () => {
    render(<DocxBrowserRenderer src={null} title="No file" />);
    expect(screen.getByText(/no document source/i)).toBeInTheDocument();
  });

  it('renders outline sidebar when outline prop is provided', async () => {
    const outline = [
      { level: 1, text: 'Chapter 1', id: 'chapter-1' },
      { level: 2, text: 'Section 1.1', id: 'section-1-1' },
    ];

    render(
      <DocxBrowserRenderer src={TEST_SRC} outline={outline} />,
    );

    await waitFor(() => {
      expect(screen.getByText('Chapter 1')).toBeInTheDocument();
    });

    expect(screen.getByText('Section 1.1')).toBeInTheDocument();
  });
});
