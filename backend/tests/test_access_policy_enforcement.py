"""Enforcement tests — verify the four defense-in-depth layers respect policy.

Covers:
  - list_data_sources  (hides fully-denied KBs, annotates restricted KBs)
  - describe_schema    (rejects denied KBs)
  - execute_query      (rejects denied KB + validates SQL against policy)
  - _apply_schema_policy (filters tables/columns)
"""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import asyncio
import uuid

import pytest

from app.database import Base, engine, SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.models.resource_share import ResourceShare
from app.models.resource_access_policy import ResourceAccessPolicy
from app.services import access_policy_service
from app.services.tool_handlers import db_tools
import app.models  # noqa: F401  register all models


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


def _seed_user(db, role="user"):
    u = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.io",
             full_name="t", role=role, password_hash="x",
             org_id="default-org", app_id="default-app")
    db.add(u)
    db.commit()
    return u


def _seed_kb(db, name="sales_db"):
    kb = KnowledgeBase(name=name, source_kind="database", db_type="sqlite",
                       org_id="default-org", app_id="default-app")
    db.add(kb)
    db.commit()
    return kb


def _add_policy(db, *, user_id, resource_type="project", resource_id,
                kb_id=None, table_name=None, mode="deny",
                column_allowlist=None, row_filter=None):
    # FK: resource_share_id must reference a real share.
    share = ResourceShare(resource_type=resource_type, resource_id=resource_id,
                          shared_with_user_id=user_id, access_level="use",
                          created_by_id=user_id)
    db.add(share)
    db.flush()
    p = ResourceAccessPolicy(
        resource_share_id=share.id,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        kb_id=kb_id,
        table_name=table_name,
        mode=mode,
        column_allowlist=column_allowlist,
        row_filter=row_filter,
    )
    db.add(p)
    db.commit()
    return p


def _ctx(resource_id, kbs):
    return {
        "bound_kb_ids": kbs,
        "resource_type": "project",
        "resource_id": resource_id,
    }


def _run(coro):
    return asyncio.run(coro)


# ── list_data_sources ──────────────────────────────────────────────────


def test_list_data_sources_hides_fully_denied_kb(db):
    user = _seed_user(db)
    kb_allowed = _seed_kb(db, "allowed_db")
    kb_denied = _seed_kb(db, "secret_db")
    rid = str(uuid.uuid4())
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb_denied.id, table_name=None, mode="deny")

    result = _run(db_tools._list_data_sources(
        {}, db, user.id, _ctx(rid, [kb_allowed.id, kb_denied.id])))
    assert result["success"] is True
    ids = [d["id"] for d in result["data_sources"]]
    assert kb_allowed.id in ids
    assert kb_denied.id not in ids


def test_list_data_sources_annotates_restricted_kb(db):
    user = _seed_user(db)
    kb = _seed_kb(db, "restricted_db")
    rid = str(uuid.uuid4())
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name="Secrets", mode="deny")

    result = _run(db_tools._list_data_sources({}, db, user.id, _ctx(rid, [kb.id])))
    assert result["success"] is True
    entry = result["data_sources"][0]
    assert entry["id"] == kb.id
    assert entry.get("restricted") is True
    assert entry.get("blocked_tables") == ["secrets"]


def test_list_data_sources_annotates_whitelist_tables(db):
    user = _seed_user(db)
    kb = _seed_kb(db, "whitelist_db")
    rid = str(uuid.uuid4())
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name=None, mode="deny")
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name="Orders", mode="allow")

    result = _run(db_tools._list_data_sources({}, db, user.id, _ctx(rid, [kb.id])))
    entry = result["data_sources"][0]
    assert entry.get("restricted") is True
    assert entry.get("allowed_tables") == ["orders"]


# ── describe_schema ────────────────────────────────────────────────────


def test_describe_schema_rejects_denied_kb(db):
    user = _seed_user(db)
    kb = _seed_kb(db, "denied_db")
    rid = str(uuid.uuid4())
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name=None, mode="deny")

    result = _run(db_tools._describe_schema(
        {"data_source_id": kb.id}, db, user.id, _ctx(rid, [kb.id])))
    assert result["success"] is False
    assert "restricted" in result["error"]


