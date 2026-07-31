# Inline Preview Design: Zhanlu vs Claude/Manus/Kimi

A comparative analysis of how modern AI agents render generated files inline, and a roadmap for bringing Zhanlu's `ArtifactPreviewCard` to parity.

---

## 1. Comparison Matrix

| Dimension | Claude Artifacts | Manus AI | Kimi (Moonshot) | Zhanlu (current) | Zhanlu (target) |
|---|---|---|---|---|---|
| **Card location** | Inside assistant message, after markdown text | Separate file panel + inline card | Inline card with file summary | `ArtifactPreviewCardList` inside `MessageBubble` (conditional on `create_artifact` tool results) | Same — inside bubble, after text |
| **File-type icon** | 40px tinted square (orange=PPT, blue=DOC, red=PDF, purple=HTML) | 32px icon + thumbnail preview | 24px MIME icon | 40px square with Lucide icon + `TYPE_META` mapping | **48px** hero icon — keeps Lucide + color mapping |
| **Status badge** | Version chip + "Published" green dot | "Building" spin → "Ready" check | N/A | Status badge (draft/building/preview_ready/validated/approved/published/failed/archived) | Same — already complete |
| **Actions** | "Open in Artifacts" (side panel) + "Download" + "Copy" | "Preview" (inline expand) + "Download" | "Open" → full preview | "Preview" (expand/collapse in-bubble) + "Download" | **"Open" (primary, → Sheet) + "Download" (secondary)** |
| **Inline preview** | Full-bleed iframe in side panel (500px height) | 280px constrained iframe in card | Full-page in new tab | iframe expand inside bubble (500px height) | iframe in sheet + first 20 lines of markdown/code in card |
| **Markdown preview** | Code block with syntax highlight in side panel | N/A | N/A | **None** — MD not expanded | **First 20 lines as monospace block** in card body |
| **Versioning** | Version picker (v1, v2, …) in side panel | Version metadata in card header | N/A | `versions[]` in API, no picker UI | Version chip in card header (e.g. "v1") |
| **Real-time status** | SSE stream: building→ready transition | WebSocket: status updates | N/A | **Poll-on-mount only** — no live updates | Future: SSE subscription |
| **Data contract** | Structured artifact object via API | Structured via sandbox result | Flat file object | Two shapes: `ArtifactPreviewCardList` uses `/api/messages/{id}/artifacts` (type-safe), `ArtifactCardList` uses `create_artifact` tool result (flat, no `artifact_type`) | Unified: always use the governed `/api/messages/{id}/artifacts` shape |

---

## 2. Zhanlu Inline Preview Data Flow

```
Agent stream (handleAgentSend / handleSend)
  ↓ SSE: streamAgentResponse(convId, {role: "user", content: text})
  ↓ SSE tokens → setMessages([...prev, {content: fullContent}])
  ↓ [Done event]
  ↓ base44.entities.ChatMessage.update(aiMsg.id, {content, tool_calls})
  ↓
  ↓ (Synexia backend Writes Artifact + MessageArtifact in a separate job)
  ↓
MessageBubble renders:
  1. ReactMarkdown (assistant text)
  2. ClarifyOptions (if [[CLARIFY]] blocks)
  3. ActivitySteps (if activity_steps present)
  4. ResultCard (if [[RESULT]] blocks)
  5. ReportCard (if report data)
  6. DataTableCard (if table data)
  7. ArtifactPreviewCardList ← renders one or more ArtifactPreviewCard
     ↓ fetch GET /api/messages/{messageId}/artifacts
     ↓ returns [{artifact_id, artifact_type, title, status, ...}]
     ↓ ArtifactPreviewCard mounts, fetches GET /api/artifacts/{artifact_id}
     ↓ renders header + expandable preview + actions
```

**Critical finding**: `ArtifactPreviewCardList` is already wired into `MessageBubble` and fires on mount. The E2E test just needs to wait for this fetch to complete.

---

## 3. Components Inventory

| Component | File | Role | Gaps |
|---|---|---|---|
| `ArtifactPreviewCard` | `ArtifactPreviewCard.jsx` | Governed-artifact inline card | No live status, cramped expand, MD not rendered |
| `ArtifactPreviewCardList` | Same file (named export) | Fetches + renders cards per message | Poll-only, no error state, hides when empty |
| `ArtifactCardList` | `ArtifactCardList.jsx` | Compact horizontal-scroll rows for `create_artifact` tool results | Data shape mismatch with governed API |
| `ArtifactPreviewSheet` | `ArtifactPreviewSheet.jsx` | Right-side Sheet for full preview | `artifact.type` vs `artifact_type` mismatch, no version picker |
| `ArtifactPanel` | `ArtifactPanel.jsx` | Full resizable side panel for `[[RESULT]]` draft resources | Legacy `[[RESULT]]` flow only, not artifact-driven |

---

## 4. Gap Analysis & Recommendations

### Gap 1: No Markdown inline preview
- **Current**: MD artifacts show the header card but no content preview in the collapsed state.
- **Fix**: When `artifact_type === 'md'`, fetch the first 20 lines from the preview blob endpoint and render as a `<pre>` block inside the card.

### Gap 2: "Open" button does not open the Sheet
- **Current**: The only expand action is in-bubble iframe (cramped at 500px).
- **Fix**: Add a primary "Open" button that calls the parent's `onArtifactPreview` callback (already passed through `MessageBubble` → `ArtifactPreviewSheet`).

### Gap 3: Card too subtle
- **Current**: 40px icon, small action row, no hover lift.
- **Fix**: 48px icon, `shadow-md → shadow-lg` on hover, primary-filled "Open" beside secondary-outline "Download".

### Gap 4: No version indicator
- **Current**: Version number hidden in metadata field — only shown if `artifact.versions.length > 0`.
- **Fix**: Always show "v{version_number}" chip when available.

### Gap 5: Two separate artifact rendering paths
- **Current**: `ArtifactPreviewCardList` (governed) vs `ArtifactCardList` (tool results) — two data shapes, two rendering paths.
- **Long-term fix**: Unify under the governed `/api/messages/{id}/artifacts` shape. Deprecate `ArtifactCardList` flat shape.

---

## 5. Rework Design (Claude-Style Polish)

### `ArtifactPreviewCard.jsx` — Proposed Changes

```
┌─────────────────────────────────────────────────┐
│ ┌──────────┐  report.md          [Preview Ready]│
│ │  MD icon │  v1 · 4.2 KB                       │
│ │  48×48   │                                    │
│ └──────────┘                                    │
│ ┌─────────────────────────────────────────────┐ │
│ │  1  # Sales Report                          │ │
│ │  2                                          │ │
│ │  3  ## Q1 2026 Results                      │ │
│ │  4  | Region  | Revenue | Growth |          │ │
│ │  ... (first 20 lines)                       │ │
│ └─────────────────────────────────────────────┘ │
│  [ Open ]  [ Download ]                         │
└─────────────────────────────────────────────────┘
```

### Changes:
1. **Header**: 48px square file-type icon, title + version·size on one line, status badge top-right
2. **Body (expanded)**: For `.md`, render first 20 lines of markdown as monospace. For HTML/PDF, keep the existing iframe.
3. **Actions**: Primary "Open" (calls `onArtifactPreview` prop → opens `ArtifactPreviewSheet`), secondary "Download"
4. **Hover**: `shadow-sm → shadow-md` (200ms ease), no other transform
5. **Animation**: `max-height` transition on expand/collapse (300ms ease-in-out)
