# Zhanlu File Storage and Inline Preview Architecture

**Status:** Final design note  
**Scope:** Generated files, database-backed artifact storage, inline chat preview, sandboxed skills, user data, dashboards, mini apps, and recurring refresh workflows.  
**Primary goal:** Help Claude Code and future developers build a robust file handling and preview system inside Zhanlu’s AI chat workspace.

---

## 1. Core Design Principle

Generated files in Zhanlu must not be treated as simple chat attachments. They must be treated as governed, versioned **Artifacts**.

```text
User message
→ AI planning
→ sandboxed skill execution
→ generated output
→ artifact storage
→ preview generation
→ inline chat artifact card
→ edit / regenerate / approve / download / publish / schedule update
```

The AI agent does not directly “send a file” to the user. The agent creates a governed Artifact through Layer 5 execution. The Artifact is stored, versioned, validated, linked to its source data, and previewed through permission-checked APIs.

Official rule:

```text
Chat messages store conversation content.
Executions store process state.
Artifacts store generated files and versions.
Preview APIs display generated files inline.
Sandbox folders are temporary only.
PostgreSQL remains the source of truth.
Redis is temporary infrastructure only.
MinIO or S3-compatible storage is optional scalable binary storage.
```

---

## 2. Modern AI Product Pattern

Modern AI systems such as Claude, Manus-style agents, Kimi-style file/app agents, GPT-based agents, and code-oriented agents typically expose a similar workflow:

```text
chat request
→ agent decomposes task
→ sandbox or tool execution
→ file/app/dashboard generation
→ inline artifact card
→ preview / edit / regenerate / download / publish
```

The exact private storage implementation of those platforms is not public, but the visible user experience pattern is clear:

1. The user requests a file, report, webpage, dashboard, or app.
2. The agent uses tools or a sandbox to create the output.
3. The UI shows progress or task steps.
4. The generated output appears as an inline artifact card.
5. The user can open a preview panel, download, publish, or ask for edits.

Zhanlu should implement the same product feeling but with enterprise-grade storage, permission, audit, and sandbox control.

---

## 3. End-to-End Flow for Generated Files

Example user request:

```text
Finance Agent, make a Q2 finance PPT from our database.
```

Recommended flow:

```text
1. User sends message in chat.
2. Backend stores the message.
3. Layer 1 creates RequestEnvelope.
4. Synexia creates TaskSpec and PlanDAG.
5. Finance Agent is selected.
6. Agent permission and datasource binding are checked.
7. Datasource Gateway creates a read-only DataSnapshot.
8. Layer 5 creates artifact_build_job and sandbox_job.
9. Sandbox Worker starts a temporary Docker sandbox.
10. Sandbox receives only approved input package.
11. PPT skill generates PPTX.
12. Preview Builder creates PDF preview and thumbnails.
13. Validation Engine validates file and preview.
14. Artifact Service stores metadata, binaries, previews, source refs, and validation report.
15. Assistant message is linked to the Artifact.
16. Frontend receives artifact.preview_ready event.
17. Chat UI shows inline Artifact Preview Card.
```

Important rule:

```text
The sandbox must not receive database credentials.
The sandbox receives DataSnapshots, templates, charts, and skill packages.
```

---

## 4. Storage Model Options

Zhanlu can support three storage strategies. The architecture should allow migration from MVP to production without changing the product model.

---

### Option A: Store Entire Files in PostgreSQL

In this model, generated files are stored in PostgreSQL using `BYTEA` or PostgreSQL large object style storage.

PostgreSQL stores:

```text
original PPTX
DOCX
PDF preview
slide thumbnails
Markdown source
HTML source
image previews
artifact metadata
permissions
version history
audit records
```

Good for:

```text
MVP
single-server Docker deployment
strict database-first governance
simple backup
simple development
transactional consistency
```

Weaknesses:

```text
large file bloat
slower database backup/restore
high traffic downloads stress PostgreSQL
preview thumbnails and large PDFs increase DB size
```

Recommended use:

```text
Use PostgreSQL BYTEA for the first MVP and small/medium deployments.
```

---

### Option B: Store Metadata in PostgreSQL, Files in MinIO/S3

In this model, PostgreSQL stores the source of truth and MinIO stores the large binary files.

PostgreSQL stores:

```text
artifact_id
org_id
app_id
conversation_id
execution_id
owner
permissions
artifact_type
version
file name
MIME type
size
checksum
storage backend
object_key
source_refs
build_manifest
validation_report
preview refs
audit records
```

MinIO stores:

