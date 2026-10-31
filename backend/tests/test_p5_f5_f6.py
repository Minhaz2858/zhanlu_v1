"""P5 closure (workstream 1) — F5 version bump + F6 rollback re-validation.

F5: version bump is centralized in MetricDefinition.before_update (service
layer) so it covers BOTH the CRUD PATCH path and the foreign-owned promotion
bridge WITHOUT editing metric_bootstrap.py.

F6: a retired/rolled-back metric must never return an old computed number —
compute_metric re-resolves the CURRENT definition + lifecycle at execution
time (fresh query every call).
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(
        eng, tables=[KnowledgeBase.__table__, MetricDefinition.__table__])
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def data_db(tmp_path):
    path = tmp_path / "sales.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sales (amt REAL, flag TEXT)")
    conn.executemany("INSERT INTO sales VALUES (?, ?)",
                     [(100.0, "C"), (50.0, "C"), (10.0, "A")])
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture()
def kb(db, data_db):
    k = KnowledgeBase(org_id="org-x", name="db", source_kind="database",
                      db_type="sqlite", api_url=data_db)
    k.id = "kb-x"
    db.add(k)
    db.commit()
    return k


# ---------------------------------------------------------------------------
# F5 — version bump (service layer)
# ---------------------------------------------------------------------------

def test_f5_new_metric_defaults_to_version_1(db):
    m = MetricDefinition(id="m1", org_id="org-x", datasource_id="kb-x",
                         name="M1", base_table="sales", base_column="amt",
                         aggregation="sum", lifecycle="published",
                         is_enabled=True, reviewed_by="a")
    db.add(m)
    db.commit()
    assert m.version == 1  # server_default / model default


def test_f5_crud_mutation_bumps_version(db):
    m = MetricDefinition(id="m2", org_id="org-x", datasource_id="kb-x",
                         name="M2", base_table="sales", base_column="amt",
                         aggregation="sum", lifecycle="published",
                         is_enabled=True, reviewed_by="a")
    db.add(m)
    db.commit()
    v0 = m.version
    m.unit = "CNY"  # a tracked column changes -> before_update bumps
    db.commit()
    assert m.version == v0 + 1


def test_f5_promotion_style_update_bumps_without_foreign_edit(db):
    # Simulates metric_bootstrap.promote_to_metric_definition updating an
    # existing row directly (the foreign-owned path) — the centralized
    # listener must bump version even though we never touched that file.
    m = MetricDefinition(id="m3", org_id="org-x", datasource_id="kb-x",
                         name="M3", base_table="sales", base_column="amt",
                         aggregation="sum", lifecycle="published",
                         is_enabled=True, reviewed_by="a")
    db.add(m)
    db.commit()
    v0 = m.version
    m.base_column = "amt"  # no-op value but we force a real change:
    m.description = "updated by promotion bridge"
    db.commit()
    assert m.version == v0 + 1


def test_f5_no_spurious_bump_when_nothing_changed(db):
    m = MetricDefinition(id="m4", org_id="org-x", datasource_id="kb-x",
                         name="M4", base_table="sales", base_column="amt",
                         aggregation="sum", lifecycle="published",
                         is_enabled=True, reviewed_by="a")
    db.add(m)
    db.commit()
    v0 = m.version
    db.commit()  # flush with no attribute change
    assert m.version == v0


# ---------------------------------------------------------------------------
# F6 — rollback / lifecycle re-validation at execution time
# ---------------------------------------------------------------------------

def _metric(db, kid, mid="mg", published=True):
    m = MetricDefinition(
        id=mid, org_id="org-x", datasource_id=kid, name="Gross",
        base_table="sales", base_column="amt", aggregation="sum",
        lifecycle="published" if published else "retired",
        is_enabled=True, reviewed_by="a", version=1,
        evidence_status="verified")
    db.add(m)
    db.commit()
    return m


def test_f6_published_metric_computes(db, kb):
    from app.services.ontology.metrics import compute_metric
    _metric(db, kb.id, published=True)
    res = compute_metric(db, "org-x", "Gross")
    assert res["success"] is True
    assert res["value"] == 160.0  # 100+50+10


def test_f6_retired_metric_never_returns_old_number(db, kb):
    from app.services.ontology.metrics import compute_metric
    _metric(db, kb.id, published=False)  # retired
    res = compute_metric(db, "org-x", "Gross")
    # Must be typed unavailable — never the old computed 160.0
    assert res["success"] is False
    assert res["unavailable"] is True
    assert "value" not in res or res.get("value") is None


def test_f6_republished_metric_computes_again(db, kb):
    from app.services.ontology.metrics import compute_metric
    m = _metric(db, kb.id, published=False)
    res = compute_metric(db, "org-x", "Gross")
    assert res["success"] is False
    # Re-publish at execution time (current definition re-validated each call)
    m.lifecycle = "published"
    db.commit()
    res2 = compute_metric(db, "org-x", "Gross")
    assert res2["success"] is True
    assert res2["value"] == 160.0


def test_f6_disabled_metric_unavailable(db, kb):
    from app.services.ontology.metrics import compute_metric
    m = _metric(db, kb.id, published=True)
    m.is_enabled = False
    db.commit()
    res = compute_metric(db, "org-x", "Gross")
    assert res["success"] is False
    assert res["unavailable"] is True
