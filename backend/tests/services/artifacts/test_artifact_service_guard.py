"""T18 regression: dashboard-turn guard in ArtifactService.create_artifact.

When FULLSTACK_DASHBOARD_ENABLED is on and the current turn is a dashboard
turn (the `dashboard_intent` ContextVar is set), any artifact written with a
source other than "dashboard_app" must be dropped (return None, 0 rows). This
prevents a stray analytics-path artifact (e.g. a static "Web page" written from
the agent's narration sentence) from landing on the same thread as the real
dashboard app.

Non-dashboard turns are unaffected, and the dashboard app itself (source
"dashboard_app") always passes through.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from app.database import Base
import app.models  # noqa: F401  ensure Artifact metadata is registered
from app.services.artifacts.artifact_service import ArtifactService
from app.services.dashboard_intent import (
    dashboard_intent,
    set_dashboard_intent,
    reset_dashboard_intent,
)
from app.models.artifact import Artifact
from app.config import settings


_engine = create_engine(
    "sqlite:///file::memory:?cache=shared&uri=true",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = __import__("sqlalchemy.orm").orm.sessionmaker(
    autocommit=False, autoflush=False, bind=_engine
)


@pytest.fixture
def db():
    # Each test gets a clean artifact table (isolation for count assertions).
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    session = _TestSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _count_artifacts(db):
    return db.query(Artifact).count()


def test_dashboard_turn_drop_non_dashboard_source(db, monkeypatch):
    """On a dashboard turn, a source='agent' artifact is dropped (0 rows)."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    token = set_dashboard_intent(True)
    try:
        svc = ArtifactService(db)
        result = svc.create_artifact(
            "html", "Your ERP Sales Overview dashboard is live.",
            conversation_id="conv1", source="agent",
        )
        assert result is None
        assert _count_artifacts(db) == 0
    finally:
        reset_dashboard_intent(token)


def test_dashboard_turn_keeps_dashboard_app_source(db, monkeypatch):
    """On a dashboard turn, source='dashboard_app' is persisted (1 row)."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    token = set_dashboard_intent(True)
    try:
        svc = ArtifactService(db)
        result = svc.create_artifact(
            "dashboard", "ERP Sales Overview",
            conversation_id="conv1", source="dashboard_app",
        )
        assert result is not None
        db.refresh(result)
        assert result.source == "dashboard_app"
        assert _count_artifacts(db) == 1
    finally:
        reset_dashboard_intent(token)


def test_non_dashboard_turn_writes_normal_artifact(db, monkeypatch):
    """A non-dashboard turn writes the artifact normally (no regression)."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    token = set_dashboard_intent(False)  # not a dashboard turn
    try:
        svc = ArtifactService(db)
        result = svc.create_artifact(
            "html", "A normal analytics artifact",
            conversation_id="conv2", source="agent",
        )
        assert result is not None
        assert _count_artifacts(db) == 1
    finally:
        reset_dashboard_intent(token)


def test_guard_off_when_flag_disabled(db, monkeypatch):
    """If FULLSTACK_DASHBOARD_ENABLED is off, the guard never fires."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False)
    token = set_dashboard_intent(True)
    try:
        svc = ArtifactService(db)
        result = svc.create_artifact(
            "html", "Stray on dashboard turn but flag off",
            conversation_id="conv3", source="agent",
        )
        assert result is not None
        assert _count_artifacts(db) == 1
    finally:
        reset_dashboard_intent(token)


def test_analytics_path_on_dashboard_turn_yields_zero_rows(db, monkeypatch):
    """Simulates the analytics "Crystallizing" path writing an artifact on a
    dashboard turn: it must produce exactly 0 rows (the real dashboard app is
    the only artifact expected on that thread)."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    token = set_dashboard_intent(True)
    try:
        svc = ArtifactService(db)
        # The narration-style artifact + any other analytics artifacts.
        for i in range(3):
            dropped = svc.create_artifact(
                "html", f"analytics artifact {i}",
                conversation_id="conv4", source="agent",
            )
            assert dropped is None
        # Only a dashboard_app artifact should survive.
        svc.create_artifact(
            "dashboard", "ERP Sales Overview",
            conversation_id="conv4", source="dashboard_app",
        )
        assert _count_artifacts(db) == 1
    finally:
        reset_dashboard_intent(token)
