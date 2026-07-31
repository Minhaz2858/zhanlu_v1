# Zhanlu Artifact Storage, Chat Data, and Inline Preview Flow

## 1. Core rule

```text
Chat data, execution data, agent data, skill runs, MCP calls, artifact metadata, artifact binaries, previews, and audit records are stored in PostgreSQL.
The backend file system and sandbox file system are temporary only.
```

## 2. What a chat request stores

When a user chats with an agent, Zhanlu stores:

```text
User message
Assistant message
RequestEnvelope
Execution record
PlanDAG
Agent invocation
Skill runs
MCP/tool calls
Datasource queries
DataSnapshots
Generated artifacts
Preview files
Artifact actions
Audit logs
```

One chat request becomes a full traceable execution.

## 3. Main tables

```text
conversations
messages
request_envelopes
executions
execution_events
plans
plan_nodes
agent_invocations
skill_runs
mcp_calls
tool_calls
data_snapshots
artifacts
artifact_versions
artifact_blobs
artifact_previews
message_artifacts
artifact_interactions
audit_logs
```

## 4. Example: user asks “make PPT for me”

```text
1. User sends message in chat.
2. Backend stores the user message.
3. Layer 1 builds RequestEnvelope.
4. Backend stores request_envelope.
5. Synexia starts execution.
6. Synexia creates TaskSpec and PlanDAG.
7. Finance Agent is selected.
8. Finance Agent uses approved finance datasource.
9. SQL/data skill creates DataSnapshot.
10. Chart skill creates chart artifacts.
11. PPT skill runs inside sandbox.
12. Sandbox creates PPTX temporarily.
13. Sandbox converts PPTX to PDF/images for preview.
14. PPTX, PDF preview, thumbnails are saved into PostgreSQL artifact tables.
15. Assistant message is stored with artifact card link.
16. Chat UI receives artifact.preview_ready event.
17. User previews PPT inline in chat.
```

The sandbox may temporarily create files. Permanent PPT data is stored in PostgreSQL.

## 5. Chat tables

```sql
CREATE TABLE conversations (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    user_id UUID NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    user_id UUID,
    role TEXT NOT NULL,
    content TEXT,
    content_json JSONB DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

For artifact responses, the assistant message can say:

```text
Your Q2 Finance PPT is ready. You can preview it below.
```

The actual file is linked through `message_artifacts`.

## 6. Artifact tables

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE artifact_versions (
    id UUID PRIMARY KEY,
    artifact_id UUID NOT NULL REFERENCES artifacts(id),
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    version INT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    build_manifest JSONB NOT NULL DEFAULT '{}',
    validation_report JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE artifact_blobs (
    id UUID PRIMARY KEY,
    artifact_version_id UUID NOT NULL REFERENCES artifact_versions(id),
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
```

For a PPT, store:

```text
original PPTX blob
preview PDF blob
slide thumbnail blobs
optional slide image blobs
```

## 7. Link artifact to chat

```sql
CREATE TABLE message_artifacts (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    message_id UUID NOT NULL REFERENCES messages(id),
    artifact_id UUID NOT NULL REFERENCES artifacts(id),
    artifact_version_id UUID NOT NULL REFERENCES artifact_versions(id),
    display_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The assistant message is text. The artifact preview card comes from `message_artifacts`.

## 8. Inline preview APIs

Frontend never opens raw file paths. It calls permission-checked APIs:

```text
GET /api/v1/artifacts/{artifact_id}
GET /api/v1/artifacts/{artifact_id}/preview
GET /api/v1/artifacts/{artifact_id}/versions
POST /api/v1/artifacts/{artifact_id}/edit
POST /api/v1/artifacts/{artifact_id}/regenerate
POST /api/v1/artifacts/{artifact_id}/approve
GET /api/v1/artifacts/{artifact_id}/download
```

Preview flow:

```text
Frontend receives artifact.preview_ready event
↓
Frontend renders ArtifactPreviewCard
↓
Card calls /artifacts/{id}/preview
↓
Backend checks permission
↓
Backend streams preview PDF or slide thumbnails from PostgreSQL
↓
Frontend shows inline preview
```

## 9. Preview by artifact type

```text
PPTX → PDF preview + slide thumbnails
DOCX → PDF preview + document outline
MD → rendered HTML preview
HTML → sandboxed iframe preview
Dashboard → interactive card/iframe preview
Mini app → sandboxed iframe preview
Chart → inline chart card
```

## 10. Artifact actions

Each artifact card should support:

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

## 11. DataSnapshot links

Data-driven artifacts must link to DataSnapshots.

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

Build manifest example:

```json
{
  "artifact_type": "pptx",
  "created_by_skill": "company_finance_ppt_skill",
  "template_artifact_id": "template_uuid",
  "source_data_snapshots": ["snapshot_uuid_1", "snapshot_uuid_2"],
  "source_charts": ["chart_artifact_uuid_1"],
  "validation_report_id": "validation_uuid"
}
```

## 12. Artifact interactions

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Actions include:

```text
preview_opened
downloaded
approved
rejected
regenerate_requested
published
exported
shared
```

## 13. Final rule

Chat messages store conversation content. Executions store AI process state. DataSnapshots store database-derived evidence. Artifacts store generated files and versions. Message-artifact links attach generated PPT/DOCX/PDF/XLSX/MD/HTML/dashboard/mini app outputs to chat replies. Inline chat preview is served through permission-checked artifact preview APIs, not raw file paths. Sandbox files are temporary only; PostgreSQL is the source of truth for all persistent user, agent, skill, MCP, memory, artifact, and audit data.
