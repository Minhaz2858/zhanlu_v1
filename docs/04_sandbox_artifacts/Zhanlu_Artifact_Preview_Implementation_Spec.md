# Zhanlu Artifact Preview Implementation Spec

## Purpose

Generated outputs should appear inside chat as inline artifacts, similar to modern AI artifact/canvas interfaces.

## Core rule

Frontend never reads raw server file paths. Every preview/download action goes through permission-checked backend APIs.

## Artifact lifecycle

```text
draft → building → preview_ready → editing → validated → approved → published/exported
```

## Artifact types

MVP:

- `md`
- `html`
- `pptx`

Later:

- `docx`
- `pdf`
- `xlsx`
- `chart`
- `dashboard`
- `mini_app`

## Required tables

```sql
CREATE TABLE artifacts (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    conversation_id UUID,
    execution_id UUID,
    created_by_user_id UUID,
    created_by_agent_id UUID,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'creating',
    current_version_id UUID,
    visibility TEXT NOT NULL DEFAULT 'conversation_private',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE artifact_versions (
    id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    version INT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    source_json JSONB DEFAULT '{}',
    build_manifest JSONB NOT NULL DEFAULT '{}',
    validation_report JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE artifact_blobs (
    id UUID PRIMARY KEY,
    artifact_version_id UUID NOT NULL,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    blob_kind TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_name TEXT NOT NULL,
    data BYTEA NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE message_artifacts (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    message_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    artifact_version_id UUID NOT NULL,
    display_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Preview behavior by type

### Markdown

Storage:

- original `.md` as artifact blob,
- rendered HTML as preview blob or generated on request.

Preview:

```text
GET /api/v1/artifacts/{artifact_id}/preview
```

Returns sanitized rendered HTML or JSON:

```json
{
  "preview_kind": "rendered_html",
  "html": "<h1>...</h1>"
}
```

### HTML

Storage:

- original HTML/CSS/JS as artifact version/source,
- preview served through sandboxed route.

Preview:

```html
<iframe sandbox="allow-scripts" src="/api/v1/artifacts/{artifact_id}/preview"></iframe>
```

Default iframe must not include:

- `allow-same-origin`,
- `allow-forms`,
- `allow-popups`,
- credentials,
- unrestricted network.

### PPTX

Storage:

- original PPTX blob,
- preview PDF blob if generated,
- slide thumbnails if generated,
- source_json containing slide structure when available.

Preview path:

```text
PPTX → PDF preview → thumbnails → inline viewer/card
```

APIs:

```text
GET /api/v1/artifacts/{artifact_id}/preview
GET /api/v1/artifacts/{artifact_id}/thumbnail?page=1
GET /api/v1/artifacts/{artifact_id}/download
```

MVP fallback if PDF converter is missing:

- show artifact card,
- show slide outline from `source_json`,
- provide download,
- mark preview as `limited`.

### DOCX

Storage:

- original DOCX blob,
- PDF preview when converter available,
- document outline in source_json.

Preview:

- PDF viewer if available,
- outline + download fallback.

### Dashboard

Dashboard is a data-bound artifact.

Storage:

- dashboard_definition JSON,
- chart specs,
- data_snapshot_ids,
- refresh_policy,
- version history.

Preview:

- frontend renders dashboard JSON using approved React chart components.

### Mini app

Mini app is a sandboxed interactive artifact.

Storage:

- mini_app_definition,
- source package,
- manifest,
- permissions,
- validation report.

Preview:

- sandboxed iframe,
- no data access unless explicit approved data binding exists.

## Artifact card actions

Every artifact card may show:

```text
Preview
Edit
Regenerate
Compare versions
Approve
Publish
Download
Export
Share
Schedule update
Open full workspace
```

Available actions depend on:

- artifact type,
- user permission,
- validation status,
- visibility,
- policy.

## Regeneration

Endpoint:

```text
POST /api/v1/artifacts/{artifact_id}/regenerate
```

Request:

```json
{
  "instruction": "Make slide 3 simpler.",
  "target_part_id": "slide_3"
}
```

Regeneration creates a new execution and new artifact version. Do not overwrite the existing version.

## Validation

Artifacts are not trusted until validation passes.

Validation checks may include:

- file opens successfully,
- preview generated,
- file size within limit,
- no unsafe macros,
- all required source references exist,
- data-driven claims link to DataSnapshots,
- template rules followed,
- sandbox output manifest valid.

## DOCX Inline Preview (2026-07-15)

DOCX artifacts are previewed in two tiers, selected server-side and
advertised on `GET /api/artifacts/{id}`:

1. **Self-hosted inline reader (always available)** — `DocxArtifactPreview`
   fetches `/api/artifacts/{id}/preview?format=html`. The backend converts
   the DOCX to sanitized HTML with `python-mammoth` and returns it with
   `Content-Type: text/html`. The same artifact payload carries a heading
   outline (`preview_outline`) used by `DocxOutline` for in-page
   navigation in the card expander and side panel.

2. **Side panel — Microsoft Word Online (opt-in)** — when
   `APP_PUBLIC_URL` is set, the backend advertises
   `ms_word_open_url` pointing at `view.officeapps.live.com/op/embed.aspx?src=<signed>`.
   The sheet shows an "Open in Word" button that opens this URL in a
   new tab. The signed token is HMAC-SHA256 over `artifact_id:exp` with
   a 5-minute TTL; only the download endpoint accepts it.

**Download remains the always-on escape hatch** — every surface keeps
the existing Download button.

### Privacy & deployment notes

- The MS Word Online tier is *never* used unless `APP_PUBLIC_URL` is
  configured. See `app.services.artifacts.preview_builder.build_ms_word_open_url`.
- Signed tokens have a 5-minute TTL; they are validated by
  `_verify_artifact_token` before any download is served.
- `FilePreviewer` no longer routes Office docs to `view.officeapps.live.com`
  by default — it prefers our own `/api/artifacts/` endpoint.
- mammoth strips raw HTML from the DOCX; the inline reader renders
  inside a sandboxed container with `data-docx-body` for tests.

### Endpoint summary

| Endpoint | Purpose |
|---|---|
| `GET /api/artifacts/{id}/preview?format=html` | Sanitized DOCX HTML (mammoth) |
| `GET /api/artifacts/{id}/preview?format=pdf`  | Existing PDF path |
| `GET /api/artifacts/{id}/download?token=…`     | Existing download, now accepts a short-lived signed token |
| `GET /api/artifacts/{id}`                       | Now includes `preview_modes`, `preview_outline`, `ms_word_open_url` |

### Components

| Component | Purpose |
|---|---|
| `DocxArtifactPreview` | Fetches `/preview?format=html` and renders in sandboxed container |
| `DocxOutline` | Sidebar nav of heading outline, fires onJump with anchor id |
| `ArtifactPreviewCard` | Shows `DocxArtifactPreview` in expanded card for DOCX |
| `ArtifactPreviewSheet` | Shows `DocxArtifactPreview` in side panel; "Open in Word" button when available |
| `FilePreviewer` | Gated — prefers self-hosted preview for `/api/artifacts/` URLs |


---

## Marker Contract + PPTX Inline Preview (2026-07-21)

This section documents the marker-driven artifact runtime that closes the gap between the `docx`, `pptx`, and `web-artifacts-builder` skills' documented output behavior and the backend's actual artifact pipeline.

### Marker contract

Skills instruct the LLM to emit a marker at the END of its reply describing the file it just wrote to `outputs/`. Three kinds are supported:

```
◤MD_DOCX◤{"md_path": "outputs/report.md", "filename": "Report.docx"}◤END_MD_DOCX◤
◤HTML_DOCX◤{"html_path": "outputs/r.html", "filename": "R.docx"}◤END_HTML_DOCX◤
◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Deck.pptx"}◤END_PPTX◤
```

### Flow

1. The LLM emits the marker as the last line of its reply.
2. The v2 `add_message` endpoint (`backend/app/routers/agents.py`) parses the marker via `app.services.artifact_markers.find_markers`.
3. For each marker, the proven `_create_artifact_tool` pipeline is called with the appropriate `type` (`docx` for MD/HTML markers, `pptx` for slide markers).
4. The assistant message content is then stripped of all marker text via `strip_markers`, so the user only sees the prose.
5. The artifact is stored, indexed, and rendered via the existing inline-preview card. Best-effort: a marker failure is logged as non-fatal and never breaks the chat response.

The v3 SSE `add_message_stream` endpoint applies the same logic before the final delta emit, so streamed chunks also never expose raw `◤` text.

### PPTX → HTML preview strategy

`backend/app/services/artifacts/preview_builder.py` adds two functions:

- `convert_pptx_to_html(pptx_bytes)` — uses python-pptx to walk each slide, extracting text frames, tables, and pictures. Pictures are inlined as base64 `<img>` tags with a 5 MB total cap. Slide text is HTML-escaped via the `html.escape` stdlib function (defense in depth).
- `extract_pptx_outline(pptx_bytes)` — returns one outline entry per slide, using the slide's title (or "Slide N" if no title is set).

The artifact GET endpoint advertises `preview_modes = ["self_hosted_html"]` and a slide outline for PPTX artifacts, mirroring the DOCX shape.

`GET /api/artifacts/{id}/preview?format=html` accepts both `docx` and `pptx` and dispatches to the correct converter.

The React reader `frontend/src/components/chat/PptxArtifactPreview.jsx` mirrors the DOCX reader: fetches the HTML, renders it inside an `article`, and shows a slide sidebar driven by the outline. Clicking a sidebar entry scrolls the body to the matching slide via the `data-slide` attribute.

### Updated default-skills prompt block

`backend/app/services/agent_prompts.py` now tells the agent to:

1. Call `skill_view(name)` to load the skill's methodology (progressive disclosure).
2. Follow the methodology to produce the file content.
3. If the skill body instructs emitting a marker (`◤MD_DOCX◤` / `◤HTML_DOCX◤` / `◤PPTX◤`), emit it at the END of the reply.
4. For long-running or tool-heavy generation, call `run_sandbox_skill(format=..., data=..., title=..., instructions=...)` instead of inline execution — it runs in an isolated Docker sandbox.

### Adopted MiniMax ideas (without replacing Zhanlu skills)

1. **Slide-type conventions** (from `pptx-generator`): a 9-row table in the pptx skill body listing Cover / Section divider / TOC / Content / Data callout / Comparison / Quote / Summary / Thank you, with when-to-use and layout guidance.
2. **XSD validation invocation** (from `minimax-docx`): the docx skill body now documents the 39 bundled XSD schemas and the default-on validation behavior of `pack.py`.
