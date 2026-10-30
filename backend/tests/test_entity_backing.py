"""P5-T4 — entity backing over CONFIRMED identity crosswalks only.

Invariant I5: per-source records (each side keeps its own identifiers and
rows), no destructive merge; inferred/rejected matches are NEVER promoted
at runtime; cross-org matches are invisible.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.identity import FileTableRow, IdentityMatch
from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import OntologyDomain, OntologyObject

ORG = "org-ent"
OTHER = "org-ent-other"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        eng,
        tables=[KnowledgeBase.__table__, OntologyDomain.__table__,
                OntologyObject.__table__, IdentityMatch.__table__,
                FileTableRow.__table__],
    )
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


@pytest.fixture()
def env(db, tmp_path):
    db_path = tmp_path / "crm.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO customers VALUES (?, ?)",
                     [(1, "Acme"), (2, "Globex")])
    conn.commit()
    conn.close()

    kb = KnowledgeBase(org_id=ORG, name="crm", source_kind="database",
                       db_type="sqlite", api_url=str(db_path))
    kb.id = "kb-crm"
    db.add(kb)
    fkb = KnowledgeBase(org_id=ORG, name="roster", source_kind="file",
                        file_type="xlsx", file_url="/api/uploads/roster.xlsx")
    fkb.id = "kb-file"
    db.add(fkb)
    db.flush()
    for vals in ({"cust_no": "C1", "name": "Acme"},
                 {"cust_no": "C2", "name": "Globex"}):
        db.add(FileTableRow(kb_id=fkb.id, sheet_name="Sheet1", row_idx=0,
                            values_json=vals))
    domain = OntologyDomain(org_id=ORG, code="ent", name="Ent")
    db.add(domain)
    db.flush()
    obj = OntologyObject(org_id=ORG, domain_id=domain.id, name="Customer",
                         backing_kind="entity", backing_ref="kb:kb-crm:customers",
                         evidence_status="verified")
    db.add(obj)
    db.commit()
    return {"kb": kb, "fkb": fkb, "obj": obj}


def _match(db, obj, *, status="confirmed", org=ORG, pk_a="1", pk_b="C1"):
    m = IdentityMatch(
        id=str(uuid.uuid4()), org_id=org, object_id=obj.id,
        source_a={"kind": "db", "kb_id": "kb-crm", "table": "customers",
                  "column": "id"},
        source_b={"kind": "file", "kb_id": "kb-file", "sheet": "Sheet1",
                  "column": "cust_no"},
        source_a_key=f"db:kb-crm:customers",
        source_b_key=f"file:kb-file:Sheet1",
        pk_a=pk_a, pk_b=pk_b, match_tier="exact", status=status,
    )
    db.add(m)
    db.commit()
    return m


def test_confirmed_crosswalk_returns_per_source_records(db, env):
    from app.services.ontology.data import read_object_data

    _match(db, env["obj"], status="confirmed", pk_a="1", pk_b="C1")
    out = read_object_data(db, ORG, env["obj"])
    assert out["success"] is True
    assert out["backing_kind"] == "entity"
    groups = out["crosswalk_groups"]
    assert len(groups) == 1 and groups[0]["confirmed_pairs"] == 1
    side_a = groups[0]["side_a"]
    side_b = groups[0]["side_b"]
    assert side_a["kind"] == "db" and side_b["kind"] == "file"
    assert any(r.get("id") == 1 for r in side_a["rows"])   # real db row
    assert any(r.get("cust_no") == "C1" for r in side_b["rows"])  # staged row
    # each side keeps its OWN identifiers — no synthetic merged record
    assert side_a["pk_column"] == "id" and side_b["pk_column"] == "cust_no"


def test_inferred_only_is_typed_unavailable(db, env):
    from app.services.ontology.data import read_object_data

    _match(db, env["obj"], status="inferred")
    out = read_object_data(db, ORG, env["obj"])
    assert out["success"] is False and out["unavailable"] is True
    assert "CONFIRMED" in out["reason"]
    assert "rows" not in out


def test_cross_org_confirmed_match_invisible(db, env):
    from app.services.ontology.data import read_object_data

    _match(db, env["obj"], status="confirmed", org=OTHER)
    out = read_object_data(db, ORG, env["obj"])
    assert out["success"] is False and out["unavailable"] is True


def test_no_destructive_merge_two_pairs_two_sides(db, env):
    from app.services.ontology.data import read_object_data

    _match(db, env["obj"], status="confirmed", pk_a="1", pk_b="C1")
    _match(db, env["obj"], status="confirmed", pk_a="2", pk_b="C2")
    out = read_object_data(db, ORG, env["obj"])
    groups = out["crosswalk_groups"]
    # both pairs land in ONE source-pair group; each side lists BOTH rows
    assert len(groups) == 1 and groups[0]["confirmed_pairs"] == 2
    assert {r["id"] for r in groups[0]["side_a"]["rows"]} == {1, 2}
    assert {r["cust_no"] for r in groups[0]["side_b"]["rows"]} == {"C1", "C2"}
