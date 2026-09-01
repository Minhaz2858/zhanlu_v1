"""Unit tests for access_policy_service policy resolution + SQL validation."""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import uuid

import pytest

from app.database import Base, engine, SessionLocal
from app.models.user import User
from app.models.resource_share import ResourceShare
from app.models.resource_access_policy import ResourceAccessPolicy
from app.services.access_policy_service import (
    ResolvedPolicy,
    resolve_user_policies,
    validate_sql_against_policy,
)
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


_RES = {"resource_type": "project", "resource_id": "r1"}


def _add_policy(db, *, user_id, resource_type="project", resource_id=_RES["resource_id"],
                kb_id=None, table_name=None, mode="deny",
                column_allowlist=None, row_filter=None):
    rid = resource_id
    # FK: resource_share_id must reference a real share.
    share = ResourceShare(resource_type=resource_type, resource_id=rid,
                          shared_with_user_id=user_id, access_level="use",
                          created_by_id=user_id)
    db.add(share)
    db.flush()
    p = ResourceAccessPolicy(
        resource_share_id=share.id,
        resource_type=resource_type,
        resource_id=rid,
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


def _resolve(db, user, kbs, **kw):
    return resolve_user_policies(
        db, user_id=user.id, resource_type="project",
        resource_id=_RES["resource_id"], bound_kb_ids=kbs, **kw,
    )


def test_default_allow_when_no_policies(db):
    user = _seed_user(db)
    kb = "kb1"
    pol = _resolve(db, user, [kb])
    assert pol.has_policies is False
    assert pol.blocked_kb_ids == set()
    assert pol.blocked_tables_for_kb(kb) == []
    assert pol.allowed_tables_for_kb(kb) is None
    assert pol.is_kb_fully_denied(kb) is False
    assert pol.is_kb_restricted(kb) is False


def test_kb_level_deny(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name=None, mode="deny")
    pol = _resolve(db, user, [kb])
    assert pol.has_policies is True
    assert pol.is_kb_fully_denied(kb) is True
    assert kb in pol.blocked_kb_ids


def test_table_level_deny(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Orders", mode="deny")
    pol = _resolve(db, user, [kb])
    assert pol.is_kb_fully_denied(kb) is False
    assert pol.is_kb_restricted(kb) is True
    assert pol.blocked_tables_for_kb(kb) == ["orders"]
    assert pol.allowed_tables_for_kb(kb) is None


def test_table_allow_overrides_kb_deny(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name=None, mode="deny")
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Orders", mode="allow")
    pol = _resolve(db, user, [kb])
    # KB denied but Orders allowlisted → not fully denied.
    assert pol.is_kb_fully_denied(kb) is False
    assert kb in pol.allowlisted_kbs
    assert pol.allowlisted_kbs[kb] == {"orders"}
    assert pol.allowed_tables_for_kb(kb) == ["orders"]


def test_column_allowlist(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Orders",
                mode="allow_columns", column_allowlist=["id", "amount"])
    pol = _resolve(db, user, [kb])
    assert pol.allowed_columns_for(kb, "Orders") == ["id", "amount"]
    assert pol.allowed_columns_for(kb, "orders") == ["id", "amount"]  # case-insensitive
    assert pol.allowed_columns_for(kb, "Other") is None
    assert pol.is_kb_restricted(kb) is True


def test_row_filter(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Orders",
                mode="allow_columns", column_allowlist=["id"],
                row_filter={"region": "cn"})
    pol = _resolve(db, user, [kb])
    assert pol.row_filter_for(kb, "Orders") == {"region": "cn"}
    assert pol.row_filter_for(kb, "Missing") is None


def test_owner_bypass(db):
    owner = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=owner.id, kb_id=kb, table_name=None, mode="deny")
    pol = resolve_user_policies(
        db, user_id=owner.id, resource_type="project",
        resource_id=_RES["resource_id"], bound_kb_ids=[kb], owner_id=owner.id,
    )
    assert pol.has_policies is False
    assert pol.is_kb_fully_denied(kb) is False


def test_admin_bypass(db):
    admin = _seed_user(db, role="admin")
    kb = "kb1"
    _add_policy(db, user_id=admin.id, kb_id=kb, table_name=None, mode="deny")
    pol = resolve_user_policies(
        db, user_id=admin.id, resource_type="project",
        resource_id=_RES["resource_id"], bound_kb_ids=[kb], is_admin=True,
    )
    assert pol.has_policies is False
    assert pol.is_kb_fully_denied(kb) is False


def test_global_deny_blocks_all_bound_kbs(db):
    user = _seed_user(db)
    _add_policy(db, user_id=user.id, kb_id=None, table_name=None, mode="deny")
    pol = _resolve(db, user, ["kb1", "kb2"])
    assert pol.is_kb_fully_denied("kb1") is True
    assert pol.is_kb_fully_denied("kb2") is True


def test_global_table_deny_applies_to_all_kbs(db):
    user = _seed_user(db)
    _add_policy(db, user_id=user.id, kb_id=None, table_name="Secrets", mode="deny")
    pol = _resolve(db, user, ["kb1", "kb2"])
    assert pol.blocked_tables_for_kb("kb1") == ["secrets"]
    assert pol.blocked_tables_for_kb("kb2") == ["secrets"]


def test_specific_table_deny_overrides_kb_allow(db):
    user = _seed_user(db)
    kb = "kb1"
    # KB is allowed (no kb-level deny), but a specific table is denied.
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Secrets", mode="deny")
    pol = _resolve(db, user, [kb])
    assert pol.blocked_tables_for_kb(kb) == ["secrets"]


def test_specific_table_allow_overrides_global_table_deny(db):
    user = _seed_user(db)
    kb = "kb1"
    # Global table deny, but a specific (kb, table) allow should win.
    _add_policy(db, user_id=user.id, kb_id=None, table_name="Orders", mode="deny")
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Orders", mode="allow")
    pol = _resolve(db, user, [kb])
    # Specific allow overrides the global deny → not blocked for this KB.
    assert pol.blocked_tables_for_kb(kb) == []


# ── SQL validation ─────────────────────────────────────────────────────


def test_validate_sql_rejects_denied_table(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Secrets", mode="deny")
    pol = _resolve(db, user, [kb])
    result = validate_sql_against_policy("SELECT * FROM Secrets", pol, kb)
    assert result.is_valid is False
    assert any("blocked" in e for e in result.errors)


def test_validate_sql_allows_non_denied_table(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Secrets", mode="deny")
    pol = _resolve(db, user, [kb])
    result = validate_sql_against_policy("SELECT * FROM Orders", pol, kb)
    assert result.is_valid is True


def test_validate_sql_whitelist_mode_rejects_unlisted_table(db):
    user = _seed_user(db)
    kb = "kb1"
    _add_policy(db, user_id=user.id, kb_id=kb, table_name=None, mode="deny")
    _add_policy(db, user_id=user.id, kb_id=kb, table_name="Orders", mode="allow")
    pol = _resolve(db, user, [kb])
    # Whitelist mode: only Orders allowed.
    assert validate_sql_against_policy("SELECT * FROM Orders", pol, kb).is_valid is True
    assert validate_sql_against_policy("SELECT * FROM Secrets", pol, kb).is_valid is False
