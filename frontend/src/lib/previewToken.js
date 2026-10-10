// Decide whether an iframe pointed at a preview URL must first mint a signed
// `?token=` — because the route is Bearer-authenticated and an iframe/img
// cannot send the Authorization header.
//
// Bug fixed 2026-07-29: ArtifactPreviewPane (the right-anchored preview pane)
// pointed its iframe straight at `/api/automations/files/{id}/preview` with NO
// token. That route requires auth (services/automation_api preview endpoint),
// so the iframe got `401 {"detail":"Authentication required"}` and the browser
// rendered the raw JSON inside the pane instead of the file. The inline card
// (InlineArtifactPreview) already minted a token via POST
// `/api/automations/files/{id}/preview-token`; the pane did not.
//
// Bug fixed 2026-10-10: `/api/artifacts/{id}/preview` was believed to be
// "served UNAUTHENTICATED by design" — but the artifacts router now applies
// `get_current_user_required` to EVERY route, so iframe previews of chat
// artifacts rendered `401 {"detail":"Authentication required"}` too. Mint
// tokens for both URL families. The mint endpoint differs:
//   /api/automations/files/{id}/preview-token   (AutomationFile table)
//   /api/artifacts/{id}/preview-token           (Artifact table)

import { authFetch } from '../api/authFetch';

const AUTOMATION_FILES_PREVIEW = /\/api\/automations\/files\/[^/]+\/preview(\?|$)/;
const ARTIFACTS_PREVIEW = /\/api\/artifacts\/[^/?]+\/(preview|download|thumbnail)(\?|$)/;

export function needsPreviewToken(previewUrl) {
  if (!previewUrl || typeof previewUrl !== 'string') return false;
  return AUTOMATION_FILES_PREVIEW.test(previewUrl) || ARTIFACTS_PREVIEW.test(previewUrl);
}

/**
 * POST the right preview-token endpoint for `previewUrl` and resolve with
 * the signed `{ url }` the backend returned. Uses the global authFetch.
 */
export async function mintPreviewTokenUrl(previewUrl) {
  if (!previewUrl || typeof previewUrl !== 'string') return null;
  let mintUrl = null;
  let m = previewUrl.match(/\/api\/automations\/files\/([^/?]+)/);
  if (m) {
    mintUrl = `/api/automations/files/${m[1]}/preview-token`;
  } else {
    m = previewUrl.match(/\/api\/artifacts\/([^/?]+)/);
    if (m) mintUrl = `/api/artifacts/${m[1]}/preview-token`;
  }
  if (!mintUrl) return null;
  const res = await authFetch(mintUrl, { method: 'POST' });
  if (!res || !res.ok) return null;
  const data = await res.json();
  return data && data.url ? data.url : null;
}
