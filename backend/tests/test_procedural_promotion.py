"""P1-5 procedural memory promotion tests."""
import pytest

from app.config import settings
from app.database import Base, SessionLocal, engine
import app.models  # noqa: F401
from app.models.agent_app import AgentApp
from app.models.agent_memory import AgentMemory
from app.services.procedural_promotion import (
    _cluster,
    _parse_playbook,
    run_procedural_promotion,
)

APP_KEY = "pp-test-app"


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def _agent_app(db):
    if db.query(AgentApp).filter(AgentApp.id == APP_KEY).first() is None:
        db.add(AgentApp(
            id=APP_KEY, name="pp-test-agent", description="d",
            prompt_identity="x", status="published",
            org_id="default-org", app_id="pp-test-app",
        ))
        db.commit()


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mk_case(db, task: str, solution: str) -> AgentMemory:
    from app.services.case_based_reasoning import record_case

    row_id = record_case(db, APP_KEY, task, solution)
    return db.query(AgentMemory).filter(AgentMemory.id == row_id).first()


def _seed_similar_cases(db) -> None:
    _mk_case(db, "export the monthly sales report to xlsx",
             "Queried the sales warehouse then create_artifact xlsx "
             "with aggregated rows for the month under report.")
    _mk_case(db, "export the October sales report to xlsx",
             "Queried the sales warehouse then create_artifact xlsx "
             "with October rows aggregated by product line.")
    _mk_case(db, "export the weekly sales report to xlsx",
             "Queried the sales warehouse then create_artifact xlsx "
             "with the weekly totals for distribution.")


def test_cluster_groups_similar_tasks(db):
    _seed_similar_cases(db)
    cases = db.query(AgentMemory).filter(
        AgentMemory.agent_app_id == APP_KEY,
        AgentMemory.memory_type == "episodic",
    ).all()
    clusters = _cluster(cases)
    big = [c for c in clusters if len(c) >= 3]
    assert big, "similar sales-report cases should cluster together"
    assert len(big[0]) == 3


def test_parse_playbook_from_llm_output():
    raw = "blah\nTASK-PATTERN: export sales reports\nSTEPS: query; pivot; export\n"
    pb = _parse_playbook(raw)
    assert pb.startswith("[PLAYBOOK]")
    assert "TASK-PATTERN" in pb and "STEPS" in pb


@pytest.mark.asyncio
async def test_promotion_distills_cluster(db, monkeypatch):
    _seed_similar_cases(db)

    async def _fake_llm(*args, **kwargs):
        return {"response":
                "TASK-PATTERN: export sales report to xlsx\n"
                "STEPS: query the sales warehouse; aggregate; export artifact"}

    monkeypatch.setattr(
        "app.services.llm_service.call_llm", _fake_llm)
    monkeypatch.setattr(settings, "PROCEDURAL_PROMOTION_ENABLED", True)

    summary = await run_procedural_promotion(db)
    assert summary["playbooks"] >= 1

    pb = (
        db.query(AgentMemory)
        .filter(
            AgentMemory.agent_app_id == APP_KEY,
            AgentMemory.memory_type == "procedural",
        )
        .first()
    )
    assert pb is not None
    assert "[PLAYBOOK]" in pb.content
    assert "STEPS" in pb.content
    # member episodic rows are retired
    remaining = (
        db.query(AgentMemory)
        .filter(
            AgentMemory.agent_app_id == APP_KEY,
            AgentMemory.memory_type == "episodic",
            AgentMemory.is_deleted == False,  # noqa: E712
        )
        .count()
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_promotion_flag_off_is_noop(db, monkeypatch):
    monkeypatch.setattr(settings, "PROCEDURAL_PROMOTION_ENABLED", False)
    summary = await run_procedural_promotion(db)
    assert summary == {"enabled": False, "playbooks": 0}
