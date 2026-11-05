"""B8 — periodic catalog coverage refresh.

Pins down that ``_refresh_kb_coverage`` re-probes the table's temporal column and
refreshes ``kb_table_meta.coverage_json`` (the data-window ``reroute_uncovered``
acts on) together with ``indexed_at``, and that the full ``_refresh_all_kbs`` scan
only touches stale, already-covered tables of ready database KBs.

The real connector is replaced by a fake so no DB driver is required.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401  register all models
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

from app.services.knowledge_graph import catalog_coverage_refresh as cr


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def sessions():
    """Temp-sqlite session factory, wired in as the refresh task's SessionLocal."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass


class _FakeConn:
    """Context-manager connector stub that returns canned MIN/MAX dates."""

    def __init__(self, min_date: str, max_date: str):
        self._min = min_date
        self._max = max_date

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, timeout_s=None):  # noqa: ARG002 — signature parity
        return [{"min_date": self._min, "max_date": self._max}]


@pytest.fixture
def fake_connector(monkeypatch):
    """Point the refresh task at a fake connector returning fresh dates."""
    conn = _FakeConn("2024-01-01", "2026-10-22")

    def _factory(kb):
        return conn

    monkeypatch.setattr(cr, "get_connector", _factory)
    return conn


@pytest.fixture
def seeded(sessions):
    """Seed a ready database KB with one table carrying a stale window."""
    s = sessions()
    kb_id = str(uuid.uuid4())
    kb = KnowledgeBase(
        id=kb_id,
        name="sales-wh",
        source_kind="database",
        db_type="postgres",
        catalog_status="ready",
        is_deleted=False,
    )
    s.add(kb)
    s.flush()
    old = datetime.utcnow() - timedelta(days=30)
    meta = KBTableMeta(
        kb_id=kb_id,
        schema_name="public",
        table_name="sales_order",
        table_role="fact",
        indexed_at=old,
        coverage_json={
            "date_column": "order_date",
            "min_date": "2024-01-01",
            "max_date": "2026-10-08",  # stale — warehouse now has data through 10-19/22
            "probed_at": old.isoformat() + "Z",
        },
    )
    s.add(meta)
    s.flush()
    s.add(
        KBColumnMeta(
            table_meta_id=meta.id,
            column_name="order_date",
            ordinal=0,
            data_type="date",
            semantic_type="temporal",
        )
    )
    s.commit()
    s.close()
    return kb_id


# ── tests ────────────────────────────────────────────────────────────────


def test_refresh_updates_coverage_and_indexed_at(sessions, seeded, fake_connector):
    s = sessions()
    kb_id = seeded
    meta = (
        s.query(KBTableMeta).filter(KBTableMeta.kb_id == kb_id).one()
    )
    old_indexed = meta.indexed_at

    # cutoff 7 days back → the 30-day-old table is stale and gets selected.
    cutoff = datetime.utcnow() - timedelta(days=7)
    refreshed = cr._refresh_kb_coverage(s, _reload_kb(s, kb_id), cutoff)
    s.commit()

    assert refreshed == 1
    s.expire_all()
    meta = s.query(KBTableMeta).filter(KBTableMeta.kb_id == kb_id).one()
    assert meta.coverage_json["max_date"] == "2026-10-22"  # freshly probed
    assert meta.indexed_at > old_indexed  # write-once violation fixed
    s.close()


def test_refresh_skips_freshly_indexed_tables(sessions, seeded, fake_connector):
    s = sessions()
    # Make the table freshly indexed, so it is within the stale window.
    meta = s.query(KBTableMeta).filter(KBTableMeta.kb_id == seeded).one()
    meta.indexed_at = datetime.utcnow() - timedelta(hours=1)
    s.commit()
    cutoff = datetime.utcnow() - timedelta(days=1)  # 24h stale window
    refreshed = cr._refresh_kb_coverage(s, _reload_kb(s, seeded), cutoff)
    assert refreshed == 0
    s.close()


def test_refresh_skips_tables_without_coverage(sessions, seeded, fake_connector):
    s = sessions()
    meta = s.query(KBTableMeta).filter(KBTableMeta.kb_id == seeded).one()
    meta.coverage_json = None  # never had a temporal column
    s.commit()
    cutoff = datetime.utcnow() - timedelta(days=365)
    refreshed = cr._refresh_kb_coverage(s, _reload_kb(s, seeded), cutoff)
    assert refreshed == 0
    s.close()


def test_refresh_all_kbs_end_to_end(sessions, seeded, fake_connector, monkeypatch):
    monkeypatch.setattr(cr, "SessionLocal", sessions)
    total = cr._refresh_all_kbs()
    assert total == 1
    s = sessions()
    meta = s.query(KBTableMeta).filter(KBTableMeta.kb_id == seeded).one()
    assert meta.coverage_json["max_date"] == "2026-10-22"
    s.close()


def test_run_loop_guards_on_probe_disabled(monkeypatch):
    import asyncio

    monkeypatch.setattr(cr.settings, "KG_COVERAGE_REFRESH_ENABLED", True)
    monkeypatch.setattr(cr.settings, "KG_COVERAGE_PROBE_ENABLED", False)
    calls: dict = {}
    real_refresh = cr._refresh_all_kbs

    def _spy():
        calls["called"] = True
        return real_refresh()

    monkeypatch.setattr(cr, "_refresh_all_kbs", _spy)

    # stop_event already set → the loop runs its body once, then breaks.
    class _Ev:
        def is_set(self):
            return True

        def wait(self):
            return None

    async def _run():
        await cr.run_catalog_coverage_refresh(_Ev())

    asyncio.run(_run())
    assert "called" not in calls  # skipped because probe is disabled


# ── helpers ───────────────────────────────────────────────────────────────


def _reload_kb(s, kb_id):
    return s.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).one()
