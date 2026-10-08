"""Coverage expansion + self-check — the 25-vs-215 failure (2026-09-22).

Measured live: "How many contracts expire this month?" was answered with 25
(purchase contracts only) while the warehouse held 215 (190 sales contracts
in erp_t_crm_contract). Table retrieval recalled ONE contract table; nothing
in the architecture asked "are there OTHER contract tables?".

Pins:
- entity tokens: prefix noise (erp/t/v/bd) dropped, domain parts kept —
  erp_t_crm_contract and erp_t_pur_contract share the token 'contract';
- selection expansion: a NON-selected catalog table sharing an entity token
  with a selected table is pulled in (bounded, scored candidates preferred);
- uncovered siblings: tables the SQL never touched but that share an entity
  token with the touched tables — the self-check's target list.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import TimestampedBase
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta
from app.services.db.coverage import (
    entity_tokens,
    expand_entity_selection,
    find_uncovered_siblings,
)


def test_entity_tokens_drop_prefix_noise():
    assert entity_tokens("erp_t_crm_contract") >= {"crm", "contract"}
    assert "erp" not in entity_tokens("erp_t_crm_contract")
    assert "t" not in entity_tokens("erp_t_crm_contract")


def test_entity_tokens_share_contract():
    a = entity_tokens("erp_t_crm_contract")
    b = entity_tokens("erp_t_pur_contract")
    assert a & b, "both contract tables must share the 'contract' token"


def test_entity_tokens_generic_names_degrade_to_self():
    assert entity_tokens("material") == {"material"}


# ── selection expansion ───────────────────────────────────────────────────


def test_expansion_pulls_entity_sibling():
    scored = [
        ("erp_t_pur_contract", 5.0),
        ("erp_t_crm_contract", 4.0),
        ("erp_t_bd_lotmaster", 0.2),
    ]
    out, added = expand_entity_selection(
        None, "kb-1", ["erp_t_pur_contract"], scored_candidates=scored
    )
    assert "erp_t_crm_contract" in out
    assert added == ["erp_t_crm_contract"]
    assert "erp_t_bd_lotmaster" not in out


def test_expansion_prefers_higher_scored_sibling():
    scored = [
        ("erp_t_pur_contract", 5.0),
        ("erp_t_crm_contract", 4.0),
        ("erp_v_contract_execution", 4.5),
    ]
    out, added = expand_entity_selection(
        None, "kb-1", ["erp_t_pur_contract"], scored_candidates=scored,
        max_added=1,
    )
    assert added == ["erp_v_contract_execution"], "4.5 beats 4.0"


def test_expansion_cap():
    scored = [
        ("erp_t_pur_contract", 5.0),
        ("erp_t_crm_contract", 4.0),
        ("erp_v_contract_execution", 4.5),
        ("erp_t_crm_contractentry", 4.2),
    ]
    out, added = expand_entity_selection(
        None, "kb-1", ["erp_t_pur_contract"], scored_candidates=scored,
        max_added=2,
    )
    assert len(added) == 2


def test_expansion_noop_when_all_selected():
    scored = [("a_contract", 5.0), ("b_contract", 4.0)]
    out, added = expand_entity_selection(None, "kb-1", ["a_contract", "b_contract"],
                                         scored_candidates=scored)
    assert added == []


def test_expansion_garbage_inputs():
    assert expand_entity_selection(None, "kb-1", []) == ([], [])
    assert expand_entity_selection(None, "", ["a_contract"])[0] == ["a_contract"]


# ── uncovered siblings (self-check target list) ───────────────────────────


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
    session.add(KnowledgeBase(id="kb-1", name="w", source_kind="database"))
    session.flush()
    for name in (
        "erp_t_crm_contract", "erp_t_crm_contractentry",
        "erp_t_pur_contract", "erp_t_pur_contractentry",
        "erp_v_contract_execution", "erp_t_bd_lotmaster",
    ):
        session.add(KBTableMeta(kb_id="kb-1", table_name=name, table_type="TABLE"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_uncovered_siblings_found(db):
    sibs = find_uncovered_siblings(db, "kb-1", ["erp_t_pur_contract"], max_tables=3)
    assert "erp_t_crm_contract" in sibs
    assert "erp_v_contract_execution" in sibs
    assert "erp_t_pur_contract" not in sibs
    assert "erp_t_bd_lotmaster" not in sibs, "unrelated tables are not siblings"


def test_uncovered_siblings_capped(db):
    sibs = find_uncovered_siblings(db, "kb-1", ["erp_t_pur_contract"], max_tables=2)
    assert len(sibs) <= 2


def test_uncovered_siblings_fail_soft():
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("boom")

    assert find_uncovered_siblings(Boom(), "kb-1", ["x_contract"]) == []

def test_same_family_details_excluded(db):
    """crm_contractentry/-f are header/detail extensions of the touched
    erp_t_crm_contract — they must NOT crowd out the cross-family sibling
    erp_t_pur_contract (the measured 25-vs-215 failure)."""
    sibs = find_uncovered_siblings(db, "kb-1", ["erp_t_crm_contract"], max_tables=5)
    assert "erp_t_pur_contract" in sibs
    assert "erp_t_crm_contractentry" not in sibs
    assert "erp_t_crm_contractentry_f" not in sibs
