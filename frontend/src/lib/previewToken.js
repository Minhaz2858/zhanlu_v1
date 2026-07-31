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
// `/api/artifacts/{id}/preview` (the chat-artifact sidecar route) is served
// UNAUTHENTICATED by design, so it needs no token.

const AUTOMATION_FILES_PREVIEW = /\/api\/automations\/files\/[^/]+\/preview(\?|$)/;

export function needsPreviewToken(previewUrl) {
  if (!previewUrl || typeof previewUrl !== 'string') return false;
  return AUTOMATION_FILES_PREVIEW.test(previewUrl);
}
