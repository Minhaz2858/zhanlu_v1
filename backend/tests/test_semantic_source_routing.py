"""Cross-source retrieval wiring — the "Bound Data Sources" prompt block.

Proves the SEAM (a real ``prepare_data_source_runtime`` call emits the ranked
shortlist), not just the ranking function. The ranking itself is proven on live
data by ``scripts_probe_semantic_store.py``.

Why this matters: with more than one database bound, the agent used to choose
between them from a flat list of names. These tests pin the behaviour that the
choice now arrives with evidence.
"""

import os
import tempfile
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta
from app.services.data_source_runtime import prepare_data_source_runtime
import app.models  # noqa: F401  register all models


@pytest.fixture
def env():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass


def _kb(db, name, tables):
    kb_id = str(uuid.uuid4())
    db.add(
        KnowledgeBase(
            id=kb_id, name=name, source_kind="database", db_type="postgresql",
            database_name=name.lower().replace(" ", "_"), status="active",
        )
    )
    for tname, desc in tables:
        db.add(
            KBTableMeta(
                id=str(uuid.uuid4()), kb_id=kb_id, schema_name="",
                table_name=tname, table_type="TABLE", row_count=10,
                description_en=desc, table_role="fact",
            )
        )
    db.commit()
    return kb_id


def _agent(db, kb_ids):
    agent = AgentApp(
        name="routing-agent", description="t", project="global",
        capabilities=[], model="automatic", agent_type="sequential",
        prompt_identity="", prompt_boundary="", prompt_reasoning="",
        prompt_tools="", prompt_output="", skills=[],
        knowledge_bases=list(kb_ids), topology="standalone", sub_agents=[],
        max_call_count=50, max_retries=3, max_iterations=5,
    )
    db.add(agent)
    db.commit()
    return agent


def test_two_bound_sources_get_a_ranked_match(env):
    erp = _kb(env, "ERP Warehouse", [("sales_order", "sales order fact")])
    hr = _kb(env, "HR Warehouse", [("employee_roster", "employee roster")])
    agent = _agent(env, [erp, hr])

    _, prompt, extras = prepare_data_source_runtime(
        env, agent, base_tools=[], base_system_prompt="BASE",
        user_message="what was total sales order value last month",
    )

    assert "Bound Data Sources" in prompt
    assert "Datasource match for this question" in prompt
    # the matching source is named, the non-matching one is not recommended
    block = prompt.split("Datasource match for this question", 1)[1]
    assert "ERP Warehouse" in block.split("Pick the datasource", 1)[0]
    assert "HR Warehouse" not in block.split("Pick the datasource", 1)[0]
    # the binding still reaches the tool handlers unchanged
    assert set(extras.get("bound_kb_ids") or []) == {erp, hr}


def test_single_bound_source_gets_no_ranking(env):
    erp = _kb(env, "ERP Warehouse", [("sales_order", "sales order fact")])
    agent = _agent(env, [erp])
    _, prompt, _ = prepare_data_source_runtime(
        env, agent, base_tools=[], base_system_prompt="BASE",
        user_message="sales order value",
    )
    assert "Bound Data Sources" in prompt
    # nothing to choose between -> no ranking noise
    assert "Datasource match for this question" not in prompt


def test_no_matching_evidence_adds_nothing(env):
    erp = _kb(env, "ERP Warehouse", [("sales_order", "sales order fact")])
    hr = _kb(env, "HR Warehouse", [("employee_roster", "employee roster")])
    agent = _agent(env, [erp, hr])
    _, prompt, _ = prepare_data_source_runtime(
        env, agent, base_tools=[], base_system_prompt="BASE",
        user_message="please summarise the meeting notes",
    )
    assert "Datasource match for this question" not in prompt


def test_routing_can_be_disabled(env, monkeypatch):
    from app.config import settings

    erp = _kb(env, "ERP Warehouse", [("sales_order", "sales order fact")])
    hr = _kb(env, "HR Warehouse", [("employee_roster", "employee roster")])
    agent = _agent(env, [erp, hr])
    monkeypatch.setattr(settings, "SEMANTIC_SOURCE_ROUTING_ENABLED", False,
                        raising=False)
    _, prompt, _ = prepare_data_source_runtime(
        env, agent, base_tools=[], base_system_prompt="BASE",
        user_message="total sales order value",
    )
    assert "Datasource match for this question" not in prompt
    assert "Bound Data Sources" in prompt


def test_missing_question_is_not_an_error(env):
    erp = _kb(env, "ERP Warehouse", [("sales_order", "sales order fact")])
    hr = _kb(env, "HR Warehouse", [("employee_roster", "employee roster")])
    agent = _agent(env, [erp, hr])
    _, prompt, _ = prepare_data_source_runtime(
        env, agent, base_tools=[], base_system_prompt="BASE", user_message=None,
    )
    assert "Bound Data Sources" in prompt
    assert "Datasource match for this question" not in prompt
