"""Diagnostic tool — print which database the backend is actually talking to.

This is the first command to run whenever something looks "off" with data
(records missing, wrong counts, fresh-looking tables). It answers three
questions in one shot:

  1. Which SQLAlchemy URL is in effect? (read from env / .env)
  2. Is the alembic schema at the expected version?
  3. How many rows are in every entity table?

Usage
-----
    cd /root/zhanlu/backend
    PYTHONPATH=. ./venv/bin/python scripts/check_db.py

    # Or, with a one-off override (does NOT mutate .env):
    PYTHONPATH=. DATABASE_URL=postgresql+psycopg2://u:p@h:5432/d ./venv/bin/python scripts/check_db.py

Exit codes
----------
    0  connection succeeded, summary printed
    1  DATABASE_URL missing (config refused to start)
    2  connection failed (wrong host, bad creds, etc.)
    3  alembic_version table missing (schema not migrated)
"""

from __future__ import annotations

import sys
import traceback
from urllib.parse import urlparse


def _print_banner(line: str) -> None:
    bar = "─" * max(40, len(line) + 4)
    print(f"\n{bar}\n  {line}\n{bar}")


def _parse_url(url: str) -> dict:
    """Pull host/port/user/db/driver out of a SQLAlchemy URL.

    Tolerant of both ``postgresql+psycopg2://`` and ``sqlite:///abs/path``.
    """
    try:
        u = urlparse(url)
        driver = url.split("://", 1)[0] if "://" in url else "?"
        # SQLite paths live in u.path with a leading "/" we don't want.
        database = u.path.lstrip("/") if u.scheme.startswith("sqlite") else u.path.lstrip("/")
        return {
            "driver": driver,
            "scheme": u.scheme,
            "user": u.username or "",
            "host": u.hostname or "(local)",
            "port": u.port or "",
            "database": database or u.netloc or "",
        }
    except Exception:  # noqa: BLE001
        return {"driver": "?", "scheme": "?", "user": "", "host": "?", "port": "", "database": url}


