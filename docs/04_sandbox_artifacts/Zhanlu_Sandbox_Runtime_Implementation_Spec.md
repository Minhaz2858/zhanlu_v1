# Zhanlu Sandbox Runtime Implementation Spec

## Purpose

The sandbox runtime gives Zhanlu a Claude-Code-like live execution experience while protecting the host, database, files, credentials, and enterprise data.

## Required design

```text
User chat → Synexia plan → Layer 5 execution → sandbox job → temporary Docker container → output → validation → PostgreSQL artifact storage → inline preview
```

## Service responsibilities

### backend

- stores conversations/messages/executions/artifacts,
- creates sandbox_jobs rows,
- enqueues job_id into Redis,
- serves preview/download APIs,
- never accesses Docker socket.

### worker

- executes normal non-sandbox workflow nodes,
- may enqueue sandbox jobs,
- never accesses Docker socket.

### sandbox-worker

- the only service allowed to create temporary sandbox containers,
- reads sandbox job from Redis,
- loads job metadata from PostgreSQL,
- materializes input package,
- runs container with limits,
- streams stdout/stderr events,
- collects outputs,
- validates outputs,
- stores outputs back through backend/service functions,
- deletes temp files and containers.

## Docker socket rule

MVP may mount Docker socket only into `zhanlu-sandbox-worker`.

```yaml
sandbox-worker:
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - sandbox_tmp:/tmp/zhanlu_sandbox
```

No other service may mount Docker socket.

Future hardening:

- rootless Docker,
- Docker socket proxy,
- separate sandbox host,
- gVisor,
- Kata Containers,
- Firecracker/microVM,
- managed sandbox provider.

## Sandbox container default command pattern

```bash
docker run --rm \
  --name zhanlu-sandbox-job-${JOB_ID} \
  --network none \
  --memory 1g \
  --cpus 1 \
  --pids-limit 128 \
  --read-only \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  --user 1000:1000 \
  -v /tmp/zhanlu_sandbox/${JOB_ID}/input:/workspace/input:ro \
  -v /tmp/zhanlu_sandbox/${JOB_ID}/output:/workspace/output:rw \
  zhanlu-sandbox-pptx:latest
```

## Default sandbox limits

```text
network: none
cpu: 1
memory: 1024 MB
timeout: 120 seconds
pids_limit: 128
root_fs: read-only
user: non-root
capabilities: drop all
input: read-only
output: writable
host filesystem: not mounted
secrets: not mounted
Docker socket inside sandbox container: never
```

## Input package

Sandbox receives only approved inputs.

Example:

```text
/workspace/input/input.json
/workspace/input/data_snapshots/*.json
/workspace/input/templates/company_template.pptx
/workspace/input/charts/*.png
/workspace/input/skill_package/
```

Sandbox must not receive:

- raw database password,
- model provider API key,
- SSH key,
- full enterprise data dump,
- host filesystem path,
- unrestricted internet access,
- Docker socket.

## Output package

Sandbox writes to:

```text
/workspace/output/
```

Possible outputs:

```text
artifact.pptx
preview.pdf
thumbnail_001.png
thumbnail_002.png
artifact.html
artifact.md
build_manifest.json
validation_report.json
stdout.log
stderr.log
```

The sandbox-worker copies outputs into PostgreSQL artifact tables and deletes temp files.

## Database tables

Minimum tables:

```sql
CREATE TABLE sandbox_jobs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL,
    app_id UUID NOT NULL,
    execution_id UUID NOT NULL,
    artifact_build_job_id UUID,
    skill_version_id UUID,
    runtime_image TEXT NOT NULL,
    status TEXT NOT NULL,
    network_policy TEXT NOT NULL DEFAULT 'none',
    cpu_limit TEXT NOT NULL DEFAULT '1',
    memory_limit_mb INT NOT NULL DEFAULT 1024,
    timeout_seconds INT NOT NULL DEFAULT 120,
    input_manifest JSONB NOT NULL DEFAULT '{}',
    output_manifest JSONB NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE sandbox_commands (
    id UUID PRIMARY KEY,
    sandbox_job_id UUID NOT NULL,
    command_display TEXT NOT NULL,
    exit_code INT,
    stdout TEXT,
    stderr TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE sandbox_job_events (
    id UUID PRIMARY KEY,
    sandbox_job_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    event_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Runtime images

Prebuild controlled images:

```text
zhanlu-sandbox-python
zhanlu-sandbox-node
zhanlu-sandbox-pptx
zhanlu-sandbox-docx
zhanlu-sandbox-office
zhanlu-sandbox-webapp
zhanlu-sandbox-dashboard
```

Avoid installing arbitrary packages during production execution. Use approved dependencies or reviewed skill images.

## Recovery after restart

On startup, sandbox-worker must:

1. scan PostgreSQL for `sandbox_jobs` with `running` or `queued` status,
2. check whether container still exists,
3. mark orphaned jobs as `failed` or `retryable`,
4. clean stale temp folders,
5. requeue safe retryable jobs according to policy.

## Security policy

Default sandbox mode is strict.

Allowed automatically:

- MD generation,
- static HTML generation,
- PPT/DOCX generation from approved snapshots/templates,
- chart rendering,
- PDF/thumbnail conversion.

Requires approval:

- network access,
- installing new dependencies,
- custom code skill,
- external API call,
- large data export,
- publishing externally.

Always blocked:

- host filesystem access,
- Docker socket access inside sandbox,
- privileged container,
- arbitrary internet,
- raw credential access,
- production database write,
- unbounded runtime,
- deleting persistent data.
