---
name: content-creation-publisher
description: Use when publishing content from cloud repositories, synchronizing skills or content from remote storage (GitHub, S3, GCS, OSS, Notion), setting up environment configuration for content distribution, or running runtime tests before automated content deployment. Triggers on "publish content", "sync skills from remote", "content distribution workflow", "deploy content pipeline".
---

# Content Creation Publisher

Publish content from cloud repositories: synchronize skills and content from remote storage with environment configuration and runtime testing for automated content distribution workflows.

## When to use

- "Publish this content to our repo / site / store"
- "Sync skills from remote storage"
- Setting up environment configuration for content distribution
- Running runtime tests before automated content deployment
- "Content distribution workflow", "publish pipeline", "deploy content"

## Workflow

1. **Identify sources** — where does the content live? (GitHub repo, S3/GCS/OSS bucket, Notion, local dir). Resolve credentials/config for the source.
2. **Synchronize** — pull the latest content/skills from the remote source into the target workspace:
   - GitHub: shallow clone or raw fetch (codeload tarball is fastest for large repos)
   - Object storage: download via the provider CLI (aws s3 sync, gsutil rsync, ossutil)
   - Keep a manifest of what was synced (source, revision, timestamp)
3. **Environment configuration** — ensure runtime prerequisites exist: env vars, API keys, PATH entries, dependency installs. Verify each with a concrete check, not an assumption.
4. **Runtime tests** — before publishing, smoke-test the content: validate SKILL.md frontmatter (name/description present, parseable YAML), required files exist, referenced scripts run, no broken paths.
5. **Publish** — push to the target (repo commit/PR, store, site). Record what was published and the resulting URL/artifact.
6. **Verify** — confirm the published artifact is reachable and correct (HTTP 200, expected content hash).

## Rules

- Never publish without a runtime test pass — a broken publish is worse than a delayed one
- Keep environment configuration explicit: list every required variable and its source
- Record a manifest (source → revision → target) so the publish is auditable
- When syncing skills, preserve the original frontmatter names or document renames explicitly

## Output

A publish summary: what was synced, what was tested, what was published, and verification evidence (URLs, hashes, status).
