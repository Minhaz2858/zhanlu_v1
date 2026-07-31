/**
 * partitionArtifacts — split a chat message's artifacts into the two
 * render surfaces used by MessageBubble:
 *
 * - ``inline``: automation-deliverable / previewable files rendered by
 *   InlineArtifactPreview (Manus-style card + expandable preview).
 * - ``cards``: ordinary chat-only artifacts rendered by ArtifactCardList.
 *
 * The two lists are disjoint — an artifact must never appear in both.
 * Previously MessageBubble passed the full array to ArtifactCardList AND
 * filtered it again for InlineArtifactPreview, so automation files (and
 * any has_preview artifact) rendered as two stacked cards for the same
 * file.
 */
export function partitionArtifacts(artifacts) {
  const list = Array.isArray(artifacts) ? artifacts.filter(Boolean) : [];
  const inline = list.filter(
    (a) => a.source === 'automation_file' || a.source === 'dashboard' || a.has_preview,
  );
  const inlineSet = new Set(inline);
  const cards = list.filter((a) => !inlineSet.has(a));
  return { inline, cards };
}
