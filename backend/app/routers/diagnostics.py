"""Diagnostic router — ``GET /api/_db-info``.

Returns a JSON snapshot of which database the backend is connected to, the
alembic schema version, and the row count for every entity table. Designed
to be the first thing you hit when the UI is showing empty data and you
need to know which engine is actually serving requests.

Endpoint is intentionally unauthenticated — it leaks no PII, only schema
metadata — so it can be used from curl, from a browser console, or from
ops dashboards without juggling tokens.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter
from sqlalchemy import inspect, text

from app.config import settings
from app.database import SessionLocal, engine
from app.models import (  # noqa: F401 — full model import for the registry
    AgentApp, AgentConversation, AgentDataBinding, AgentSkillBinding,
    AgentInvocation, AgentMemory, AgentTestCase, AgentTodo, AnalyticsEvent,
    AppWorkspace, ApprovalRequest, Artifact, ArtifactBlob,
    ArtifactBuildManifest, ArtifactSourcePart, ArtifactVersion, AuditEvent,
    AuditLog, AutomationTask, ChatMessage, ChatSession, ContextManifest,
    CostLedger, DataSnapshot, DecisionFlow, Datasource, ExperienceEntry,
    Execution, KnowledgeBase, LearningProposal, MarketAgent, MarketplaceRating,
    MarketplaceSkill, McpServer, MessageArtifact, MetricDefinition,
    ObservationRecord, Organization, OtpCode, PasswordResetToken, Plan, PlanNode,
    PolicyDecision, Project, Report, ResponseCacheEntry, SandboxCommand, SandboxJob,
    SandboxJobEvent, SemanticMapping, SkillCandidate, SkillProfile, SkillRun,
    SkillTestCase, SnapshotArtifactLink, Tool, User, UserFile, UserSetting,
    WorkspaceSetting,
)


logger = logging.getLogger(__name__)
router = APIRouter(tags=["diagnostics"])


# Order chosen to match the data hierarchy: orgs → workspaces → users → …
# so the most "structural" tables appear first.
_MODELS_FOR_REPORT = [
    Organization, AppWorkspace, User, Project,
    ChatSession, ChatMessage, AgentApp, AgentConversation, AgentMemory, AgentTodo,
    AgentDataBinding, AgentSkillBinding, AgentInvocation, AgentTestCase,
    KnowledgeBase, AutomationTask, Tool, UserFile, Report, DecisionFlow,
    MarketAgent, MarketplaceSkill, MarketplaceRating, McpServer, UserSetting,
    AnalyticsEvent, OtpCode, PasswordResetToken, WorkspaceSetting,
    Artifact, ArtifactVersion, ArtifactBlob, MessageArtifact, ArtifactSourcePart,
    ArtifactBuildManifest, SandboxJob, SandboxJobEvent, SandboxCommand,
    DataSnapshot, SnapshotArtifactLink, Execution, Plan, PlanNode, ObservationRecord,
    SkillProfile, SkillCandidate, SkillRun, SkillTestCase,
    PolicyDecision, ApprovalRequest, CostLedger, AuditLog, AuditEvent,
    Datasource, MetricDefinition, SemanticMapping, ContextManifest,
    ExperienceEntry, LearningProposal,
]


def _parse_url(url: str) -> dict:
    u = urlparse(url)
    driver = url.split("://", 1)[0] if "://" in url else "?"
    return {
        "driver": driver,
        "scheme": u.scheme,
        "user": u.username or "",
        "host": u.hostname or "",
        "port": u.port,
        "database": u.path.lstrip("/") if u.scheme.startswith("sqlite") else u.path.lstrip("/"),
    }


@router.get("/_db-info")
def db_info() -> dict:
    """Return engine, schema version, and per-table row counts.

    Example::

        $ curl -s http://localhost:5002/api/_db-info | jq .
        {
          "engine": { "driver": "postgresql+psycopg2", "host": "postgres", ... },
          "database_url": "postgresql+psycopg2://zhanlu:***@postgres:5432/zhanlu",
          "is_sqlite": false,
          "org_id": "default-org",
          "app_id": "default-app",
          "alembic_version": "016",
          "table_counts": { "users": 1, "chat_sessions": 17, ... },
          "connected": true,
          "server_version": "PostgreSQL 16.3 ..."
        }
    """
    result: dict = {
        "connected": False,
        "engine": _parse_url(settings.DATABASE_URL),
        "database_url": settings.database_url_safe,
        "is_sqlite": settings.is_sqlite,
        "org_id": settings.DEFAULT_ORG_ID,
        "app_id": settings.DEFAULT_APP_ID,
    }

    try:
        with engine.connect() as conn:
            if settings.is_sqlite:
                v = conn.execute(text("SELECT sqlite_version()")).scalar() or ""
            else:
                v = conn.execute(text("SELECT version()")).scalar() or ""
        result["server_version"] = (v.split(" on ")[0].strip() if " on " in v else v)
        result["connected"] = True
    except Exception as exc:  # noqa: BLE001
        logger.exception("db_info: connection probe failed")
        result["connection_error"] = f"{type(exc).__name__}: {exc}"
        return result

    # Alembic version (non-fatal if missing)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        result["alembic_version"] = row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        result["alembic_version"] = None
        result["alembic_error"] = f"{type(exc).__name__}: {exc}"

    # Row counts
    counts: dict[str, int | str] = {}
    inspector = inspect(engine)
    present_tables = set(inspector.get_table_names())
    db = SessionLocal()
    try:
        for model in _MODELS_FOR_REPORT:
            tbl = model.__tablename__
            if tbl not in present_tables:
                continue
            try:
                counts[tbl] = db.query(model).count()
            except Exception as exc:  # noqa: BLE001
                counts[tbl] = f"ERR: {type(exc).__name__}"
    finally:
        db.close()
    result["table_counts"] = counts

    return result


@router.get("/api/_agent-metrics")
async def agent_metrics():
    """Agent reliability metrics — counters and histograms for P0-P4 features.

    Returns a JSON snapshot of all agent reliability metrics: guardrail fires,
    iteration budget consumption, tool result persistence, pre-API pruning,
    error classification distribution, prompt caching, verification-on-stop,
    provider fallback, message sanitization, and background review.

    Intentionally unauthenticated (same as /api/_db-info) — no PII, only
    aggregate operational metrics.
    """
    from app.services.agent_metrics import metrics as _metrics
    return _metrics.get_snapshot()


@router.get("/api/_skill-curation")
async def skill_curation():
    """Skill curation report — overlapping and stale skill suggestions.

    Returns a JSON report of skill overlap pairs (with merge suggestions)
    and stale skills (with archive suggestions). Does NOT modify skills.
    """
    from app.services.skill_curator import run_skill_curation
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        report = run_skill_curation(db)
    return report.to_dict()