```text
original PPTX
original DOCX
preview PDF
slide thumbnails
images
HTML bundle
mini app bundle
dashboard export
large uploaded files
```

Good for:

```text
production scale
large files
many previews
fast binary storage
S3-compatible future migration
separate object lifecycle policies
```

Weaknesses:

```text
more infrastructure
must secure object access
must keep DB metadata and object storage consistent
must back up PostgreSQL and MinIO together
```

Recommended use:

```text
Use this for production or when generated files become large/frequent.
```

---

### Option C: Hybrid Storage

This is the recommended final direction.

```text
PostgreSQL = source of truth
MinIO = scalable binary object storage
Redis = temporary queue/cache/event infrastructure
```

Hybrid rules:

```text
Small artifacts may be stored directly in PostgreSQL.
Large artifacts are stored in MinIO.
PostgreSQL always stores metadata, ownership, permissions, version, checksum, source refs, validation report, and audit.
The frontend never relies on raw object paths.
All access goes through permission-checked Artifact APIs or short-lived signed URLs.
```

Recommended migration plan:

```text
MVP:
  PostgreSQL stores metadata and binary blobs.

Production:
  PostgreSQL stores metadata, governance, checksums, and lineage.
  MinIO stores large binary files and preview derivatives.
  Redis handles queues, locks, and event fanout.
```

---

## 5. Role of PostgreSQL, Redis, and MinIO

### PostgreSQL

PostgreSQL is the permanent source of truth.

Store:

```text
users
organizations
apps/projects
conversations
messages
request_envelopes
executions
execution_events
agents
skills
datasources
data_snapshots
artifacts
artifact_versions
artifact_blobs or object refs
artifact_previews
message_artifacts
sandbox_jobs
audit_logs
schedules
dashboard_definitions
mini_app_definitions
artifact_interactions
```

PostgreSQL answers:

```text
Who owns this file?
Which chat created it?
Which agent created it?
Which skill created it?
Which data snapshot was used?
Can this user preview it?
Which version is current?
Was it validated?
Was it approved?
Can it be published?
```

### Redis

Redis is temporary infrastructure only.

Use Redis for:

```text
worker queue
sandbox job queue
live event fanout
temporary progress state
locks
rate limiting
worker heartbeat
short-lived preview cache keys
SSE/WebSocket event coordination
```

Do not use Redis for:

```text
permanent PPT storage
permanent artifact metadata
audit logs
long-term chat history
source of truth
```

Rule:

```text
If Redis is lost, Zhanlu may restart or replay jobs, but it must not lose artifacts, messages, executions, or audit truth.
```

### MinIO

MinIO or S3-compatible object storage is optional for scalable binary storage.

Use MinIO for:

```text
original PPTX
original DOCX
preview PDF
slide thumbnails
image previews
HTML bundles
mini app bundles
dashboard static exports
large uploaded documents
large generated files
```

Do not expose arbitrary MinIO paths directly to the frontend.

Access options:

```text
Backend proxy:
  frontend calls Zhanlu API
  backend checks permission
  backend streams object from MinIO

Short-lived signed URL:
  backend checks permission
  backend creates short-lived signed URL
  frontend uses URL temporarily
```

For MVP, use backend proxy. For production downloads/previews, signed URLs may be used after permissions are checked.

---

## 6. Artifact Types and Preview Strategies

| Artifact Type | Original Storage | Preview Derivative | Inline Preview Method |
|---|---|---|---|
| PPTX | PPTX file | PDF preview + slide thumbnails | PDF viewer or slide carousel |
| DOCX | DOCX file | PDF preview | PDF viewer |
| PDF | PDF file | thumbnails | PDF.js or browser PDF viewer |
| Markdown | `.md` text | sanitized HTML | rendered markdown component |
| HTML | HTML/CSS/JS bundle | same bundle | sandboxed iframe |
| Image | PNG/JPG/WebP | thumbnail + original | image viewer |
| Dashboard | dashboard JSON + data refs | rendered React view | dashboard renderer |
| Mini App | source/build bundle | iframe app preview | sandboxed iframe |
| Chart | image/SVG/JSON | PNG/SVG preview | chart card |
| XLSX | XLSX file | HTML/PDF/table preview | table viewer or PDF preview |

MVP recommendation:

```text
PPTX/DOCX → convert to PDF → preview inline
MD → render to sanitized HTML
HTML → sandboxed iframe
Dashboard → React JSON renderer
Mini App → sandboxed iframe
Images → direct permission-checked preview
```

---

## 7. Preview Generation Pipeline

Preview generation should be asynchronous when the file is heavy.

