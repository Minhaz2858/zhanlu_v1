# Zhanlu Existing UI Integration Guide

## Principle

The user already has a UI. Do not replace it. Connect it to the backend APIs.

## Tasks for Claude Code

1. Inspect the existing frontend.
2. Identify current routes, stores, chat components, sidebar components, artifact/preview areas, and API service files.
3. Map UI components to the API contract.
4. Add missing API client functions without rewriting unrelated UI.
5. Add a live event renderer for SSE/WebSocket events.
6. Add ArtifactPreviewCard if not present.
7. Add fallback rendering if a preview type is not ready.

## UI features to connect

- login/session,
- app/workspace selector,
- conversation list,
- chat message list,
- chat input,
- streaming response,
- execution timeline,
- command output cards,
- artifact preview card,
- artifact download,
- agent selector,
- skill list,
- datasource list,
- settings/admin if present.

## Artifact preview component behavior

ArtifactPreviewCard should accept:

```ts
type ArtifactCardProps = {
  artifactId: string;
  artifactVersionId?: string;
  artifactType: 'md' | 'html' | 'pptx' | 'docx' | 'dashboard' | 'mini_app' | string;
  title: string;
  status: string;
  previewAvailable: boolean;
  actions: string[];
};
```

Actions:

- Preview
- Download
- Regenerate
- Approve
- Publish later

## Event timeline component behavior

Timeline should render:

- execution events as steps,
- sandbox commands as code/output cards,
- artifact readiness as artifact cards,
- failures as retry/error cards.

## Do not

- Do not rebuild visual design from zero.
- Do not remove existing UI pages unless broken.
- Do not hardcode fake data after backend integration.
- Do not use localStorage as source of truth for conversations/artifacts.
- Do not expose backend secrets or raw file paths.
