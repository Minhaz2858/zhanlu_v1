<div align="center">

# Zhanlu (展鹭) — Enterprise AI Operating System

**A governed, multi-tenant AI agent platform: chat that queries your real databases, executes code in disposable sandboxes, and ships verifiable deliverables — with a receipt for every step.**

`FastAPI` · `React 18 + Vite` · `PostgreSQL + pgvector` · `Redis` · `MinIO` · `Docker sandboxes` · `SSE streaming`

</div>

---

> Enterprises can't treat an LLM chatbot as production software. Zhanlu runs every
> agent as a **versioned harness** — layered constitutional prompts, iteration
> budgets, grounding gates, tenant-isolated data access, and audit events — so
> answers are **verifiable, sandboxed, and auditable** rather than merely conversational.

## What it does

| Capability | How it works |
|---|---|
| 🤖 **Governed agent runtime** | A main agent that plans work, delegates to specialized sub-agents, and streams live activity (plan steps, tool progress) over SSE. Every tool call passes a registry with risk tiers + approval gates. |
| 🗄️ **Policy-enforced NL2SQL** | Questions become SQL only through a gateway: schema grounding, source-verification gates, row caps, read-only boundaries, and tenant-scoped data sources — never free-form queries against your ERP. |
| 📦 **Real deliverables, not chat text** | Reports ship as PPTX / DOCX / XLSX / PDF / HTML dashboards and mini-apps, generated in disposable Docker sandboxes and stored as permission-checked artifacts. |
| 🔁 **n8n-parity workflow engine** | Visual workflow canvas (build-with-AI), ~18 node types, topological execution, cron scheduling with exactly-once occurrence claims across processes. |
| ⏱️ **Durable automation runtime** | Scheduled tasks with fenced state-machine writes (lease tokens), persisted retries, crash recovery, and durable execution-event ledgers — see [Automation Architecture](docs/architecture). |
| 👥 **Multi-tenant by construction** | `org_id` / `app_id` scoping on every entity, project-scoped knowledge bases, RBAC, and admin-gated capabilities. |
| 🧠 **Memory & context engineering** | Layered context compaction, pgvector semantic memory, and conversation summarization — long sessions stay inside budget without losing the plot. |

## Architecture

```mermaid
flowchart TB
    subgraph Client["Frontend — React 18 + Vite (nginx :8089)"]
        UI1["Chat — SSE live activity stream"]
        UI2["Workflow Canvas — build with AI"]
        UI3["Agent Studio · Automation Center<br/>Dashboards · Marketplace"]
    end

    subgraph API["FastAPI Backend (:5002) — 40+ routers under /api"]
        AUTH["Auth — JWT + rotating refresh,<br/>OTP registration, rate limits"]
        AGENTS["Agent chat loop — tool registry,<br/>guardrails, delegation"]
        WF["Workflow runtime +<br/>automation dispatcher"]
        ART["Artifact pipeline —<br/>PPTX/DOCX/XLSX/PDF/HTML"]
    end

    subgraph Data[("Data plane")]
        PG[("PostgreSQL<br/>+ pgvector")]
        RD[("Redis")]
        S3[("MinIO<br/>artifacts & files")]
        CH[("ChromaDB<br/>vectors")]
    end

    subgraph Sand["Execution plane"]
        SBX["Disposable Docker sandboxes<br/>(python / office / pptx / webapp)"]
    end

    Client -->|SSE / REST| API
    API --> PG & RD & S3
    API --> SBX
    AGENTS -->|"NL2SQL gateway<br/>(grounded, read-only, row-capped)"| PG
```

## Quickstart

```bash
# Full stack (builds sandbox images first, then starts everything)
make up

# Services
#   UI       → http://localhost:8089
#   API      → http://localhost:5002/healthz
#   Postgres → localhost:5400   Redis → localhost:6300   MinIO → localhost:9400

# Other commands
make logs      # tail all service logs
make smoke     # API E2E smoke test suite
make db-info   # which database the backend is actually connected to
make reset     # full teardown + rebuild
```

First visit registers the initial admin (first user bypasses email OTP); every
subsequent account verifies via emailed OTP. Configuration templates live in
[`backend/.env.example`](backend/.env.example) and [`.env.example`](.env.example).

## Project structure

```
├── backend/            # FastAPI app (40+ routers, ~139-tool registry, sandbox worker)
│   ├── app/routers/    #   HTTP layer
│   ├── app/services/   #   Agent runtime, NL2SQL gateway, workflows, artifacts
│   ├── alembic/        #   130+ migrations
│   └── tests/          #   Large pytest suite (unit + PG acceptance)
├── frontend/           # React 18 + Vite (chat, workflow canvas, studio, admin)
├── deploy/             # nginx + deployment configs
├── infra/              # Postgres init, local infra
├── scripts/            # DB setup / seeding / maintenance scripts
├── docs/               # Architecture, audits, plans, reports
├── Makefile            # make up / down / logs / smoke / db-*
└── docker-compose.yml  # backend · frontend · postgres · redis · minio · sandboxes
```

## Testing

```bash
cd backend && python -m pytest tests/ -k automation   # backend suites
cd frontend && npm test                               # frontend (vitest)
make smoke                                            # end-to-end API smoke
```

The backend distinguishes **implemented ≠ tested ≠ production-ready** — suites
cover unit, integration, and real-PostgreSQL acceptance scenarios (fencing,
crash recovery, multi-process exactly-once scheduling).

## Documentation

| Doc | Contents |
|---|---|
| [`ZHANLU_PROJECT_OVERVIEW.md`](ZHANLU_PROJECT_OVERVIEW.md) | Traceable technical overview — architecture, subsystems, every claim sourced to a file |
| [`PROJECT.md`](PROJECT.md) | Full deep-dive: agent runtime, workflow engine, infrastructure, domain subsystems |
| [`docs/AUTHENTICATION.md`](docs/AUTHENTICATION.md) | Auth & security model: tokens, rotation, revocation, rate limits, configuration table |
| [`docs/architecture/`](docs/architecture) | Automation target architecture & decision records |
| [`docs/audits/`](docs/audits) | Security & consistency audits |
| [`docs/reports/`](docs/reports) | Implementation reports and progress records |

## Security model (summary)

- **No anonymous access** — every page and every mutating/LLM/data endpoint requires auth.
- Short-lived access JWT (15 min) + long-lived rotating refresh token (30 days,
  SHA-256-hashed at rest); logout blacklists the token family.
- Password policy (length + complexity) and per-IP rate limits on auth endpoints.
- Tool calls pass a **risk-tiered registry with approval gates**; database access
  is read-only by default with row caps and tenant scoping.

Full details: [`docs/AUTHENTICATION.md`](docs/AUTHENTICATION.md).

---

<div align="center">
<sub>Built by SYNEXIA — Zhanlu (展鹭) & Synexia™ are working names of the platform and its agent core.</sub>
</div>