```text
artifact created
↓
preview job queued
↓
preview worker converts file
↓
preview saved as artifact preview
↓
artifact.preview_ready event emitted
↓
frontend updates inline artifact card
```

For PPTX:

```text
PPTX
→ PDF preview
→ slide thumbnails
→ preview metadata
```

For DOCX:

```text
DOCX
→ PDF preview
→ page thumbnails if needed
```

For Markdown:

```text
Markdown
→ sanitized HTML
→ inline rendered preview
```

For HTML:

```text
HTML/CSS/JS bundle
→ security scan
→ iframe preview with CSP/sandbox attributes
```

For dashboard:

```text
dashboard_definition.json
+ DataSnapshot refs
→ React dashboard renderer
→ inline interactive preview
```

For mini app:

```text
mini app source/build bundle
→ sandbox build/test
→ iframe preview
→ optional publish after approval
```

---

## 8. Recommended Database Tables

### artifacts

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
    visibility TEXT NOT NULL DEFAULT 'conversation_private',
    current_version_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### artifact_versions

```sql
CREATE TABLE artifact_versions (
    id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL REFERENCES artifacts(id),
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    version INT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    checksum TEXT NOT NULL,
    storage_backend TEXT NOT NULL,
    -- postgres_bytea | minio | s3
    object_key TEXT,
    source_json JSONB NOT NULL DEFAULT '{}',
    build_manifest JSONB NOT NULL DEFAULT '{}',
    validation_report JSONB NOT NULL DEFAULT '{}',
    source_refs JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### artifact_blobs for PostgreSQL-only MVP

```sql
CREATE TABLE artifact_blobs (
    id UUID PRIMARY KEY,
    artifact_version_id UUID NOT NULL REFERENCES artifact_versions(id),
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    blob_kind TEXT NOT NULL,
    -- original | preview_pdf | thumbnail | html_source | markdown_source | image_preview
    mime_type TEXT NOT NULL,
    file_name TEXT NOT NULL,
    data BYTEA NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### artifact_objects for MinIO/S3 hybrid storage

```sql
CREATE TABLE artifact_objects (
    id UUID PRIMARY KEY,
    artifact_version_id UUID NOT NULL REFERENCES artifact_versions(id),
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    object_kind TEXT NOT NULL,
    -- original | preview_pdf | thumbnail | bundle | export
    storage_backend TEXT NOT NULL DEFAULT 'minio',
    bucket TEXT NOT NULL,
    object_key TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### message_artifacts

```sql
CREATE TABLE message_artifacts (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    message_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    artifact_version_id UUID NOT NULL,
    display_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### artifact_interactions

```sql
CREATE TABLE artifact_interactions (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    user_id UUID NOT NULL,
    conversation_id UUID,
    artifact_id UUID NOT NULL,
    artifact_version_id UUID,
    action TEXT NOT NULL,
    -- preview_opened | downloaded | approved | rejected | regenerated | published | shared
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### data_snapshots

```sql
CREATE TABLE data_snapshots (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    datasource_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    query_hash TEXT NOT NULL,
    query_text TEXT,
    semantic_model_id UUID,
    tables_used TEXT[] DEFAULT '{}',
    columns_used JSONB DEFAULT '{}',
    row_count INT,
    result_json JSONB,
    result_checksum TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 9. Inline Preview API Contract

Frontend must never read raw file paths.

Required APIs:

```text
GET  /api/v1/artifacts/{artifact_id}
GET  /api/v1/artifacts/{artifact_id}/preview
GET  /api/v1/artifacts/{artifact_id}/thumbnail?page=1
GET  /api/v1/artifacts/{artifact_id}/download
POST /api/v1/artifacts/{artifact_id}/regenerate
POST /api/v1/artifacts/{artifact_id}/approve
POST /api/v1/artifacts/{artifact_id}/publish
POST /api/v1/artifacts/{artifact_id}/schedule-refresh
```

Preview endpoint behavior:

```text
1. Authenticate user.
2. Resolve org_id and app_id.
3. Check conversation/app access.
4. Check artifact visibility.
5. Load current artifact version.
6. Select best preview derivative.
7. Stream preview from PostgreSQL or MinIO.
8. Log artifact_interaction = preview_opened.
```

Artifact metadata response:

```json
{
  "artifact_id": "artifact_uuid",
  "artifact_type": "pptx",
  "title": "Q2 Finance Report",
  "status": "preview_ready",
  "current_version": 1,
  "created_by_agent": "Finance Agent",
  "created_by_skill": "Company Finance PPT Skill",
  "source_refs": {
    "data_snapshots": ["snapshot_1", "snapshot_2"],
    "template_artifact_id": "template_1"
  },
  "actions": ["preview", "edit", "regenerate", "download", "approve"]
}
```

---

## 10. Frontend Inline Preview UX

The chat UI should contain:

```text
AssistantMessage
  ├── Assistant text
  ├── Live Execution Timeline, collapsible
  └── Artifact Preview Card
```

Artifact card example:

```text
Q2 Finance Report.pptx
Preview ready · Version v1
Created by Finance Agent
Sources: 2 DataSnapshots, 1 template

Preview · Edit · Regenerate · Compare · Download · Approve · Publish
```

Recommended layout:

```text
Left: project/task sidebar
Center: chat + live execution timeline + artifact cards
Right: preview/code panel
```

Preview UI by type:

```text
PPTX:
  slide thumbnails + PDF slide preview

DOCX:
  PDF page preview + document outline

MD:
  rendered preview + raw markdown tab

HTML:
  iframe preview + source/code tab

Dashboard:
  interactive cards/charts + refresh status

Mini app:
  sandboxed app preview + code tab + publish button
```

---

## 11. Event Stream for Preview Readiness

Artifact preview should be event-driven.

Important events:

```text
artifact.created
artifact.preview_started
artifact.preview_ready
artifact.preview_failed
artifact.validation_started
artifact.validation_passed
artifact.validation_failed
artifact.version_created
artifact.downloaded
artifact.approved
artifact.published
```

Example:

```json
{
  "event_type": "artifact.preview_ready",
  "org_id": "org_uuid",
  "app_id": "app_uuid",
  "conversation_id": "conversation_uuid",
  "execution_id": "execution_uuid",
  "message_id": "assistant_message_uuid",
  "artifact": {
    "artifact_id": "artifact_uuid",
    "artifact_version_id": "version_uuid",
    "artifact_type": "pptx",
    "title": "Q2 Finance Report",
    "preview_kind": "pdf",
    "actions": ["preview", "edit", "regenerate", "download", "approve"]
  }
}
```

Frontend behavior:

```text
Receive artifact.preview_ready
→ update message artifact list
→ render ArtifactPreviewCard
→ user clicks Preview
→ call preview API
→ open inline preview panel
```

---

## 12. Sandbox and User Data Rules

The sandbox is temporary execution, not storage.

Correct pattern:

```text
Agent has datasource binding
→ backend creates DataSnapshot through governed query
→ sandbox receives DataSnapshot
→ skill generates artifact
→ backend stores artifact
```

Forbidden pattern:

```text
Sandbox receives production database credentials.
Sandbox connects freely to enterprise database.
Sandbox reads arbitrary tables.
Sandbox stores final output only in /tmp.
Frontend reads raw sandbox file path.
```

Sandbox input package should contain only:

```text
input.json
approved DataSnapshot JSON/CSV
approved template artifact
approved skill package
approved chart assets
brand rules
build instructions
```

Sandbox output package may contain:

```text
original file
preview file
thumbnails
source_json
build_manifest
validation_report
logs
```

After output is stored:

```text
sandbox container is removed
temporary files are deleted
PostgreSQL/MinIO remains authoritative
```

---

## 13. Security Best Practices

### Permission Checks

Every artifact preview request must verify:

```text
same org
same app
conversation access
artifact visibility
user role
approval state if required
```

### No Raw Paths

Bad:

```text
https://server.com/tmp/job_123/output/report.pptx
```

Good:

```text
GET /api/v1/artifacts/{artifact_id}/preview
```

### Validation Before Trust

Generated artifacts are not trusted until validation passes.

Validation checks:

```text
file opens successfully
MIME type matches artifact type
checksum computed
file size within limit
preview generated successfully
no unsafe macros
no suspicious embedded scripts
all data-driven claims link to DataSnapshots
source refs are present
template rules are followed
```

### HTML and Mini App Preview Security

HTML/mini apps must run in sandboxed iframe.

Example:

```html
<iframe
  sandbox="allow-scripts"
  src="/api/v1/artifacts/{artifact_id}/preview-html">
</iframe>
```

Avoid by default:

```text
allow-same-origin
allow-popups
allow-forms
unrestricted network
credential access
```

### Redaction

Never show in chat timeline or preview APIs:

```text
API keys
database passwords
raw credentials
internal environment variables
Docker socket paths
private system prompts
unsafe raw stack traces
full internal host paths
```

---

## 14. Scalability Best Practices

### Store Preview Derivatives

Do not convert files every time the user previews.

Store:

```text
original PPTX
preview PDF
slide thumbnails
artifact metadata
source parts JSON
```

### Use Async Preview Jobs

Preview generation should be queued and executed by a worker.

```text
artifact created
→ preview job queued
→ preview worker converts
→ preview stored
→ preview_ready event emitted
```

### Cache Lightweight Metadata

Redis can cache:

```text
artifact preview status
thumbnail list
short-lived signed URL info
execution progress fanout
```

But PostgreSQL remains source of truth.

### Use MinIO for Large Production Files

Production storage rule:

```text
PostgreSQL stores metadata, permissions, lineage, checksums, and versions.
MinIO stores large binary files and preview derivatives.
Redis stores only temporary queue/cache/events.
```

### Support Versioning

Every edit or regeneration creates a new version.

```text
v1: original generated PPT
v2: slide 3 simplified
v3: translated Chinese version
v4: approved version
```

Users should be able to compare versions.

---

## 15. Dashboard and Automatic Refresh

Dashboard artifacts are data-bound.

Store:

```text
dashboard_definition JSON
DataSnapshot refs
refresh policy
last refresh status
next run time
artifact version history
```

Auto-refresh flow:

```text
scheduled task triggers
→ datasource gateway creates new DataSnapshot
→ dashboard renderer creates new version
→ validation runs
→ preview updates
→ user notified if needed
```

Dashboard update modes:

```text
manual_refresh
scheduled_refresh
event_driven_refresh
condition_based_refresh
```

Rule:

```text
Dashboard auto-update must use approved datasource binding and validated DataSnapshots.
```

---

## 16. Mini App and Web App Preview

Mini apps are generated interactive artifacts.

Store:

```text
mini_app_definition
source bundle or build bundle
permissions manifest
input/output schema
data bindings
runtime config
preview status
published status
validation report
```

Preview:

```text
sandboxed iframe
restricted permissions
no enterprise data unless approved binding exists
publish requires approval
```

Mini app lifecycle:

```text
draft
building
preview_ready
editing
validated
approved
published
archived
```

---

## 17. MVP vs Production Recommendation

### MVP

```text
PostgreSQL stores artifact metadata and BYTEA blobs.
Redis handles queue and live events.
Docker sandbox generates files.
PPTX/DOCX convert to PDF preview.
MD renders to HTML.
HTML previews in sandboxed iframe.
Dashboard uses JSON-driven React renderer.
```

### Production

```text
PostgreSQL stores metadata, versions, permissions, lineage, checksums, audit.
MinIO stores large binaries and preview derivatives.
Redis handles queues, locks, fanout, and short cache.
Preview workers generate derivatives asynchronously.
Signed URLs may be used for large downloads after permission check.
Sandbox isolation is strengthened with rootless Docker, gVisor, Kata, or managed sandbox.
```

---

## 18. Acceptance Tests for Claude Code

Claude Code should satisfy these tests:

```text
1. User can ask chat to generate a Markdown artifact.
2. Artifact is stored in PostgreSQL.
3. Assistant message links to artifact through message_artifacts.
4. User can preview Markdown inline.
5. User can ask chat to generate an HTML artifact.
6. HTML preview opens in sandboxed iframe.
7. User can ask chat to generate PPTX.
8. PPTX is stored as artifact version.
9. PDF preview is generated and stored.
10. Preview API checks user permission.
11. User cannot preview another user's private artifact.
12. Redis restart does not delete artifacts.
13. Sandbox temp files are deleted after run.
14. Artifact preview still works after browser refresh.
15. Artifact interaction is logged when preview opens.
16. Regeneration creates a new artifact version.
17. Data-driven artifacts link to DataSnapshots.
18. No raw file path is exposed to frontend.
19. No secrets are shown in timeline or preview.
20. Large-file mode can switch from PostgreSQL BYTEA to MinIO object refs without changing frontend APIs.
```

---

## 19. Final Architecture Rule

Use this as the official rule for Zhanlu:

**Generated files in Zhanlu are governed Artifacts, not ordinary chat attachments. The AI agent creates artifacts through sandboxed skills, the backend stores metadata, versions, source references, validation reports, and preview derivatives, and the frontend displays them inline through permission-checked preview APIs. PostgreSQL remains the source of truth for metadata, permissions, lineage, and audit; Redis is temporary infrastructure for queues, locks, and event fanout; MinIO or S3-compatible storage is used for scalable binary storage when files become large. The sandbox receives only approved input packages and never raw database credentials. Every artifact is versioned, validated, previewed, and linked back to the chat, execution, agent, skill, and DataSnapshot that produced it.**
