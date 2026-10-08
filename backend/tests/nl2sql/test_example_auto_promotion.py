"""Q→SQL example auto-promotion — OCI feedback loop (P0-3) tests.

Pins: flag-gated (default off), only verified callers invoke it, dedupe by
sql_hash per datasource, per-datasource cap, manual rows are never touched by
the promotion path, and the promoted row carries source='workload'.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models.base import TimestampedBase
from app.models.q_sql_example import QSqlExample
from app.services.q_sql_examples.service import QSqlExampleService


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TimestampedBase.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED", True)


def _promote(session, question="monthly sales", sql="SELECT 1", ds="kb1"):
    return QSqlExampleService.maybe_auto_promote(
        session, question=question, sql=sql, datasource_id=ds
    )


def test_promotion_inserts_workload_row(db):
    assert _promote(db) is True
    row = db.query(QSqlExample).one()
    assert row.source == "workload"
    assert row.sql_hash
    assert row.datasource_id == "kb1"
    assert row.enabled is True


def test_flag_off_blocks_promotion(db, monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED", False)
    assert _promote(db) is False
    assert db.query(QSqlExample).count() == 0


def test_dedupe_by_sql_hash_same_datasource(db):
    assert _promote(db) is True
    # Same SQL re-verified under a different phrasing → no second row.
    assert _promote(db, question="sales monthly") is False
    assert db.query(QSqlExample).count() == 1


def test_same_sql_other_datasource_is_separate(db):
    assert _promote(db, ds="kb1") is True
    assert _promote(db, ds="kb2") is True
    assert db.query(QSqlExample).count() == 2


def test_whitespace_normalised_dedupe(db):
    assert _promote(db, sql="SELECT   1") is True
    assert _promote(db, sql="select 1") is False
    assert db.query(QSqlExample).count() == 1


def test_empty_inputs_refused(db):
    assert _promote(db, question="  ") is False
    assert _promote(db, sql="") is False
    assert db.query(QSqlExample).count() == 0


def test_manual_rows_are_not_deduped_away(db):
    db.add(
        QSqlExample(
            question="authored by a human",
            sql="SELECT 1",
            datasource_id="kb1",
            enabled=True,
            source="manual",
        )
    )
    db.commit()
    # The manual row has no sql_hash, so the promoter still inserts its own —
    # and it NEVER overwrites the manual row.
    assert _promote(db) is True
    rows = db.query(QSqlExample).all()
    manual = [r for r in rows if r.source == "manual"]
    workload = [r for r in rows if r.source == "workload"]
    assert len(manual) == 1 and len(workload) == 1
    assert manual[0].sql == "SELECT 1"
    assert manual[0].question == "authored by a human"


def test_per_datasource_cap(db, monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_EXAMPLE_AUTO_PROMOTION_MAX", 2)
    assert _promote(db, sql="SELECT 1") is True
    assert _promote(db, sql="SELECT 2") is True
    assert _promote(db, sql="SELECT 3") is False
    assert db.query(QSqlExample).count() == 2


def test_never_raises_on_db_error(db):
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("boom")

    assert QSqlExampleService.maybe_auto_promote(
        Boom(), question="q", sql="SELECT 1", datasource_id="kb1"
    ) is False
