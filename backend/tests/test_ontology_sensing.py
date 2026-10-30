"""P3-2 sensing pipeline tests — state-change fan-out, scheduling,
honesty on errors. The LLM/fan-out boundary is monkeypatched; SQL runs
through a mocked QueryService (the read-only gate itself is tested
elsewhere)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology_sensor import OntologySensor, OntologySensorRun
import app.models.identity  # noqa: F401 — register identity tables in metadata (ordering-dependent create_all)

ORG = "org-sense"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _mk_sensor(db, **kw):
    defaults = dict(
        org_id=ORG, name="stock watch", object_name="Supplier",
        kb_id="kb1", watch_sql="SELECT qty FROM wh_stock LIMIT 1",
        op="lt", threshold=10.0, schedule_seconds=3600, enabled=True,
    )
    defaults.update(kw)
    sensor = OntologySensor(**defaults)
    db.add(sensor)
    db.commit()
    return sensor


def _patch_query(monkeypatch, rows):
    """Fake QueryService.execute returning the given first-row values."""
    import app.services.db.query_service as qs

    calls = []

    def fake_execute(self, kb_id, sql, **kw):
        calls.append({"kb_id": kb_id, "sql": sql})
        if isinstance(rows, Exception):
            raise rows
        return {"rows": rows}

    monkeypatch.setattr(qs.QueryService, "execute", fake_execute)
    return calls


def _patch_fanout(monkeypatch):
    fired = []

    def fake_notify(db_, org_id, object_name, summary, changed_by=None,
                    team_id=None):
        fired.append({"org_id": org_id, "object_name": object_name,
                      "summary": summary, "changed_by": changed_by,
                      "team_id": team_id})
        return {"notified": ["procurement-agent"], "audit_logged": 1}

    import app.services.ontology.fanout as fanout
    monkeypatch.setattr(fanout, "notify_object_change", fake_notify)
    # sensing.py imports inside the function — patch the module attribute
    import app.services.ontology.sensing as sensing
    monkeypatch.setattr(sensing, "notify_object_change", fake_notify,
                        raising=False)
    return fired


# ── core semantics ───────────────────────────────────────────────────────

def test_breach_on_first_run_fires_fanout(db, monkeypatch):
    from app.services.ontology.sensing import evaluate_sensor

    sensor = _mk_sensor(db)  # threshold: value < 10 → 3 is a breach
    _patch_query(monkeypatch, [{"qty": 3}])
    fired = _patch_fanout(monkeypatch)
    out = evaluate_sensor(db, sensor)
    assert out["breached"] and out["transition"] == "breach"
    assert len(fired) == 1
    assert fired[0]["object_name"] == "Supplier"
    assert fired[0]["changed_by"] == f"sensor:{sensor.id}"


def test_no_duplicate_fanout_while_breach_persists(db, monkeypatch):
    from app.services.ontology.sensing import evaluate_sensor

    sensor = _mk_sensor(db)
    _patch_query(monkeypatch, [{"qty": 3}])
    fired = _patch_fanout(monkeypatch)
    evaluate_sensor(db, sensor)
    evaluate_sensor(db, sensor)  # still breached — must NOT fire again
    assert len(fired) == 1
    transitions = [r.transition for r in db.query(OntologySensorRun).all()]
    assert transitions == ["breach", "none"]


def test_recovery_notifies_once(db, monkeypatch):
    from app.services.ontology.sensing import evaluate_sensor

    sensor = _mk_sensor(db)
    q = _patch_query(monkeypatch, [{"qty": 3}])
    fired = _patch_fanout(monkeypatch)
    evaluate_sensor(db, sensor)          # breach fires
    evaluate_sensor(db, sensor)          # persists — no fire
    q.clear(); q.append({"kb_id": "kb1", "sql": ""})
    monkeypatch.setattr(
        "app.services.db.query_service.QueryService.execute",
        lambda self, kb_id, sql, **kw: {"rows": [{"qty": 50}]})
    out = evaluate_sensor(db, sensor)    # recovers — fires once
    assert out["transition"] == "recovery"
    assert len(fired) == 2
    assert "RECOVERED" in fired[1]["summary"]
    evaluate_sensor(db, sensor)          # still fine — no fire
    assert len(fired) == 2


def test_no_breach_never_fires(db, monkeypatch):
    from app.services.ontology.sensing import evaluate_sensor

    sensor = _mk_sensor(db)
    _patch_query(monkeypatch, [{"qty": 99}])
    fired = _patch_fanout(monkeypatch)
    out = evaluate_sensor(db, sensor)
    assert not out["breached"] and out["transition"] == "none"
    assert fired == []


def test_non_numeric_result_is_honest_error(db, monkeypatch):
    from app.services.ontology.sensing import evaluate_sensor

    sensor = _mk_sensor(db)
    _patch_query(monkeypatch, [{"qty": "not-a-number"}])
    _patch_fanout(monkeypatch)
    out = evaluate_sensor(db, sensor)
    assert out["error"] and "not numeric" in out["error"]
    assert out["transition"] == "none"


def test_query_failure_recorded_not_raised(db, monkeypatch):
    from app.services.ontology.sensing import evaluate_sensor

    sensor = _mk_sensor(db)
    _patch_query(monkeypatch, RuntimeError("db down"))
    _patch_fanout(monkeypatch)
    out = evaluate_sensor(db, sensor)
    assert out["error"] and "db down" in out["error"]
    assert out["transition"] == "none"
    runs = db.query(OntologySensorRun).all()
    assert len(runs) == 1 and runs[0].error


def test_all_operators(db, monkeypatch):
    from app.services.ontology.sensing import evaluate_sensor

    for op, value, threshold, expect in [
        ("gt", 15, 10, True), ("gt", 5, 10, False),
        ("gte", 10, 10, True), ("lte", 10, 10, True),
        ("eq", 7, 7, True), ("eq", 8, 7, False),
        ("lt", 3, 10, True), ("lte", 11, 10, False),
    ]:
        sensor = _mk_sensor(db, op=op, threshold=float(threshold),
                            last_state_json=None)
        _patch_query(monkeypatch, [{"qty": value}])
        _patch_fanout(monkeypatch)
        out = evaluate_sensor(db, sensor)
        assert out["breached"] is expect, (op, value, threshold)


# ── scheduling ───────────────────────────────────────────────────────────

def test_run_due_respects_schedule(db, monkeypatch):
    from app.services.ontology.sensing import run_due_sensors

    due = _mk_sensor(db, schedule_seconds=3600)
    not_due = _mk_sensor(db, name="recent", schedule_seconds=3600,
                         last_run_at=None)
    # give not_due a fresh last_run_at
    from datetime import datetime, timedelta, timezone
    not_due.last_run_at = datetime.now(timezone.utc).isoformat()
    db.commit()

    _patch_query(monkeypatch, [{"qty": 3}])
    _patch_fanout(monkeypatch)
    results = run_due_sensors(db)
    assert [r["sensor_id"] for r in results] == [due.id]


def test_run_due_runs_overdue_sensor(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.services.ontology.sensing import run_due_sensors

    overdue = _mk_sensor(db, schedule_seconds=60)
    overdue.last_run_at = (
        datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db.commit()
    _patch_query(monkeypatch, [{"qty": 3}])
    _patch_fanout(monkeypatch)
    results = run_due_sensors(db)
    assert [r["sensor_id"] for r in results] == [overdue.id]


def test_disabled_sensor_skipped(db, monkeypatch):
    from app.services.ontology.sensing import run_due_sensors

    _mk_sensor(db, enabled=False)
    _patch_query(monkeypatch, [{"qty": 3}])
    _patch_fanout(monkeypatch)
    assert run_due_sensors(db) == []
