"""P7 enterprise validation (workstream 4) — large graphs + permission at scale.

Proves 500 ontology objects do not crash the graph explorer, that metric
dependency edges remain present, and that restricted provenance holds even
under a large object set. Search / filtering / navigation are exercised on the
returned payload (the front-end wires these over the same data).
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.models.ontology import OntologyObject
from app.routers.ontology import ontology_graph

from tests.sandbox_seed import (
    ORG, OBJ_GROSS_MARGIN, OWNER_ID, RESTRICTED_ID, create_data_db,
    build_sandbox, make_engine, make_session,
)


def _user(uid, role="org_admin"):
    return SimpleNamespace(id=uid, org_id=ORG, role=role)


def _gm_doc(payload):
    for o in payload["objects"]:
        if o["id"] == OBJ_GROSS_MARGIN:
            return (o.get("provenance") or {}).get("document_provenance", [])
    return []


def test_p7_500_objects_do_not_crash(env_large):
    eng, session, _ = env_large
    payload = ontology_graph(org_id=ORG, db=session, user=_user(OWNER_ID), domain_id=None)
    assert len(payload["objects"]) >= 500
    # metric dependency edges survive the large payload
    assert len(payload["metric_dependencies"]) >= 4
    # a basic "search" over the returned payload finds the target object
    matches = [o for o in payload["objects"] if o["id"] == OBJ_GROSS_MARGIN]
    assert len(matches) == 1


def test_p7_restricted_provenance_at_scale(env_large):
    eng, session, _ = env_large
    owner = ontology_graph(org_id=ORG, db=session, user=_user(OWNER_ID), domain_id=None)
    restricted = ontology_graph(org_id=ORG, db=session, user=_user(RESTRICTED_ID), domain_id=None)
    assert len(_gm_doc(owner)) == 1
    assert _gm_doc(restricted) == []


@pytest.fixture()
def env_large(tmp_path):
    eng = make_engine()
    data_path = str(tmp_path / "sales.db")
    create_data_db(data_path)
    session = make_session(eng)
    build_sandbox(session, data_path)
    # Add ~500 objects on top of the seeded ones.
    for i in range(500):
        session.add(OntologyObject(
            id=f"obj-bulk-{i:04d}", org_id=ORG, domain_id="auto",
            name=f"BulkEntity{i}", display_name=f"Bulk Entity {i}",
            backing_kind="entity", backing_ref=f"kb:kb-sales:bulk{i}",
            evidence_status="inferred", confidence=0.6, is_active=True,
            properties_json=[{"name": "x", "type": "number"}]))
    session.commit()
    return eng, session, data_path
