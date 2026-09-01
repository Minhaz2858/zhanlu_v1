# Zhanlu Database Schema v1

## Principle

PostgreSQL is the source of truth. Redis is temporary infrastructure only.

This file defines the MVP schema families. Claude Code should implement this using Alembic migrations or the existing migration system.

## Required PostgreSQL extensions

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- Optional if available:
-- CREATE EXTENSION IF NOT EXISTS vector;
```

## Identity and workspace tables

```sql
CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    email TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    password_hash TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, email)
);

CREATE TABLE apps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    description TEXT,
    visibility TEXT NOT NULL DEFAULT 'private',
    status TEXT NOT NULL DEFAULT 'active',
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    user_id UUID REFERENCES users(id),
    role TEXT NOT NULL DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Conversation tables

```sql
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    user_id UUID NOT NULL REFERENCES users(id),
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    user_id UUID REFERENCES users(id),
    role TEXT NOT NULL,
    content TEXT,
    content_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Request and execution tables

```sql
CREATE TABLE request_envelopes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    user_id UUID NOT NULL REFERENCES users(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    message_id UUID REFERENCES messages(id),
    channel TEXT NOT NULL DEFAULT 'web',
    capability_snapshot JSONB NOT NULL DEFAULT '{}',
    preference_snapshot JSONB NOT NULL DEFAULT '{}',
    payload TEXT NOT NULL,
    selected_dataset_ids UUID[] NOT NULL DEFAULT '{}',
    selected_artifact_ids UUID[] NOT NULL DEFAULT '{}',
    attachment_ids UUID[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'sealed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    user_id UUID NOT NULL REFERENCES users(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    envelope_id UUID REFERENCES request_envelopes(id),
    assistant_message_id UUID REFERENCES messages(id),
    task_kind TEXT NOT NULL DEFAULT 'normal_chat',
    task_spec JSONB NOT NULL DEFAULT '{}',
    fsm_state TEXT NOT NULL DEFAULT 'init',
    status TEXT NOT NULL DEFAULT 'running',
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE execution_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    conversation_id UUID REFERENCES conversations(id),
    execution_id UUID REFERENCES executions(id),
    node_run_id UUID,
    event_type TEXT NOT NULL,
    event_payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE plan_node_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    execution_id UUID NOT NULL REFERENCES executions(id),
    node_key TEXT NOT NULL,
    node_type TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    input_json JSONB NOT NULL DEFAULT '{}',
    output_json JSONB NOT NULL DEFAULT '{}',
    error_code TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
```

## Agent and skill tables

```sql
CREATE TABLE agent_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID REFERENCES apps(id),
    agent_key TEXT NOT NULL,
    name TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, app_id, agent_key)
);

CREATE TABLE agent_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    version INT NOT NULL,
    manifest JSONB NOT NULL DEFAULT '{}',
    prompt_pack JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(agent_profile_id, version)
);

CREATE TABLE skill_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID REFERENCES apps(id),
    skill_key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, app_id, skill_key)
);

CREATE TABLE skill_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    skill_profile_id UUID NOT NULL REFERENCES skill_profiles(id),
    version TEXT NOT NULL,
    manifest JSONB NOT NULL DEFAULT '{}',
    skill_md TEXT,
    package_blob BYTEA,
    checksum TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_skill_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID REFERENCES apps(id),
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    skill_profile_id UUID NOT NULL REFERENCES skill_profiles(id),
    policy_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Artifact tables

Use the artifact tables from `Zhanlu_Artifact_Preview_Implementation_Spec.md`.

Add:

```sql
CREATE TABLE artifact_interactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    user_id UUID NOT NULL REFERENCES users(id),
    conversation_id UUID REFERENCES conversations(id),
    artifact_id UUID NOT NULL,
    artifact_version_id UUID,
    action TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Datasource and snapshot tables

```sql
CREATE TABLE datasources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID REFERENCES apps(id),
    name TEXT NOT NULL,
    datasource_type TEXT NOT NULL,
    config_json JSONB NOT NULL DEFAULT '{}',
    credential_ref TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_data_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID REFERENCES apps(id),
    agent_profile_id UUID NOT NULL REFERENCES agent_profiles(id),
    datasource_id UUID NOT NULL REFERENCES datasources(id),
    access_mode TEXT NOT NULL DEFAULT 'read_only',
    allowed_tables TEXT[] NOT NULL DEFAULT '{}',
    blocked_tables TEXT[] NOT NULL DEFAULT '{}',
    policy_json JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE data_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID NOT NULL REFERENCES apps(id),
    datasource_id UUID REFERENCES datasources(id),
    execution_id UUID REFERENCES executions(id),
    query_hash TEXT,
    query_text TEXT,
    semantic_model_id UUID,
    tables_used TEXT[] NOT NULL DEFAULT '{}',
    columns_used JSONB NOT NULL DEFAULT '{}',
    row_count INT,
    result_json JSONB,
    result_checksum TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Sandbox tables

Use the sandbox tables from `Zhanlu_Sandbox_Runtime_Implementation_Spec.md`.

## Audit table

```sql
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES organizations(id),
    app_id UUID REFERENCES apps(id),
    user_id UUID REFERENCES users(id),
    execution_id UUID REFERENCES executions(id),
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Indexes

Create indexes for:

```sql
CREATE INDEX idx_messages_conversation_created ON messages(conversation_id, created_at);
CREATE INDEX idx_execution_events_execution_created ON execution_events(execution_id, created_at);
CREATE INDEX idx_artifacts_conversation ON artifacts(conversation_id, created_at);
CREATE INDEX idx_artifact_blobs_version ON artifact_blobs(artifact_version_id);
CREATE INDEX idx_data_snapshots_execution ON data_snapshots(execution_id);
CREATE INDEX idx_audit_logs_org_created ON audit_logs(org_id, created_at);
```

## Tenant isolation

Every tenant/app-scoped table must include `org_id`. App-scoped records should include `app_id`.

Do not rely only on frontend filtering. Backend must filter by org/app/user permissions.
