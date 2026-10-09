"""P0-1 → P2-5 NL2SQL OCI-parity batch tests (2026-10-09)."""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine, get_db  # noqa: E402
from app.deps import get_current_user_required  # noqa: E402
import app.models  # noqa: F401, E402
from app.models.query_workload_stat import QueryWorkloadStat  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.db.semantic_store import SemanticStore  # noqa: E402


KB = "kb-oci-p2"


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def _mined(db):
    # idempotent: the (kb_id, stat_key) unique constraint forbids re-seeding
    db.query(QueryWorkloadStat).filter(
        QueryWorkloadStat.kb_id == KB).delete()
    db.add(QueryWorkloadStat(
        kb_id=KB, stat_kind="table_pair", table_name="sales",
        peer="customers", occurrences=5, stat_key="pair-s-c",
    ))
    db.add(QueryWorkloadStat(
        kb_id=KB, stat_kind="table_pair", table_name="sales",
        peer="regions", occurrences=1, stat_key="pair-s-r",
    ))
    db.add(QueryWorkloadStat(
        kb_id=KB, stat_kind="table_filter", table_name="sales",
        detail={"filter": "status = 'open'"}, occurrences=3,
        stat_key="filt-s-open",
    ))
    db.commit()


# ── P0-1: mined join/filter stats consumed ───────────────────────────────

def test_join_hints_min_occurrence(db, _mined):
    store = SemanticStore(db, [KB])
    hints = store.join_hints(min_occurrences=2)
    assert len(hints) == 1
    # pairs are normalized (sorted) by the store
    assert {hints[0]["table_a"], hints[0]["table_b"]} == {"sales", "customers"}
    assert hints[0]["occurrences"] == 5


def test_common_filters_for_selected_tables(db, _mined):
    store = SemanticStore(db, [KB])
    filters = store.common_filters(["sales"], k=6)
    assert filters and filters[0]["filter"] == "status = 'open'"
    assert filters[0]["occurrences"] == 3
    # unrelated tables → nothing
    assert store.common_filters(["other_table"]) == []


def test_workload_hints_prompt_block(db, _mined):
    store = SemanticStore(db, [KB])
    block = store.workload_hints_for_tables(["sales", "customers"])
    assert block.startswith("[WORKLOAD HINTS")
    assert "⋈" in block and "customers" in block and "sales" in block
    assert "status = 'open'" in block
    # a table set with no mined pair → no join section (fresh-warehouse clean)
    block2 = store.workload_hints_for_tables(["other"])
    assert block2 == "" or "⋈" not in block2


def test_kb_scoping_blocks_other_kb(db, _mined):
    store = SemanticStore(db, ["kb-other"])
    assert store.join_hints() == []


# ── P0-2 / P1-4: activated flags ─────────────────────────────────────────

def test_clarify_and_planner_flags_active():
    assert settings.NL2SQL_PLANNER_ENABLED is True
    assert settings.NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED is True


def test_promotion_and_chart_flags_active():
    assert settings.NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED is True
    assert settings.NL2SQL_CHART_SUGGESTION_ENABLED is True


def test_workload_hints_flag_default_on():
    assert settings.NL2SQL_WORKLOAD_HINTS_ENABLED is True


# ── P1-3: terminology authoring API ──────────────────────────────────────

@pytest.fixture
def client(db):
    from app.routers.terminologies import register_terminologies_router

    app = FastAPI()
    app.include_router(register_terminologies_router())
    admin = db.query(User).filter_by(email="admin@zhanlu.dev").first()
    if admin is None:
        admin = User(
            email="admin@zhanlu.dev", full_name="Admin", password_hash="x",
            role="admin", org_id="default-org",
        )
        db.add(admin)
        db.commit()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_required] = lambda: admin
    return TestClient(app)


def test_terminology_authoring_roundtrip(client):
    ds = "ds-oci-p2"
    r = client.post(
        f"/datasources/{ds}/terminologies",
        json={"word": "ARR", "description": "Annual Recurring Revenue"},
    )
    assert r.status_code == 200
    entry = r.json()
    assert entry["word"] == "ARR"

    # list shows it
    r2 = client.get(f"/datasources/{ds}/terminologies")
    words = [x["word"] for x in r2.json()]
    assert "ARR" in words

    # patch description
    r3 = client.patch(
        f"/datasources/{ds}/terminologies/{entry['id']}",
        json={"description": "ARR = sum of annualized subscriptions"},
    )
    assert r3.status_code == 200
    assert "annualized" in r3.json()["description"]

    # delete
    r4 = client.delete(f"/datasources/{ds}/terminologies/{entry['id']}")
    assert r4.status_code == 200
    r5 = client.get(f"/datasources/{ds}/terminologies")
    assert "ARR" not in [x["word"] for x in r5.json()]


def test_terminology_upsert_requires_word(client):
    r = client.post("/datasources/ds-x/terminologies", json={"word": ""})
    assert r.status_code == 400


def test_terminology_update_wrong_ds_404(client):
    r = client.post(
        "/datasources/ds-a/terminologies",
        json={"word": "Churn", "description": "customer loss rate"},
    )
    entry = r.json()
    r2 = client.patch(
        f"/datasources/ds-other/terminologies/{entry['id']}",
        json={"description": "x"},
    )
    assert r2.status_code == 404


# ── P2-5: execution feedback ─────────────────────────────────────────────

def test_assess_scan_disabled_by_default(db):
    from app.services.db.execution_feedback import assess_scan

    assert assess_scan(db, "SELECT 1") is None


def test_assess_scan_skips_sqlite_even_when_enabled(db, monkeypatch):
    from app.services.db.execution_feedback import assess_scan

    monkeypatch.setattr(settings, "NL2SQL_EXPLAIN_FEEDBACK_ENABLED", True)
    # the test bind is sqlite — EXPLAIN row estimates unsupported → None
    assert assess_scan(db, "SELECT 1") is None


def test_persist_rewrite_outcome_disabled(db):
    from app.services.db.execution_feedback import persist_rewrite_outcome

    assert persist_rewrite_outcome(db, KB, "SELECT 1") is False


def test_persist_rewrite_outcome_writes_history(db, monkeypatch):
    from app.services.db.execution_feedback import persist_rewrite_outcome

    monkeypatch.setattr(settings, "NL2SQL_EXPLAIN_FEEDBACK_ENABLED", True)
    ok = persist_rewrite_outcome(
        db, KB, "SELECT * FROM sales",
        rules_applied=["limit_added"], estimated_rows=42,
    )
    assert ok is True
    row = (
        db.query(QueryWorkloadStat)
        .filter(
            QueryWorkloadStat.kb_id == KB,
            QueryWorkloadStat.stat_kind == "rewrite_outcome",
        )
        .first()
    )
    assert row is not None
    assert row.table_name == "sales"
    assert row.detail["estimated_rows"] == 42
    assert "limit_added" in row.detail["rules"]


def test_mysql_estimate_parsing():
    from app.services.db.execution_feedback import _parse_estimate

    # mappings keyed like real MySQL EXPLAIN output
    raw = [{"id": 1, "select_type": "SIMPLE", "rows": 5000},
           {"id": 1, "select_type": "SIMPLE", "rows": 10}]
    assert _parse_estimate("mysql", raw) == 5000
    assert _parse_estimate("mysql", []) is None
    assert _parse_estimate("mysql", [{"rows": None}]) is None
