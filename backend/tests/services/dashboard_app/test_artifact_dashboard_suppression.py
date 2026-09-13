"""Contract test for the T18 dashboard-turn artifact suppression seam.

ArtifactService.create_artifact() returns None (not an Artifact) when the
dashboard-intent guard suppresses an analytics-path artifact. The create
tool handler must None-check before dereferencing artifact.id (2026-09-02
review #2 — previously crashed with AttributeError on dashboard turns).
"""

from unittest.mock import MagicMock

from app.config import settings
from app.services.artifacts.artifact_service import ArtifactService
from app.services.dashboard_intent import reset_dashboard_intent, set_dashboard_intent


def test_create_artifact_returns_none_when_dashboard_turn_suppresses():
    token = None
    try:
        token = set_dashboard_intent(True)
        db = MagicMock()
        svc = ArtifactService(db)
        art = svc.create_artifact(
            artifact_type="html",
            title="stray static page",
            conversation_id="conv-1",
            source="agent",  # NOT dashboard_app → suppressed
        )
        assert art is None
    finally:
        if token is not None:
            reset_dashboard_intent(token)


def test_create_artifact_passes_through_for_dashboard_app_source():
    token = None
    try:
        token = set_dashboard_intent(True)
        db = MagicMock()
        # Fake the add/commit used by the real path so we can observe a
        # non-None Artifact is returned for source="dashboard_app".
        from app.models.artifact import Artifact
        fake = Artifact(id="a-1")
        db.add = MagicMock()
        db.commit = MagicMock()
        db.refresh = MagicMock(side_effect=lambda obj: obj)
        svc = ArtifactService(db)
        # Monkeypatch the internal create to simulate persistence success
        # while still exercising the guard branch.
        import app.services.artifacts.artifact_service as asvc

        original = asvc.Artifact
        asvc.Artifact = lambda **kw: fake
        try:
            art = svc.create_artifact(
                artifact_type="html",
                title="real dashboard app",
                conversation_id="conv-1",
                source="dashboard_app",  # exempt from suppression
            )
            assert art is not None
        finally:
            asvc.Artifact = original
    finally:
        if token is not None:
            reset_dashboard_intent(token)