def main() -> int:
    # ── 1. Settings (will raise if DATABASE_URL is missing) ──────────────
    try:
        from app.config import settings  # noqa: WPS433 — script import
    except Exception as exc:  # noqa: BLE001
        print("✗ Failed to load settings — DATABASE_URL is probably missing or invalid.")
        print(f"  {type(exc).__name__}: {exc}")
        return 1

    _print_banner("ACTIVE DATABASE")
    parts = _parse_url(settings.DATABASE_URL)
    print(f"  URL     : {settings.database_url_safe}")
    print(f"  Driver  : {parts['driver']}")
    print(f"  Host    : {parts['host']}{':' + str(parts['port']) if parts['port'] else ''}")
    print(f"  Database: {parts['database']}")
    print(f"  User    : {parts['user'] or '(none)'}")
    print(f"  org_id  : {settings.DEFAULT_ORG_ID}")
    print(f"  app_id  : {settings.DEFAULT_APP_ID}")

    # ── 2. Live connection ───────────────────────────────────────────────
    _print_banner("CONNECTION TEST")
    try:
        from app.database import engine  # noqa: WPS433
        from sqlalchemy import text  # noqa: WPS433
        with engine.connect() as conn:
            if settings.is_sqlite:
                v = conn.execute(text("SELECT sqlite_version()")).scalar()
                print(f"  ✓ Connected (SQLite {v})")
            else:
                v = conn.execute(text("SELECT version()")).scalar()
                # psycopg returns something like "PostgreSQL 16.3 on ..."
                short = (v or "").split(" on ")[0].strip() or v
                print(f"  ✓ Connected ({short})")
    except Exception as exc:  # noqa: BLE001
        print("  ✗ Connection failed:")
        print(f"    {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 2

    # ── 3. Alembic version ───────────────────────────────────────────────
    _print_banner("ALEMBIC VERSION")
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
        if row is None:
            print("  ✗ alembic_version table is empty — schema has never been migrated.")
            return 3
        print(f"  ✓ schema version: {row[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ Could not read alembic_version: {exc}")
        return 3

    # ── 4. Row counts ────────────────────────────────────────────────────
    _print_banner("ENTITY TABLE ROW COUNTS")
    # Importing the models package triggers the full model registry, which
    # itself requires DATABASE_URL — we've already proven that above.
    try:
        from app.models import (  # noqa: WPS433
            Organization, AppWorkspace, User, Project, ChatSession, ChatMessage,
            AgentApp, KnowledgeBase, AutomationTask, Tool, UserFile, Report,
            DecisionFlow, MarketAgent, McpServer, UserSetting,
            AgentConversation, AnalyticsEvent, OtpCode, PasswordResetToken,
            AgentMemory, AgentTodo, Artifact, ArtifactVersion, ArtifactBlob,
            MessageArtifact, ArtifactSourcePart, SandboxJob, SandboxJobEvent,
            SandboxCommand, DataSnapshot, SnapshotArtifactLink, Execution,
            Plan, PlanNode, ObservationRecord, AgentDataBinding,
            AgentSkillBinding, SkillProfile, SkillCandidate, PolicyDecision,
            ApprovalRequest, CostLedger, AuditLog, WorkspaceSetting,
            MarketplaceSkill, MarketplaceRating, SkillRun, AgentInvocation,
            AgentTestCase, SkillTestCase, ArtifactBuildManifest, Datasource,
            MetricDefinition, SemanticMapping, ContextManifest,
            ExperienceEntry, LearningProposal, AuditEvent,
        )
        from sqlalchemy import inspect  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ Could not import model registry: {exc}")
        return 2

    from app.database import SessionLocal  # noqa: WPS433
    db = SessionLocal()
    try:
        inspector = inspect(engine)
        present_tables = set(inspector.get_table_names())
        model_table_pairs = []
        for model in [
            Organization, AppWorkspace, User, Project, ChatSession, ChatMessage,
            AgentApp, KnowledgeBase, AutomationTask, Tool, UserFile, Report,
            DecisionFlow, MarketAgent, McpServer, UserSetting,
            AgentConversation, AnalyticsEvent, OtpCode, PasswordResetToken,
            AgentMemory, AgentTodo, Artifact, ArtifactVersion, ArtifactBlob,
            MessageArtifact, ArtifactSourcePart, SandboxJob, SandboxJobEvent,
            SandboxCommand, DataSnapshot, SnapshotArtifactLink, Execution,
            Plan, PlanNode, ObservationRecord, AgentDataBinding,
            AgentSkillBinding, SkillProfile, SkillCandidate, PolicyDecision,
            ApprovalRequest, CostLedger, AuditLog, WorkspaceSetting,
            MarketplaceSkill, MarketplaceRating, SkillRun, AgentInvocation,
            AgentTestCase, SkillTestCase, ArtifactBuildManifest, Datasource,
            MetricDefinition, SemanticMapping, ContextManifest,
            ExperienceEntry, LearningProposal, AuditEvent,
        ]:
            tbl = model.__tablename__
            if tbl not in present_tables:
                continue
            try:
                count = db.query(model).count()
            except Exception as exc:  # noqa: BLE001
                count = f"ERR: {exc.__class__.__name__}"
            model_table_pairs.append((model.__name__, tbl, count))

        # Pretty print
        name_w = max(len(n) for n, _, _ in model_table_pairs)
        tbl_w = max(len(t) for _, t, _ in model_table_pairs)
        for name, tbl, count in sorted(model_table_pairs, key=lambda x: x[1]):
            print(f"  {name:<{name_w}}  {tbl:<{tbl_w}}  {count}")

        # Tables that exist in DB but have no model registered (informational)
        registered = {t for _, t, _ in model_table_pairs}
        orphans = sorted(present_tables - registered - {"alembic_version"})
        if orphans:
            print("\n  (DB has these tables with no matching model: {})".format(", ".join(orphans)))
    finally:
        db.close()

    _print_banner("DONE")
    print("  ✓ This is the database the backend will read/write against.")
    print("  ✓ If you expected a different one, fix DATABASE_URL in backend/.env")
    print("    and restart the backend container.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
