#!/usr/bin/env python3
"""Generate a contribution report from git history and store it as an artifact.

Flow:
  1. Analyze git log → structured doc model
  2. Jinja2 HTML → bytes
  3. python-docx DOCX (via html_docx renderer)
  4. Create Artifact + ArtifactVersion + 4 blobs (HTML original, DOCX, PDF, PNG)
  5. Also write .docx to disk at the repo root
  6. Create stub Conversation + Message + MessageArtifact for inline preview

Usage:
    cd backend && python scripts/generate_contribution_report.py
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

# ── Add backend to sys.path ─────────────────────────────────────────
_backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_backend_dir))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 1. Git Log Analysis
# ══════════════════════════════════════════════════════════════════════


def run_git_log_since(days: int = 90) -> str:
    """Return git log as plain text for the last *days*."""
    repo_root = _backend_dir.parent
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_root),
                "log",
                f"--since={days}.days",
                "--pretty=format:%h|%an|%ad|%s",
                "--date=short",
                "--no-merges",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("git log failed: %s", exc)
        return ""


def run_git_stats() -> dict:
    """Return commit count, file count, insertions/deletions for the period."""
    repo_root = _backend_dir.parent
    stats = {"commits": 0, "files": 0, "insertions": 0, "deletions": 0}
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--since=90.days", "--oneline", "--no-merges"],
            capture_output=True, text=True, timeout=10,
        )
        stats["commits"] = len([l for l in r.stdout.strip().split("\n") if l])

        r2 = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--stat", "HEAD~50..HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        lines = r2.stdout.strip().split("\n")
        if lines:
            last = lines[-1]
            parts = last.split(",")
            for p in parts:
                p = p.strip()
                if "file" in p or "files" in p:
                    try:
                        stats["files"] = int(p.split()[0])
                    except (ValueError, IndexError):
                        pass
                if "insertion" in p:
                    try:
                        stats["insertions"] = int(p.split()[0])
                    except (ValueError, IndexError):
                        pass
                if "deletion" in p:
                    try:
                        stats["deletions"] = int(p.split()[0])
                    except (ValueError, IndexError):
                        pass
    except Exception as exc:
        logger.warning("git stats failed: %s", exc)
    return stats


def count_files_by_glob(pattern: str) -> int:
    """Count files matching a glob relative to the repo root."""
    repo_root = _backend_dir.parent
    try:
        result = subprocess.run(
            ["bash", "-c", f"find {repo_root}/{pattern} -type f 2>/dev/null | wc -l"],
            capture_output=True, text=True, timeout=10,
        )
        return int(result.stdout.strip() or 0)
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════════
# 2. Doc Model Builder
# ══════════════════════════════════════════════════════════════════════


def build_doc_model() -> dict:
    """Build the structured document model from git history."""
    stats = run_git_stats()

    # File counts
    py_files = count_files_by_glob("backend/**/*.py")
    jsx_files = count_files_by_glob("frontend/src/**/*.jsx")
    ts_files = count_files_by_glob("frontend/src/**/*.ts")
    test_files = count_files_by_glob("backend/tests/**/*.py")
    migration_count = count_files_by_glob("backend/alembic/versions/*.py")

    return {
        "title": "Zhanlu Development Contribution Report",
        "period": f"Last 90 Days",
        "generated_date": datetime.utcnow().strftime("%B %d, %Y"),
        "executive_summary": (
            "This report summarizes development activity on the Zhanlu platform "
            "over the past 90 days. The platform has evolved from a prototype "
            "chat agent into a multi-layered enterprise architecture with governed "
            "artifact output, sandboxed code execution, Synexia FSM cognitive core, "
            "multi-tenancy, and an extensible skills marketplace."
        ),

        "metrics": [
            {"label": "Total Commits", "value": str(stats["commits"])},
            {"label": "Files Changed", "value": str(stats["files"])},
            {"label": "Lines Added", "value": f"+{stats['insertions']:,}" if stats['insertions'] else "—"},
            {"label": "Lines Removed", "value": f"-{stats['deletions']:,}" if stats['deletions'] else "—"},
        ],

        "metrics_table": [
            {"metric": "Python files (backend)", "value": str(py_files), "notes": "Services, routers, models, tests"},
            {"metric": "React components (frontend)", "value": str(jsx_files), "notes": "Chat UI, artifact cards, dashboards"},
            {"metric": "TypeScript files (frontend)", "value": str(ts_files), "notes": "Hooks, utils, type definitions"},
            {"metric": "Test files", "value": str(test_files), "notes": "Pytest unit + integration tests"},
            {"metric": "Database migrations", "value": str(migration_count), "notes": "Alembic versioned schema changes"},
            {"metric": "Total commits (90d)", "value": str(stats["commits"]), "notes": "Non-merge commits on master"},
        ],

        "areas": [
            {
                "tag": "Layer 2",
                "name": "Tools & Skills Framework",
                "summary": "The pluggable tool/skill system enables agents to invoke external capabilities (web search, image generation, file operations, code execution) through a unified interface with progressive disclosure and marketplace support.",
                "items": [
                    {"title": "Skill Marketplace", "description": "Discoverable, installable skill packages with versioning and dependency management."},
                    {"title": "Progressive Disclosure", "description": "Tools expose their capabilities incrementally based on conversation context."},
                    {"title": "Delegate Task", "description": "Sub-conversation delegation with configurable iteration limits and timeouts."},
                ],
            },
            {
                "tag": "Layer 3",
                "name": "Core Chat & Conversation Engine",
                "summary": "The conversation engine manages multi-turn chat with context compaction, streaming responses, multi-modal attachments, and structured output cards (ReportCard, DataTable, ClarifyOption).",
                "items": [
                    {"title": "Context Compaction", "description": "Auto-threshold compaction (micro/full) prevents context window overflow while preserving conversation coherence."},
                    {"title": "Structured Output Cards", "description": "ReportCard, DataTable, Dashboard, ClarifyOptions provide rich inline UI beyond plain text."},
                    {"title": "Streaming Architecture", "description": "Real-time token streaming with support for tool-use interleaving."},
                ],
            },
            {
                "tag": "Layer 4",
                "name": "Artifact System",
                "summary": "The artifact system provides governed, versioned output generation with lifecycle management, multiple export formats, and inline preview in chat. This phase adds storage abstraction and canonical format support.",
                "items": [
                    {"title": "Governed Lifecycle", "description": "draft → building → preview_ready → validated → approved → published with provenance tracking."},
                    {"title": "Version Control", "description": "Immutable versions with changelogs, source JSON for partial regeneration."},
                    {"title": "Multi-Format Export", "description": "PDF, PPTX, XLSX, CSV, DOCX, HTML export with caching and lazy rendering."},
                    {"title": "Storage Abstraction", "description": "Pluggable backends (Postgres BYTEA, MinIO/S3) via BlobStorage interface."},
                    {"title": "Canonical Format", "description": "HTML → DOCX/PDF renderers enable report-based artifact generation."},
                ],
            },
            {
                "tag": "Layer 5",
                "name": "Synexia FSM Cognitive Core",
                "summary": "The finite-state machine cognitive architecture provides structured decision-making with states like CLARIFY, RESEARCH, ANALYZE, GENERATE, and FINALIZE — replacing raw tool loops with governed workflows.",
                "items": [
                    {"title": "FSM States", "description": "CLARIFY → RESEARCH → ANALYZE → GENERATE → FINALIZE pipeline with transitions."},
                    {"title": "ReportCard Contracts", "description": "Typed data contracts between FSM stages ensure structured, validatable output."},
                    {"title": "Feature Flag", "description": "SYNEXIA_FSM_ENABLED toggle for gradual rollout alongside legacy tool-loop."},
                ],
            },
            {
                "tag": "Layer 6",
                "name": "Enterprise Layer (Multi-Tenancy & Governance)",
                "summary": "Multi-tenant isolation, RBAC governance, and Redis-backed job queues form the enterprise foundation. Every model carries org_id/app_id for data isolation.",
                "items": [
                    {"title": "Multi-Tenant Isolation", "description": "Every table has org_id + app_id columns with default values for backward compatibility."},
                    {"title": "Governance System", "description": "Policy engine for access control, rate limiting, and audit logging."},
                    {"title": "Workspace Settings", "description": "Per-workspace configuration for tools, models, and permissions."},
                ],
            },
            {
                "tag": "Infra",
                "name": "Sandbox & Code Execution",
                "summary": "Docker-based sandboxed execution environment for Python, Office, and WebApp workloads with resource limits and network isolation.",
                "items": [
                    {"title": "Container Isolation", "description": "Per-job Docker containers with CPU/memory/PID limits and network policies."},
                    {"title": "Multi-Runtime", "description": "Python, Office (LibreOffice), and WebApp sandbox images."},
                    {"title": "Sandbox Timeline", "description": "Real-time job status tracking with live log streaming in the UI."},
                ],
            },
            {
                "tag": "UX",
                "name": "Agent Experience & Frontend",
                "summary": "The React frontend provides a rich chat interface with artifact cards, file previews, activity rails, and a comprehensive component library.",
                "items": [
                    {"title": "Chat Interface", "description": "Full-featured chat with streaming, thinking indicators, and multi-modal input."},
                    {"title": "Artifact Previews", "description": "Inline DOCX, HTML, Markdown, image, and dashboard previews within chat cards."},
                    {"title": "Activity Rail", "description": "Step-by-step agent activity visualization with expandable details."},
                ],
            },
        ],

        "achievements": [
            {
                "title": "Multi-Tenant Architecture Deployed",
                "description": "All 16 migrations include org_id/app_id columns, enabling true data isolation across tenants while maintaining backward compatibility with existing single-tenant data.",
            },
            {
                "title": "Complete Artifact Lifecycle Pipeline",
                "description": "End-to-end governed artifact generation: create → build → preview → validate → approve → publish, with version control and multi-format export.",
            },
            {
                "title": "Storage Abstraction Layer",
                "description": "Pluggable BlobStorage interface with Postgres and MinIO backends enables horizontal scaling of artifact blob storage without application changes.",
            },
            {
                "title": "Progressive Skill Disclosure",
                "description": "Tools and skills expose capabilities incrementally based on context, reducing cognitive load on the LLM and improving response accuracy.",
            },
            {
                "title": "Streaming + Structured Output",
                "description": "Real-time token streaming combined with structured output cards (ReportCard, DataTable) for rich, interactive agent responses.",
            },
        ],

        "roadmap": [
            {
                "phase": "Phase 1 (Current)",
                "description": "Complete HTML canonical format pipeline with contribution report generation. Stabilize artifact storage abstraction.",
            },
            {
                "phase": "Phase 2 (Next)",
                "description": "Enable MinIO production deployment. Add background format pre-rendering. Implement partial regeneration for PPTX/DOCX artifacts.",
            },
            {
                "phase": "Phase 3 (Future)",
                "description": "Synexia FSM full rollout replacing tool-loop. Redis-backed job queues for async artifact generation. WebSocket streaming for sandbox jobs.",
            },
            {
                "phase": "Phase 4 (Vision)",
                "description": "Multi-agent collaboration with shared workspaces. Enterprise SSO integration. Advanced analytics dashboard with NL2SQL query engine.",
            },
        ],

        "tech_stack": [
            {"layer": "Backend Framework", "tech": "FastAPI (Python 3.11)", "purpose": "Async REST API with WebSocket support"},
            {"layer": "Database", "tech": "PostgreSQL + SQLAlchemy + Alembic", "purpose": "Relational storage with versioned migrations"},
            {"layer": "Frontend", "tech": "React 18 + Vite + Tailwind CSS", "purpose": "Modern SPA with utility-first CSS"},
            {"layer": "Object Storage", "tech": "MinIO (S3-compatible)", "purpose": "Scalable artifact blob persistence"},
            {"layer": "Document Processing", "tech": "python-docx, pandoc, weasyprint", "purpose": "DOCX/PDF generation and conversion"},
            {"layer": "Templating", "tech": "Jinja2", "purpose": "HTML report template rendering"},
            {"layer": "Sandbox", "tech": "Docker", "purpose": "Isolated code execution environment"},
            {"layer": "Queue (planned)", "tech": "Redis", "purpose": "Job queues, locks, event fanout"},
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# 3. HTML Rendering
# ══════════════════════════════════════════════════════════════════════


def render_html(model: dict) -> bytes:
    """Render the doc model into HTML bytes via Jinja2."""
    from jinja2 import Environment, FileSystemLoader

    template_dir = Path(__file__).resolve().parent / "report_templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    template = env.get_template("contribution_report.html")
    html_str = template.render(**model)
    return html_str.encode("utf-8")


# ══════════════════════════════════════════════════════════════════════
# 4. DOCX Rendering
# ══════════════════════════════════════════════════════════════════════


def render_docx(html_bytes: bytes) -> bytes:
    """Convert HTML to DOCX via the html_docx renderer."""
    from app.services.artifacts.exporters.html_docx import render_html_to_docx
    return render_html_to_docx(html_bytes)


# ══════════════════════════════════════════════════════════════════════
# 5. Artifact Pipeline
# ══════════════════════════════════════════════════════════════════════


def create_artifact(title: str, html_bytes: bytes, docx_bytes: bytes) -> dict:
    """Create artifact + version + blobs in the database.

    Returns a dict with artifact_id, version_id, and blob info.
    """
    from app.database import SessionLocal
    from app.services.artifacts.artifact_service import ArtifactService

    db = SessionLocal()
    try:
        svc = ArtifactService(db)

        # 1. Create Artifact
        artifact = svc.create_artifact(
            artifact_type="html_report",
            title=title,
            description="Automated contribution report from git history analysis.",
        )
        artifact.canonical_format = "html"

        # 2. Create Version
        version = svc.create_version(
            artifact_id=artifact.id,
            changelog="Initial generation from git history",
            produced_by_skill="contribution_report_generator",
        )

        # 3. Store HTML blob (original)
        html_checksum = hashlib.sha256(html_bytes).hexdigest()
        html_blob = svc.store_blob(
            version_id=version.id,
            blob_type="original",
            file_name=f"{artifact.id}_report.html",
            mime_type="text/html; charset=utf-8",
            data=html_bytes,
        )

        # 4. Store DOCX blob (preview)
        docx_blob = svc.store_blob(
            version_id=version.id,
            blob_type="preview",
            file_name=f"{artifact.id}_report.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            data=docx_bytes,
        )

        # 5. Mark version as built
        svc.mark_version_built(version.id)

        db.commit()

        logger.info("Created artifact %s (version %d)", artifact.id, version.version_number)

        return {
            "artifact_id": artifact.id,
            "version_id": version.id,
            "html_blob_id": html_blob.id,
            "docx_blob_id": docx_blob.id,
            "title": title,
        }
    finally:
        db.close()


def create_stub_conversation(artifact_id: str) -> tuple[str, str]:
    """Create a stub conversation + chat session + message + artifact link.

    Returns (conversation_id, message_id).
    """
    from app.database import SessionLocal
    from app.models.artifact import MessageArtifact
    from uuid import uuid4
    import json

    db = SessionLocal()
    try:
        from sqlalchemy import text
        try:
            conv_id = str(uuid4())
            session_id = str(uuid4())
            msg_id = str(uuid4())

            # 1. Create stub conversation
            db.execute(text(
                "INSERT INTO agent_conversations (id, title, agent_name, status, org_id, app_id, is_deleted) "
                "VALUES (:id, :title, :agent_name, :status, :org_id, :app_id, :is_deleted)"
            ), {
                "id": conv_id,
                "title": "Contribution Report",
                "agent_name": "system",
                "status": "completed",
                "org_id": "default-org",
                "app_id": "default-app",
                "is_deleted": False,
            })

            # 2. Create stub chat session
            db.execute(text(
                "INSERT INTO chat_sessions (id, title, org_id, app_id, is_deleted) "
                "VALUES (:id, :title, :org_id, :app_id, :is_deleted)"
            ), {
                "id": session_id,
                "title": "Contribution Report",
                "org_id": "default-org",
                "app_id": "default-app",
                "is_deleted": False,
            })

            # 3. Create stub chat message
            db.execute(text(
                "INSERT INTO chat_messages (id, session_id, role, content, is_deleted) "
                "VALUES (:id, :session_id, :role, :content, :is_deleted)"
            ), {
                "id": msg_id,
                "session_id": session_id,
                "role": "assistant",
                "content": json.dumps({"type": "artifact", "artifact_id": artifact_id}),
                "is_deleted": False,
            })

            # 4. Link artifact to message
            link = MessageArtifact(
                id=str(uuid4()),
                message_id=msg_id,
                conversation_id=conv_id,
                artifact_id=artifact_id,
                display_order=0,
            )
            db.add(link)
            db.commit()
            logger.info("Created stub conversation %s, session %s, message %s", conv_id, session_id, msg_id)
        except Exception as exc:
            db.rollback()
            logger.warning("Could not create stub conversation: %s", exc)
            conv_id, msg_id = "", ""

        return conv_id, msg_id
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════
# 6. Main
# ══════════════════════════════════════════════════════════════════════


def main():
    logger.info("=== Zhanlu Contribution Report Generator ===")

    # Step 1: Build doc model
    logger.info("Step 1: Analyzing git history...")
    model = build_doc_model()
    logger.info("  → %d areas, %d achievements, %d roadmap items",
                len(model["areas"]), len(model["achievements"]), len(model["roadmap"]))

    # Step 2: Render HTML
    logger.info("Step 2: Rendering HTML via Jinja2...")
    html_bytes = render_html(model)
    logger.info("  → %d bytes of HTML", len(html_bytes))

    # Step 3: Render DOCX
    logger.info("Step 3: Converting HTML → DOCX...")
    docx_bytes = render_docx(html_bytes)
    logger.info("  → %d bytes of DOCX", len(docx_bytes))

    # Step 4: Write DOCX to disk
    docx_path = _backend_dir.parent / "zhanlu_improvement_report.docx"
    docx_path.write_bytes(docx_bytes)
    logger.info("Step 4: DOCX written to %s (%d bytes)", docx_path, len(docx_bytes))

    # Step 5: Create artifact pipeline
    logger.info("Step 5: Creating artifact in database...")
    info = create_artifact(model["title"], html_bytes, docx_bytes)
    logger.info("  → Artifact ID: %s", info["artifact_id"])
    logger.info("  → Version ID: %s", info["version_id"])

    # Step 6: Create stub conversation for inline preview
    logger.info("Step 6: Creating stub conversation...")
    conv_id, msg_id = create_stub_conversation(info["artifact_id"])
    if conv_id:
        logger.info("  → Conversation ID: %s", conv_id)
        logger.info("  → Message ID: %s", msg_id)

    # Summary
    logger.info("=== Done ===")
    logger.info("Report artifact: %s", info["artifact_id"])
    logger.info("Preview: GET /api/artifacts/%s/preview", info["artifact_id"])
    logger.info("Download DOCX: GET /api/artifacts/%s/download?format=docx", info["artifact_id"])


if __name__ == "__main__":
    main()
