# Zhanlu ENV and Config Guide

## Required `.env.example`

```env
# App
APP_ENV=development
APP_NAME=Zhanlu
PUBLIC_BASE_URL=http://localhost:5152

# Ports
FRONTEND_PORT=5152
BACKEND_PORT=5002
SYNEXIA_PORT=8643

# Database
DATABASE_URL=postgresql+psycopg://zhanlu:zhanlu_password@postgres:5432/zhanlu
POSTGRES_DB=zhanlu
POSTGRES_USER=zhanlu
POSTGRES_PASSWORD=zhanlu_password

# Redis
REDIS_URL=redis://redis:6379/0

# Auth
JWT_SECRET=change_me_in_production
JWT_EXPIRES_MINUTES=10080
DEV_AUTH_ENABLED=true

# Model provider, only used by Synexia BrainClient
MODEL_PROVIDER=openai
MODEL_NAME=gpt-4.1-mini
MODEL_API_KEY=replace_me
MODEL_BASE_URL=

# Synexia
SYNEXIA_BASE_URL=http://synexia:8643
SYNEXIA_ENABLE_MODEL_CALLS=true
SYNEXIA_MAX_STEPS=20

# Sandbox
SANDBOX_ENABLED=true
SANDBOX_TMP_DIR=/tmp/zhanlu_sandbox
SANDBOX_DEFAULT_NETWORK=none
SANDBOX_DEFAULT_TIMEOUT_SECONDS=120
SANDBOX_DEFAULT_MEMORY_MB=1024
SANDBOX_DEFAULT_CPUS=1
SANDBOX_MAX_OUTPUT_MB=100
SANDBOX_ALLOW_DOCKER_SOCKET=false

# Uploads and artifacts
MAX_UPLOAD_MB=50
STORE_ARTIFACT_BLOBS_IN_POSTGRES=true
ARTIFACT_PREVIEW_ENABLED=true

# Preview converters
PPTX_PREVIEW_ENABLED=true
DOCX_PREVIEW_ENABLED=true
OFFICE_CONVERTER_MODE=local
GOTENBERG_URL=

# Security
CORS_ORIGINS=http://localhost:5152
ENABLE_AUDIT_LOG=true
ALLOW_PUBLIC_SIGNUP=false

# Workers
WORKER_CONCURRENCY=2
SANDBOX_WORKER_CONCURRENCY=1
```

## Secret rules

- Do not commit real `.env` files.
- Do not expose model API keys to frontend.
- Do not pass model API keys into sandbox.
- Do not pass database credentials into sandbox.
- Use credential references for datasources.

## Docker Compose environment

Docker Compose should read `.env` and pass only required variables to each service.

Backend needs:

- DATABASE_URL
- REDIS_URL
- JWT_SECRET
- SYNEXIA_BASE_URL

Synexia needs:

- DATABASE_URL if storing directly, or backend API URL if not
- MODEL_PROVIDER
- MODEL_API_KEY
- MODEL_NAME

Sandbox-worker needs:

- DATABASE_URL
- REDIS_URL
- SANDBOX_TMP_DIR
- sandbox limits
- Docker socket access in MVP only

Frontend needs:

- VITE_API_BASE_URL
- VITE_WS_OR_SSE_URL if needed

## Production changes

Production must change:

- JWT_SECRET,
- POSTGRES_PASSWORD,
- MODEL_API_KEY,
- CORS_ORIGINS,
- PUBLIC_BASE_URL,
- TLS/Nginx config.

Production should disable:

- DEV_AUTH_ENABLED,
- public signup unless required,
- unrestricted sandbox network.
