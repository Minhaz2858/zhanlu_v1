import { describe, it, expect } from 'vitest';
// needsPreviewToken decides whether an iframe pointed at a preview URL must
// first mint a signed ?token= (because the route is Bearer-authenticated and
// an iframe cannot send the Authorization header). Bug: ArtifactPreviewPane
// (the right-anchored pane) pointed its iframe straight at
// /api/automations/files/{id}/preview with NO token → 401, and the browser
// rendered the {"detail":"Authentication required"} JSON inside the pane.
// InlineArtifactPreview already minted a token; the pane did not.
import { needsPreviewToken } from '../previewToken';

describe('previewToken.needsPreviewToken', () => {
  it('automation-files preview routes need a token (Bearer-authenticated)', () => {
    expect(needsPreviewToken('/api/automations/files/abc-123/preview')).toBe(true);
    expect(needsPreviewToken('/api/automations/files/80367e35-7363/preview?x=1')).toBe(true);
  });

  it('artifact sidecar previews are served unauthenticated → no token', () => {
    expect(needsPreviewToken('/api/artifacts/abc-123/preview')).toBe(false);
    expect(needsPreviewToken('/api/artifacts/abc/preview?format=html')).toBe(false);
  });

  it('null/empty/non-string → false (caller falls back to graceful state)', () => {
    expect(needsPreviewToken(null)).toBe(false);
    expect(needsPreviewToken(undefined)).toBe(false);
    expect(needsPreviewToken('')).toBe(false);
  });

  it('absolute URLs to the same authenticated route still need a token', () => {
    expect(needsPreviewToken('http://zhanlu.ai:8000/api/automations/files/abc/preview')).toBe(true);
  });
});
