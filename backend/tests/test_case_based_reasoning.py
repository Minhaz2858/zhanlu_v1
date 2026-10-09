"""P1-3 case-based reasoning tests (episodic/procedural memory + recall)."""
import pytest

from app.config import settings
from app.database import Base, SessionLocal, engine
import app.models  # noqa: F401
from app.models.agent_app import AgentApp
from app.models.agent_memory import AgentMemory
from app.services.case_based_reasoning import (
    build_cbr_block,
    find_similar_cases,
    record_case,
)

APP_KEY = "cbr-test-app"


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _agent_app(db):
    existing = db.query(AgentApp).filter(AgentApp.id == APP_KEY).first()
    if existing is None:
        db.add(AgentApp(
            id=APP_KEY, name="cbr-test-agent", description="d",
            prompt_identity="x", status="published",
            org_id="default-org", app_id="cbr-test-app",
        ))
        db.commit()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_record_case_persists_episodic_memory(db):
    row_id = record_case(
        db, APP_KEY,
        "export the October sales report to Excel",
        "Used db_query on the sales warehouse, then create_artifact xlsx "
        "with the aggregated rows attached.",
        project_id=None,
    )
    assert row_id is not None
    row = db.query(AgentMemory).filter(AgentMemory.id == row_id).first()
    assert row.memory_type == "episodic"
    assert row.target == "memory"
    assert "[CASE] TASK:" in row.content
    assert "OUTCOME: success" in row.content


def test_record_case_deduplicates(db):
    args = (
        "reconcile invoices against the ledger monthly",
        "Ran the ledger reconciliation query then pivoted mismatches by "
        "account and exported the result set.",
    )
    first = record_case(db, APP_KEY, *args)
    second = record_case(db, APP_KEY, *args)
    assert first is not None
    assert second is None  # duplicate content rejected


def test_record_case_skips_short_content(db):
    assert record_case(db, APP_KEY, "hi", "ok then") is None


def test_find_similar_cases_ranks_lexically(db, monkeypatch):
    # force lexical scoring (no embedding backend in tests)
    record_case(db, APP_KEY,
                "build a sales forecast for next quarter",
                "Pulled historical revenue, fitted the trend model and "
                "exported the forecast fan chart artifact.")
    record_case(db, APP_KEY,
                "translate the onboarding email to Chinese",
                "Fetched the template, translated with the LLM and saved "
                "the localized copy to the docs library.")
    cases = find_similar_cases(
        db, APP_KEY, "forecast our sales for the coming quarter", k=2)
    assert cases, "expected at least one similar case"
    top = cases[0]["memory"].content
    assert "forecast" in top.lower()


def test_build_cbr_block_formats_cases(db, monkeypatch):
    monkeypatch.setattr(settings, "CBR_RECALL_ENABLED", True)
    record_case(db, APP_KEY,
                "generate the weekly KPI digest",
                "Queried the metrics warehouse, formatted the digest and "
                "delivered it as an artifact.")
    block = build_cbr_block(db, APP_KEY, "create the weekly KPI digest again")
    assert block.startswith("[RELEVANT PAST EXPERIENCE]")
    assert "SOLUTION:" in block


def test_cbr_recall_flag_gates_block(db, monkeypatch):
    monkeypatch.setattr(settings, "CBR_RECALL_ENABLED", False)
    assert build_cbr_block(db, APP_KEY, "anything meaningful here") == ""


def test_legacy_null_memory_type_reads_as_semantic(db):
    row = AgentMemory(
        agent_app_id=APP_KEY, target="memory",
        content="legacy note", char_count=11,
    )
    db.add(row)
    db.commit()
    assert row.memory_type is None  # legacy rows unchanged