# ── execute_query ──────────────────────────────────────────────────────


def test_execute_query_rejects_denied_kb(db):
    user = _seed_user(db)
    kb = _seed_kb(db, "denied_db")
    rid = str(uuid.uuid4())
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name=None, mode="deny")

    result = _run(db_tools._execute_query(
        {"data_source_id": kb.id, "sql": "SELECT 1"}, db, user.id,
        _ctx(rid, [kb.id])))
    assert result["success"] is False
    assert "restricted" in result["error"]


def test_execute_query_rejects_denied_table(db):
    user = _seed_user(db)
    kb = _seed_kb(db, "kb")
    rid = str(uuid.uuid4())
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name="Secrets", mode="deny")

    result = _run(db_tools._execute_query(
        {"data_source_id": kb.id, "sql": "SELECT * FROM Secrets"},
        db, user.id, _ctx(rid, [kb.id])))
    assert result["success"] is False
    assert "policy" in result["error"].lower()


def test_execute_query_rejects_unlisted_table_in_whitelist_mode(db):
    user = _seed_user(db)
    kb = _seed_kb(db, "kb")
    rid = str(uuid.uuid4())
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name=None, mode="deny")
    _add_policy(db, user_id=user.id, resource_id=rid,
                kb_id=kb.id, table_name="Orders", mode="allow")

    result = _run(db_tools._execute_query(
        {"data_source_id": kb.id, "sql": "SELECT * FROM Secrets"},
        db, user.id, _ctx(rid, [kb.id])))
    assert result["success"] is False
    assert "policy" in result["error"].lower()


# ── _apply_schema_policy (pure filter) ─────────────────────────────────


def _mk_policy(denied=None, allowed=None, columns=None):
    return access_policy_service.ResolvedPolicy(
        denied_tables={(k, t): "deny" for (k, t) in (denied or [])},
        allowlisted_kbs={k: set(v) for k, v in (allowed or {}).items()},
        column_allowlists={(k, t): cols for (k, t), cols in (columns or {}).items()},
        has_policies=True,
    )


def test_apply_schema_policy_filters_list_tables():
    pol = _mk_policy(denied=[("kb1", "Secrets")])
    out = db_tools._apply_schema_policy(
        {"tables": ["Orders", "Secrets", "Customers"]}, pol, "kb1", None)
    assert out["tables"] == ["Orders", "Customers"]


def test_apply_schema_policy_whitelist_mode_filters_list_tables():
    pol = _mk_policy(allowed={"kb1": {"orders"}})
    out = db_tools._apply_schema_policy(
        {"tables": ["Orders", "Secrets"]}, pol, "kb1", None)
    assert out["tables"] == ["Orders"]


def test_apply_schema_policy_filters_describe_all_columns():
    pol = _mk_policy(columns={("kb1", "orders"): ["id"]})
    out = db_tools._apply_schema_policy(
        {"tables": [
            {"table": "Orders", "columns": [{"name": "id"}, {"name": "amount"}]},
            {"table": "Secrets", "columns": [{"name": "token"}]},
        ]},
        pol, "kb1", None)
    # Secrets is not restricted (no deny), so stays; Orders columns filtered.
    orders = next(t for t in out["tables"] if t["table"] == "Orders")
    assert [c["name"] for c in orders["columns"]] == ["id"]


def test_apply_schema_policy_raises_on_denied_table_describe():
    pol = _mk_policy(denied=[("kb1", "Secrets")])
    with pytest.raises(ValueError):
        db_tools._apply_schema_policy(
            {"table": "Secrets", "columns": [{"name": "token"}]}, pol, "kb1", "Secrets")


def test_apply_schema_policy_noop_without_policies():
    pol = access_policy_service.ResolvedPolicy(has_policies=False)
    result = {"tables": ["Orders", "Secrets"]}
    out = db_tools._apply_schema_policy(result, pol, "kb1", None)
    assert out == result  # unchanged
