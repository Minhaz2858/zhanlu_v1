# Zhanlu API Contract for Existing UI

## Base URL

```text
Backend: http://localhost:5002
Production: https://<domain>/api
```

All API responses should use JSON except file/preview streaming endpoints.

## Auth

### POST `/api/v1/auth/login`

Request:

```json
{
  "email": "admin@example.com",
  "password": "password"
}
```

Response:

```json
{
  "access_token": "jwt",
  "token_type": "bearer",
  "user": {
    "id": "uuid",
    "email": "admin@example.com",
    "display_name": "Admin",
    "role": "admin"
  }
}
```

### GET `/api/v1/auth/me`

Response:

```json
{
  "id": "uuid",
  "org_id": "uuid",
  "email": "admin@example.com",
  "display_name": "Admin",
  "role": "admin"
}
```

## Apps

### GET `/api/v1/apps`

Returns apps the user can access.

```json
{
  "items": [
    {
      "id": "uuid",
      "name": "Finance Workspace",
      "description": "Finance analysis and reporting",
      "visibility": "app_shared",
      "created_at": "2026-07-08T00:00:00Z"
    }
  ]
}
```

### POST `/api/v1/apps`

```json
{
  "name": "New Workspace",
  "description": "Optional"
}
```

## Conversations

### GET `/api/v1/apps/{app_id}/conversations`

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "app_id": "uuid",
      "title": "Q2 Finance Report",
      "status": "active",
      "updated_at": "2026-07-08T00:00:00Z"
    }
  ]
}
```

### POST `/api/v1/apps/{app_id}/conversations`

Request:

```json
{
  "title": "New Chat"
}
```

Response:

```json
{
  "id": "uuid",
  "app_id": "uuid",
  "title": "New Chat",
  "status": "active"
}
```

### GET `/api/v1/conversations/{conversation_id}/messages`

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "role": "user",
      "content": "Make a PPT for me",
      "created_at": "2026-07-08T00:00:00Z",
      "artifacts": []
    },
    {
      "id": "uuid",
      "role": "assistant",
      "content": "Your PPT is ready.",
      "created_at": "2026-07-08T00:00:00Z",
      "artifacts": [
        {
          "artifact_id": "uuid",
          "artifact_version_id": "uuid",
          "artifact_type": "pptx",
          "title": "Generated Presentation",
          "status": "ready",
          "preview_available": true
        }
      ]
    }
  ]
}
```

## Chat streaming

### POST `/api/v1/chat/stream`

Request:

```json
{
  "app_id": "uuid",
  "conversation_id": "uuid",
  "message": "Make a 10-slide PPT about Q2 sales performance.",
  "selected_agent_id": "uuid-or-null",
  "selected_artifact_ids": [],
  "selected_dataset_ids": [],
  "attachment_ids": []
}
```

Response should be `text/event-stream` by default.

Required event types are defined in `Zhanlu_Event_Stream_Contract.md`.

## Executions

### GET `/api/v1/executions/{execution_id}`

Response:

```json
{
  "id": "uuid",
  "conversation_id": "uuid",
  "status": "running",
  "task_kind": "pptx_artifact",
  "created_at": "...",
  "updated_at": "..."
}
```

### GET `/api/v1/executions/{execution_id}/events`

Returns stored event timeline.

## Artifacts

### GET `/api/v1/artifacts/{artifact_id}`

Response:

```json
{
  "id": "uuid",
  "artifact_type": "pptx",
  "title": "Q2 Sales Deck",
  "status": "ready",
  "current_version_id": "uuid",
  "visibility": "conversation_private",
  "created_at": "...",
  "sources": {
    "data_snapshot_ids": [],
    "template_artifact_id": null,
    "skill_version_id": "uuid"
  },
  "actions": ["preview", "download", "regenerate", "approve"]
}
```

### GET `/api/v1/artifacts/{artifact_id}/preview`

Returns preview content.

Behavior by type:

- `md`: HTML response or JSON with rendered HTML.
- `html`: iframe-safe HTML response.
- `pptx`: PDF preview if available.
- `docx`: PDF preview if available.
- `dashboard`: dashboard JSON.
- `mini_app`: iframe-safe app preview URL or HTML.

### GET `/api/v1/artifacts/{artifact_id}/thumbnail?page=1`

Returns slide/page thumbnail image if available.

### GET `/api/v1/artifacts/{artifact_id}/download`

Streams original file.

### POST `/api/v1/artifacts/{artifact_id}/regenerate`

Request:

```json
{
  "instruction": "Make slide 3 simpler.",
  "target_part_id": "slide_3"
}
```

Response:

```json
{
  "execution_id": "uuid",
  "status": "queued"
}
```

### POST `/api/v1/artifacts/{artifact_id}/approve`

Request:

```json
{
  "note": "Approved for internal sharing"
}
```

## Agents

### GET `/api/v1/agents`

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "name": "Finance Agent",
      "agent_type": "functional",
      "description": "Finance reporting and analysis",
      "status": "active"
    }
  ]
}
```

### GET `/api/v1/agents/{agent_id}`

Returns manifest, allowed skills, allowed data bindings, memory scope.

## Skills

### GET `/api/v1/skills`

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "name": "PPTX Generation",
      "skill_key": "pptx-generation",
      "artifact_types": ["pptx"],
      "status": "active"
    }
  ]
}
```

## Datasources

### GET `/api/v1/datasources`

Returns datasources available to the app/user.

### POST `/api/v1/datasources/{datasource_id}/test`

Tests connection through backend only.

## MCP

MCP is Phase 2 unless required immediately.

### GET `/api/v1/mcp/servers`

### POST `/api/v1/mcp/servers`

### GET `/api/v1/mcp/servers/{server_id}/tools`

## Admin

### GET `/api/v1/admin/audit-logs`

### GET `/api/v1/admin/platform/health`

### GET `/api/v1/admin/workers`

## Error shape

All errors should use:

```json
{
  "error": {
    "code": "ARTIFACT_NOT_FOUND",
    "message": "Artifact not found or access denied.",
    "details": {}
  }
}
```
