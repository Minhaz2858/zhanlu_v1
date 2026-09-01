"""Phase 5 — Weekly digest aggregation test (isolated SQLite)."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import pytest


@pytest.fixture()
def sqlite_session():
    engine = create_engine("sqlite:///:memory:")
    # Build only the artifact_events table.
    from app.models.artifact_event import ArtifactEvent
    ArtifactEvent.__table__.create(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def _seed(session, events):
    from app.models.artifact_event import ArtifactEvent
    for ev in events:
        session.add(ArtifactEvent(**ev))
    session.commit()


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "weekly_digest.py"
    spec = importlib.util.spec_from_file_location("_weekly_digest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_digest_aggregates(sqlite_session, monkeypatch):
    now = datetime.now(timezone.utc)
    _seed(sqlite_session, [
        {"event_type": "deck_generated", "artifact_id": "a1", "created_date": now},
        {"event_type": "deck_generated", "artifact_id": "a2", "created_date": now},
        {"event_type": "deck_edited", "artifact_id": "a1",
         "metadata_json": '{"edit_kind": "restyle_deck"}', "created_date": now},
        {"event_type": "deck_downloaded", "artifact_id": "a1", "created_date": now},
        # Outside the 7-day window → must be excluded.
        {"event_type": "deck_generated", "artifact_id": "old",
         "created_date": now - timedelta(days=30)},
    ])

    mod = _load_script()
    # Point the script at the in-memory session factory.
    monkeypatch.setattr(mod, "SessionLocal", lambda: sqlite_session)
    digest = mod.build_digest(days=7)

    assert digest["total_events"] == 4
    assert digest["distinct_decks"] == 2
    assert digest["by_event_type"]["deck_generated"] == 2
    assert digest["by_event_type"]["deck_edited"] == 1
    assert digest["by_event_type"]["deck_downloaded"] == 1
    assert digest["edit_kinds"]["restyle_deck"] == 1
    # The 30-day-old event is excluded.
    assert "old" not in digest["by_event_type"]
