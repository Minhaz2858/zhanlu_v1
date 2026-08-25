"""Unit tests for the schema-linker allowlist (Ecisco BI 2026-08-25)."""
from __future__ import annotations

import uuid
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.services.knowledge_graph.schema_linker import (
    _resolve_table_allowlist_for_kb,
)


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _enable():
    settings.ECISCO_BI_SCHEMA_LINKER_ALLOWLIST_ENABLED = True
    settings.ECISCO_BI_AGENT_NAMES = ["ecisco_bi_assistant"]
    settings.ECISCO_BI_SCHEMA_LINKER_TABLE_ALLOWLIST = [
        "erp_v_sale_orderentry",
        "erp_product_sales_details",
    ]


def test_resolve_returns_none_when_flag_off(db):
    settings.ECISCO_BI_SCHEMA_LINKER_ALLOWLIST_ENABLED = False
    result = _resolve_table_allowlist_for_kb(db, ["some-kb-id"])
    assert result is None


def test_resolve_returns_none_for_empty_kb_list(db):
    _enable()
    result = _resolve_table_allowlist_for_kb(db, [])
    assert result is None


def test_resolve_returns_allowlist_when_bound_to_ecisco_agent(db):
    """If any AgentApp named in ECISCO_BI_AGENT_NAMES is bound to one of
    the requested kb_ids, the allowlist is returned."""
    _enable()
    from app.models.agent_app import AgentApp
    agent_id = str(uuid.uuid4())
    kb_id = "kb_test_" + str(uuid.uuid4())
    db.add(AgentApp(
        id=agent_id,
        name="ecisco_bi_assistant",
        project_id="p_ecisco",
        knowledge_bases=[kb_id],
    ))
    db.commit()
    result = _resolve_table_allowlist_for_kb(db, [kb_id])
    assert result is not None
    assert "erp_v_sale_orderentry" in result


def test_resolve_returns_none_for_non_ecisco_agent(db):
    """A non-Ecisco-BI AgentApp bound to the KB should NOT trigger the
    allowlist (would constrain other projects)."""
    _enable()
    from app.models.agent_app import AgentApp
    agent_id = str(uuid.uuid4())
    kb_id = "kb_other_" + str(uuid.uuid4())
    db.add(AgentApp(
        id=agent_id,
        name="some_other_agent",
        project_id="p_other",
        knowledge_bases=[kb_id],
    ))
    db.commit()
    result = _resolve_table_allowlist_for_kb(db, [kb_id])
    assert result is None
