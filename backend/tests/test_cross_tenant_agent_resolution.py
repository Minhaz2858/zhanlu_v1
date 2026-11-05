"""D-5 security sweep — cross-tenant agent-app resolution regression tests.

Agent names are NOT unique across organizations. Before the D-5 fixes, a
name-only ``AgentApp`` lookup could resolve ANOTHER org's same-named app and
inherit everything hanging off it: tool config, bound data sources, workflow
definitions, cache/experience store identity. Measured live during the 2C
synthetic-schema e2e: one org's conversation resolved the other org's
catalog and its statement was rejected by the wrong tenant's metadata.

The contract under test (``_resolve_agent_app_scoped`` and the tool's
org-scoped fallback):

  1. an org's request resolves the org's OWN row for that name;
  2. another org's row is NEVER returned — no cross-tenant fallback;
  3. legacy/global rows (org NULL or '') remain resolvable;
  4. the org's own row wins over a legacy global row;
  5. through the tool's fallback data-source derivation, one org's
     bound data sources can never reach the other org's conversation.

The v3 data-ctx site (the original D-5 discovery) is covered end-to-end by
tests/test_2c_v3_e2e_unseen_db.py: both synthetic orgs use the SAME agent
name (``data_analyst_2c``) and the finance turn must ground in the finance
org's catalog — pre-fix, that e2e measured the sales org's catalog.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal  # noqa: E402
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.user import User  # noqa: E402
from app.routers.agents import (  # noqa: E402
    _resolve_agent_app_key,
    _resolve_agent_app_scoped,
)

_ORG_A = f"org-xta-a-{uuid.uuid4().hex[:8]}"
_ORG_B = f"org-xta-b-{uuid.uuid4().hex[:8]}"
_NAME = f"xta_collide_{uuid.uuid4().hex[:6]}"


def _mk_kb(kb_id: str, org_id: str) -> KnowledgeBase:
    return KnowledgeBase(
        id=kb_id,
        name=f"xta_{org_id[-4:]}",
        org_id=org_id,
        app_id="app-xta",
        type="database",
        source_kind="database",
        db_type="sqlite",
        database_name=f"/tmp/nonexistent-{kb_id}.db",
        catalog_status="ready",
        nl2sql_state="nl2sql_ready",
    )


def _mk_agent(agent_id: str, name: str, org_id, kb_ids) -> AgentApp:
    return AgentApp(
        id=agent_id,
        name=name,
        org_id=org_id,
        app_id="app-xta",
        project="global",
        description="",
        prompt_identity="",
        prompt_boundary="",
        prompt_reasoning="",
        prompt_tools="",
        prompt_output="",
        capabilities=[],
        skills=[],
        knowledge_bases=kb_ids,
        topology="standalone",
        sub_agents=[],
        data_read=True,
        data_write=False,
        status="active",
        is_deleted=False,
    )


def _mk_user(user_id: str, org_id: str) -> User:
    return User(
        id=user_id,
        email=f"xta_{org_id[-8:]}@example.invalid",
        full_name="XTA",
        role="user",
        org_id=org_id,
        password_hash="x",
    )


@pytest.fixture(scope="module")
def seeded():
    """Two orgs, IDENTICALLY named agents, each bound to its own KB."""
    db = SessionLocal()
    ids = {
        "kb_a": f"kb-xta-a-{uuid.uuid4().hex[:8]}",
        "kb_b": f"kb-xta-b-{uuid.uuid4().hex[:8]}",
        "app_a": f"app-xta-a-{uuid.uuid4().hex[:8]}",
        "app_b": f"app-xta-b-{uuid.uuid4().hex[:8]}",
        "user_a": f"user-xta-a-{uuid.uuid4().hex[:8]}",
        "user_b": f"user-xta-b-{uuid.uuid4().hex[:8]}",
        "app_legacy": f"app-xta-legacy-{uuid.uuid4().hex[:8]}",
        "app_legacy_blank": f"app-xta-blank-{uuid.uuid4().hex[:8]}",
    }
    db.add_all([
        _mk_kb(ids["kb_a"], _ORG_A),
        _mk_kb(ids["kb_b"], _ORG_B),
        _mk_agent(ids["app_a"], _NAME, _ORG_A, [ids["kb_a"]]),
        _mk_agent(ids["app_b"], _NAME, _ORG_B, [ids["kb_b"]]),
        # A name that exists ONLY in org A — org B must get None for it.
        _mk_agent(
            f"app-xta-only-a-{uuid.uuid4().hex[:8]}",
            f"xta_only_a_{ids['app_a'][-6:]}",
            _ORG_A, [ids["kb_a"]],
        ),
        # Legacy/global rows: org NULL and org ''.
        _mk_agent(ids["app_legacy"], f"xta_legacy_{uuid.uuid4().hex[:6]}",
                  None, [ids["kb_a"]]),
        _mk_agent(ids["app_legacy_blank"], f"xta_blank_{uuid.uuid4().hex[:6]}",
                  "", [ids["kb_a"]]),
        _mk_user(ids["user_a"], _ORG_A),
        _mk_user(ids["user_b"], _ORG_B),
    ])
    db.commit()
    try:
        yield ids
    finally:
        try:
            db.query(KnowledgeBase).filter(
                KnowledgeBase.id.in_([ids["kb_a"], ids["kb_b"]])
            ).delete(synchronize_session=False)
            db.query(AgentApp).filter(
                AgentApp.app_id == "app-xta"
            ).delete(synchronize_session=False)
            db.query(User).filter(
                User.id.in_([ids["user_a"], ids["user_b"]])
            ).delete(synchronize_session=False)
            db.commit()
        except Exception:  # noqa: BLE001 — cleanup never masks a failure
            db.rollback()
        db.close()


# ── Helper contract ──────────────────────────────────────────────────────


def test_org_resolves_its_own_same_named_agent(seeded):
    db = SessionLocal()
    try:
        got = _resolve_agent_app_scoped(db, _NAME, _ORG_A)
        assert got is not None and got.id == seeded["app_a"], (
            "org A did not resolve its own same-named agent"
        )
        got_b = _resolve_agent_app_scoped(db, _NAME, _ORG_B)
        assert got_b is not None and got_b.id == seeded["app_b"], (
            "org B did not resolve its own same-named agent"
        )
    finally:
        db.close()


def test_another_orgs_row_is_never_returned(seeded):
    db = SessionLocal()
    try:
        only_a_name = f"xta_only_a_{seeded['app_a'][-6:]}"
        got = _resolve_agent_app_scoped(db, only_a_name, _ORG_B)
        assert got is None, (
            "org B resolved org A's agent — cross-tenant fallback survived"
        )
    finally:
        db.close()


def test_default_org_agent_never_reaches_another_org(seeded):
    """``default-org`` is the single-tenant compatibility bucket: its rows
    resolve for ITS users directly, and for NOBODY else. (The ORM cannot
    create NULL-org rows — org_id is NOT NULL with default 'default-org' —
    so 'default-org' is what every pre-tenancy row carries.)"""
    db = SessionLocal()
    try:
        default_org_row = db.query(AgentApp).filter(
            AgentApp.id == seeded["app_legacy"]
        ).first()
        assert default_org_row.org_id == "default-org", (
            "test premise: the un-scoped row carries the default org"
        )
        got_default = _resolve_agent_app_scoped(
            db, default_org_row.name, "default-org"
        )
        assert got_default is not None and got_default.id == seeded[
            "app_legacy"
        ], "a default-org user stopped resolving a default-org agent"
        got_foreign = _resolve_agent_app_scoped(
            db, default_org_row.name, _ORG_A
        )
        assert got_foreign is None, (
            "another org resolved a default-org agent — cross-tenant "
            "fallback survived"
        )
    finally:
        db.close()


def test_org_row_wins_over_default_org_row(seeded):
    db = SessionLocal()
    try:
        default_org = db.query(AgentApp).filter(
            AgentApp.id == seeded["app_legacy"]
        ).first()
        own = _mk_agent(
            f"app-xta-ownb-{uuid.uuid4().hex[:8]}", default_org.name, _ORG_B,
            [seeded["kb_b"]],
        )
        db.add(own)
        db.commit()
        try:
            got = _resolve_agent_app_scoped(db, default_org.name, _ORG_B)
            assert got.id == own.id, (
                "org B's own row lost to the default-org row"
            )
            got_d = _resolve_agent_app_scoped(
                db, default_org.name, "default-org"
            )
            assert got_d.id == seeded["app_legacy"], (
                "the default-org user should get the default-org row"
            )
        finally:
            db.delete(own)
            db.commit()
    finally:
        db.close()


def test_experience_store_key_is_org_scoped(seeded):
    db = SessionLocal()
    try:
        key_a = _resolve_agent_app_key(db, _NAME, "app-x", org_id=_ORG_A)
        key_b = _resolve_agent_app_key(db, _NAME, "app-x", org_id=_ORG_B)
        assert key_a == seeded["app_a"] and key_b == seeded["app_b"], (
            "feedback/experience store keys crossed tenants"
        )
    finally:
        db.close()


# ── Tool fallback: one org's data sources can never reach the other ──────


def test_tool_fallback_binds_only_calling_users_org(seeded, monkeypatch):
    """The query_database fallback derives the data scope from the calling
    USER's org. With identically named agents in both orgs, org B's user
    must get org B's bindings — never org A's."""
    import app.services.data_source_runtime.data_source_runtime as dsr

    captured: dict = {}
    real_prep = dsr.prepare_data_source_runtime

    def _recorder(db, agent_app, *a, **k):
        captured["agent_app"] = agent_app
        return real_prep(db, agent_app, *a, **k)

    monkeypatch.setattr(dsr, "prepare_data_source_runtime", _recorder)
    # Fail the pipeline immediately after scope derivation — the runtime
    # prep + its org resolution is the behavior under test.
    import app.services.synexia.fsm as fsm_mod

    class _BoomFSM:
        def __init__(self, *a, **k):
            pass

        def run(self, *a, **k):
            raise RuntimeError("2c-xta pipeline stub")

    monkeypatch.setattr(fsm_mod, "SynexiaFSM", _BoomFSM)
    from app.config import settings

    monkeypatch.setattr(
        settings, "NL2SQL_PREFLIGHT_PROBE_ENABLED", False
    )

    from app.services.tool_handlers.query_database_tool import _query_database

    result = asyncio.run(_query_database(
        {"question": "cross-tenant probe"},
        SessionLocal(),
        seeded["user_b"],  # the calling user is org B's
        context={
            "agent_name": _NAME,
            "app_id": "app-xta",
            "org_id": _ORG_B,
            "conversation_id": "conv-xta",
        },
    ))

    assert isinstance(result, dict)
    resolved = captured.get("agent_app")
    assert resolved is not None, "the tool fallback never resolved an agent"
    assert resolved.id == seeded["app_b"], (
        f"org B's conversation resolved agent {resolved.id} "
        f"(org={getattr(resolved, 'org_id', None)!r}) — cross-tenant "
        "data-source inheritance survived the D-5 sweep"
    )
    assert resolved.org_id == _ORG_B
