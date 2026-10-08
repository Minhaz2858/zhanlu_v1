"""Join-partner guarantee — OCI "Table Retrieval" hardening (2026-09-22).

Measured failure mode on non-Ecisco warehouses: "sales by customer" retrieves
only the sales table; the customer table never enters the DDL slice; the model
invents a join to a table it never saw (or filters on a column that does not
exist) and the turn fails or fabricates. When the catalog knows a relation for
a selected table but its partner was NOT selected, the partner is pulled into
the selection before SQL generation.

Pins: in-set partners mean no expansion; the highest-confidence partner wins;
the added-partner cap; below-threshold relations are ignored; sources with no
relations at all are left alone; any DB error degrades to the unchanged
selection; single-table selections DO get a partner (that is the main case).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import TimestampedBase
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, KBTableRelation
from app.services.db.join_partner import ensure_join_partners


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
    kb = KnowledgeBase(id="kb-1", name="w", source_kind="database", is_deleted=False)
    session.add(kb)
    session.flush()

    def table(name):
        t = KBTableMeta(kb_id="kb-1", table_name=name, table_type="TABLE")
        session.add(t)
        session.flush()
        return t

    def relation(src, dst, confidence=1.0, rtype="FK"):
        session.add(
            KBTableRelation(
                kb_id="kb-1",
                source_table_meta_id=src.id,
                target_table_meta_id=dst.id,
                relation_type=rtype,
                confidence=confidence,
            )
        )
        session.flush()

    t = {
        n: table(n)
        for n in ("orders", "customers", "products", "logs", "orphan")
    }
    relation(t["orders"], t["customers"], 1.0)
    relation(t["orders"], t["products"], 0.9)
    # A weak NAME_MATCH edge — below the default confidence floor.
    relation(t["logs"], t["orders"], 0.3, rtype="NAME_MATCH")
    # 'orphan' has no relations at all — nothing can be done for it.
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_both_selected_keeps_untouched(db):
    out, added = ensure_join_partners(db, "kb-1", ["orders", "customers"])
    assert out == ["orders", "customers"]
    assert added == []


def test_missing_partner_is_pulled_in(db):
    out, added = ensure_join_partners(db, "kb-1", ["orders"])
    assert "customers" in out, "the FK partner must join the selection"
    assert added == ["customers"]


def test_highest_confidence_partner_wins(db):
    out, added = ensure_join_partners(db, "kb-1", ["orders"])
    assert added == ["customers"], "confidence 1.0 beats 0.9"


def test_added_partner_cap(db):
    out, added = ensure_join_partners(db, "kb-1", ["orders"], max_added=1)
    assert len(added) == 1


def test_low_confidence_relations_ignored(db):
    out, added = ensure_join_partners(db, "kb-1", ["logs"], min_confidence=0.6)
    assert added == []


def test_weak_edge_used_when_caller_lowers_floor(db):
    """min_confidence is a hard, caller-tunable floor: with 0.2 the 0.3
    NAME_MATCH edge becomes eligible and beats no partner at all."""
    out, added = ensure_join_partners(db, "kb-1", ["logs"], min_confidence=0.2)
    assert added == ["orders"]


def test_table_without_any_relations_left_alone(db):
    out, added = ensure_join_partners(db, "kb-1", ["orphan"])
    assert out == ["orphan"]
    assert added == []


def test_single_selected_table_still_gets_partner(db):
    """The most common failure is exactly ONE table selected — the guarantee
    must not bail out on single-table selections."""
    out, added = ensure_join_partners(db, "kb-1", ["orders"])
    assert "customers" in out
    assert added == ["customers"]


def test_db_error_degrades_to_unchanged():
    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("boom")

    out, added = ensure_join_partners(Boom(), "kb-1", ["orders", "customers"])
    assert out == ["orders", "customers"]
    assert added == []


def test_empty_and_garbage_inputs(db):
    assert ensure_join_partners(db, "kb-1", []) == ([], [])
    assert ensure_join_partners(db, "", ["orders"])[0] == ["orders"]
    assert ensure_join_partners(db, "kb-1", ["orders", "", None])[0][0] == "orders"
