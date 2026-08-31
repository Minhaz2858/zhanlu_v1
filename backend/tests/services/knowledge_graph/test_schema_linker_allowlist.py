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
    settings.SCHEMA_LINKER_ALLOWLIST_ENABLED = True
    settings.PROJECT_KNOWLEDGE_AGENT_NAMES = ["data_agent"]
    settings.SCHEMA_LINKER_TABLE_ALLOWLIST = [
        "erp_v_sale_orderentry",
        "erp_product_sales_details",
    ]


def test_resolve_returns_none_when_flag_off(db):
    settings.SCHEMA_LINKER_ALLOWLIST_ENABLED = False
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
        name="data_agent",
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


def test_resolve_uses_project_scoped_binding(db):
    """Binding is project-scoped: an Ecisco agent whose project owns the
    KB triggers the allowlist even when the agent's JSON `knowledge_bases`
    column is empty (this deployment stores bindings on the KB row)."""
    _enable()
    from app.models.agent_app import AgentApp
    from app.models.knowledge_base import KnowledgeBase
    agent_id = str(uuid.uuid4())
    kb_id = "kb_proj_" + str(uuid.uuid4())
    db.add(AgentApp(
        id=agent_id,
        name="data_agent",
        project_id="p_ecisco",
        knowledge_bases=[],  # empty JSON column — must still resolve
    ))
    db.add(KnowledgeBase(
        id=kb_id,
        name="aipdp_data_warehouse_prod",
        project_id="p_ecisco",
    ))
    db.commit()
    result = _resolve_table_allowlist_for_kb(db, [kb_id])
    assert result is not None
    assert "erp_v_sale_orderentry" in result


def test_resolve_ignores_kbs_from_other_projects(db):
    """A KB belonging to a different project must NOT trigger the Ecisco
    allowlist even if the Ecisco agent row exists."""
    _enable()
    from app.models.agent_app import AgentApp
    from app.models.knowledge_base import KnowledgeBase
    agent_id = str(uuid.uuid4())
    kb_id = "kb_other_proj_" + str(uuid.uuid4())
    db.add(AgentApp(
        id=agent_id,
        name="data_agent",
        project_id="p_ecisco",
        knowledge_bases=[],
    ))
    db.add(KnowledgeBase(
        id=kb_id,
        name="some_other_db",
        project_id="p_unrelated",
    ))
    db.commit()
    result = _resolve_table_allowlist_for_kb(db, [kb_id])
    assert result is None


def test_build_full_toc_filters_to_allowlist(db):
    """The TOC the LLM sees must be restricted to allowlisted tables when
    the flag is on — stale shadow tables must not appear."""
    _enable()
    from app.models.agent_app import AgentApp
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta
    from app.services.knowledge_graph.schema_linker import build_full_toc

    kb_id = "kb_toc_" + str(uuid.uuid4())
    db.add(AgentApp(
        id=str(uuid.uuid4()),
        name="data_agent",
        project_id="p_ecisco",
        knowledge_bases=[],
    ))
    db.add(KnowledgeBase(id=kb_id, name="wh", project_id="p_ecisco"))
    for tname in ["erp_v_sale_orderentry", "erp_paez_t_lz_price", "actual_price"]:
        db.add(KBTableMeta(
            id=str(uuid.uuid4()),
            kb_id=kb_id,
            table_name=tname,
            schema_name="wh",
            table_type="BASE TABLE",
        ))
    db.commit()

    toc = build_full_toc(db, [kb_id])
    names = [t["table_name"] for t in toc]
    assert "erp_v_sale_orderentry" in names
    # NOTE: "actual_price" IS in the reference app's 35-table allowlist;
    # the meaningful guard is the stale shadow table being excluded.
    assert "erp_paez_t_lz_price" not in names  # stale price table excluded


def test_build_full_toc_unfiltered_when_flag_off(db):
    """With the flag off the TOC shows every catalogued table."""
    settings.SCHEMA_LINKER_ALLOWLIST_ENABLED = False
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta
    from app.services.knowledge_graph.schema_linker import build_full_toc

    kb_id = "kb_toc_off_" + str(uuid.uuid4())
    db.add(KnowledgeBase(id=kb_id, name="wh", project_id="p_x"))
    for tname in ["a_table", "b_table", "c_table"]:
        db.add(KBTableMeta(
            id=str(uuid.uuid4()),
            kb_id=kb_id,
            table_name=tname,
            schema_name="wh",
            table_type="BASE TABLE",
        ))
    db.commit()

    toc = build_full_toc(db, [kb_id])
    assert len(toc) == 3


def test_get_selected_tables_ddl_filters_non_allowlisted(db):
    """Even if the LLM asks for a shadow table, its DDL must be withheld."""
    _enable()
    from app.models.agent_app import AgentApp
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta, KBColumnMeta
    from app.services.knowledge_graph.schema_linker import get_selected_tables_ddl

    kb_id = "kb_ddl_" + str(uuid.uuid4())
    db.add(AgentApp(
        id=str(uuid.uuid4()),
        name="data_agent",
        project_id="p_ecisco",
        knowledge_bases=[],
    ))
    db.add(KnowledgeBase(id=kb_id, name="wh", project_id="p_ecisco"))
    meta_sales = KBTableMeta(
        id=str(uuid.uuid4()), kb_id=kb_id, table_name="erp_v_sale_orderentry",
        schema_name="wh", table_type="VIEW",
    )
    meta_stale = KBTableMeta(
        id=str(uuid.uuid4()), kb_id=kb_id, table_name="erp_paez_t_lz_price",
        schema_name="wh", table_type="BASE TABLE",
    )
    db.add_all([meta_sales, meta_stale])
    db.add(KBColumnMeta(
        id=str(uuid.uuid4()), table_meta_id=meta_sales.id,
        column_name="FAMOUNT", data_type="decimal", ordinal=1,
    ))
    db.add(KBColumnMeta(
        id=str(uuid.uuid4()), table_meta_id=meta_stale.id,
        column_name="F_PAEZ_DATE", data_type="datetime", ordinal=1,
    ))
    db.commit()

    ddl = get_selected_tables_ddl(
        db, [kb_id], ["erp_v_sale_orderentry", "erp_paez_t_lz_price"]
    )
    assert "erp_v_sale_orderentry" in ddl
    assert "erp_paez_t_lz_price" not in ddl
    assert "FAMOUNT" in ddl
    assert "F_PAEZ_DATE" not in ddl