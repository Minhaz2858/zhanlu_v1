# Zhanlu Seed Data Spec

## Purpose

Seed data makes the first development build usable immediately.

## Seed organization

```json
{
  "name": "Demo Organization",
  "slug": "demo-org"
}
```

## Seed admin user

```json
{
  "email": "admin@zhanlu.local",
  "password": "admin123456",
  "display_name": "Admin",
  "role": "admin"
}
```

## Seed default app

```json
{
  "name": "Demo Workspace",
  "description": "Default workspace for testing Zhanlu",
  "visibility": "app_shared"
}
```

## Seed agents

Seed these from `starter_templates/agent_library/system/`:

- finance-agent
- report-agent
- data-analyst-agent
- dashboard-agent
- document-agent
- mini-app-agent
- review-agent
- compliance-agent

## Seed skills

Seed these from `starter_templates/skill_library/system/`:

- markdown-generation
- html-generation
- pptx-generation
- docx-generation
- dashboard-generation
- mini-app-generation
- chart-generation
- artifact-validation
- governed-nl2sql
- data-snapshot

## Default bindings

Finance Agent:

- governed-nl2sql
- data-snapshot
- chart-generation
- pptx-generation
- dashboard-generation
- artifact-validation

Report Agent:

- markdown-generation
- html-generation
- pptx-generation
- docx-generation
- artifact-validation

Dashboard Agent:

- dashboard-generation
- chart-generation
- data-snapshot

Mini App Agent:

- html-generation
- mini-app-generation
- artifact-validation

Review Agent:

- artifact-validation

## Seed script

Create:

```text
scripts/seed_dev.py
```

It should be idempotent. Running it twice should not duplicate seed records.

## Success check

After seed:

- admin can log in,
- one app exists,
- agents are visible,
- skills are visible,
- a new chat can be created,
- a Markdown artifact can be generated.
