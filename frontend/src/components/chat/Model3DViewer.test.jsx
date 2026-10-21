/**
 * Model3DViewer + FilePreviewer CAD routing (roadmap P0: "no frontend 3D
 * viewer — users can't see what was built").
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// three + example loaders are dynamically imported inside the effect; the
// viewer bails to the error state before touching them when fetch fails, so
// these tests never need WebGL.
vi.mock('@/api/authFetch', () => ({
  authFetch: vi.fn(),
}));

import Model3DViewer from './Model3DViewer';
import FilePreviewer from './FilePreviewer';
import { authFetch } from '@/api/authFetch';

describe('Model3DViewer', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the error state when the model fetch fails', async () => {
    authFetch.mockRejectedValue(new Error('HTTP 404'));
    render(<Model3DViewer url="/api/uploads/x.stl" name="x.stl" />);
    await waitFor(() => expect(screen.getByTestId('model3d-error')).toBeTruthy());
    expect(screen.getByText(/HTTP 404/)).toBeTruthy();
  });

  it('shows the error state when no URL is given', async () => {
    render(<Model3DViewer url="" name="x.stl" />);
    await waitFor(() => expect(screen.getByTestId('model3d-error')).toBeTruthy());
  });

  it('fetches the model with authFetch (Authorization header)', async () => {
    authFetch.mockRejectedValue(new Error('HTTP 500'));
    render(<Model3DViewer url="/api/uploads/part.stl" name="part.stl" />);
    await waitFor(() =>
      expect(authFetch).toHaveBeenCalledWith('/api/uploads/part.stl'));
  });
});

describe('FilePreviewer CAD routing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each(['stl', 'obj'])('routes .%s files to the 3D viewer', async (ext) => {
    // The real viewer would init WebGL; stub it out and assert routing.
    vi.doMock('./Model3DViewer', () => ({
      default: () => <div data-testid="model3d-stub" />,
      isModelFile: () => true,
      MODEL_EXTS: ['stl', 'obj'],
    }));
    const { default: FP } = await import('./FilePreviewer?routed');
    render(<FP url={`/api/uploads/part.${ext}`} name={`part.${ext}`} />);
    await waitFor(() => expect(screen.getByTestId('model3d-stub')).toBeTruthy());
  });

  it('still renders plain text files as iframes (no regression)', () => {
    authFetch.mockResolvedValue({ ok: true });
    const { container } = render(
      <FilePreviewer url="/api/uploads/notes.txt" name="notes.txt" />,
    );
    expect(container.querySelector('iframe')).toBeTruthy();
  });
});
