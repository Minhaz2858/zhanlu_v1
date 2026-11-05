"""P0-1 — LLM credential isolation regression tests (2026-10-31 audit).

The defect: ``resolve_effective_llm``'s agent-binding step looked an
AgentApp up by NAME only. With identically named agents in two
organizations, the resolver could pick the OTHER org's agent and use its
``llm_model_id`` — routing this org's conversation through the other
tenant's LLM endpoint with the other tenant's DECRYPTED api key.

The contract under test (reviewer's required fix, refined after proving
the DB enforces ``org_id NOT NULL`` — default ``'default-org'``):
  * ``org_id`` is used in the agent-binding AND project-binding lookups;
  * org matching is STRICT equality: no cross-org fallback and NO
    NULL/''-org fallback (un-scoped historical rows cannot exist —
    pre-tenancy data carries 'default-org' and matches it directly);
  * a request with no org context fails CLOSED (no name-based
    credential selection at all);
  * one organization NEVER accesses another organization's credentials —
    not via name collision, and not via an agent/project row that
    references a foreign org's model id.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.agent_app import AgentApp  # noqa: E402
from app.models.llm_model import LlmModel  # noqa: E402
from app.services.crypto_utils import encrypt_value  # noqa: E402 — the
# LlmModel write path's own crypto (self-round-trips with decrypt_value
# in every environment; app.services.crypto uses a different key).
from app.services.llm_router import resolve_effective_llm  # noqa: E402

_ORG_A = f"org-llm-a-{uuid.uuid4().hex[:8]}"
_ORG_B = f"org-llm-b-{uuid.uuid4().hex[:8]}"
_NAME = f"xta_llm_{uuid.uuid4().hex[:6]}"
_KEY_A = "sk-secret-ORG-A-key"
_KEY_B = "sk-secret-ORG-B-key"


def _mk_model(model_row_id: str, name: str, org_id: str, base_url: str,
              api_key: str) -> LlmModel:
    return LlmModel(
        id=model_row_id,
        name=name,
        model_id=f"model-{org_id[-4:]}",
        provider="openai_compatible",
        base_url=base_url,
        api_key=encrypt_value(api_key),
        org_id=org_id,
        app_id="app-llm",
        enabled=True,
        is_deleted=False,
    )


def _mk_agent(agent_id: str, name: str, org_id: str,
              llm_model_id: str | None) -> AgentApp:
    return AgentApp(
        id=agent_id,
        name=name,
        org_id=org_id,
        app_id="app-llm",
        project="global",
        description="",
        prompt_identity="",
        prompt_boundary="",
        prompt_reasoning="",
        prompt_tools="",
        prompt_output="",
        capabilities=[],
        skills=[],
        knowledge_bases=[],
        topology="standalone",
        sub_agents=[],
        data_read=True,
        data_write=False,
        status="active",
        is_deleted=False,
        llm_model_id=llm_model_id,
    )


@pytest.fixture(scope="module")
def seeded():
    """Two orgs, IDENTICALLY named agents, DIFFERENT model endpoints and
    DIFFERENT credentials."""
    db = SessionLocal()
    ids = {
        "model_a": f"llm-a-{uuid.uuid4().hex[:8]}",
        "model_b": f"llm-b-{uuid.uuid4().hex[:8]}",
        "agent_a": f"agent-llm-a-{uuid.uuid4().hex[:8]}",
        "agent_b": f"agent-llm-b-{uuid.uuid4().hex[:8]}",
    }
    db.add_all([
        _mk_model(ids["model_a"], f"model-A-{ids['model_a'][-4:]}", _ORG_A,
                  "https://llm-a.example.invalid/v1", _KEY_A),
        _mk_model(ids["model_b"], f"model-B-{ids['model_b'][-4:]}", _ORG_B,
                  "https://llm-b.example.invalid/v1", _KEY_B),
        _mk_agent(ids["agent_a"], _NAME, _ORG_A, ids["model_a"]),
        _mk_agent(ids["agent_b"], _NAME, _ORG_B, ids["model_b"]),
    ])
    db.commit()
    try:
        yield ids
    finally:
        try:
            db.query(AgentApp).filter(
                AgentApp.app_id == "app-llm"
            ).delete(synchronize_session=False)
            db.query(LlmModel).filter(
                LlmModel.id.in_([ids["model_a"], ids["model_b"]])
            ).delete(synchronize_session=False)
            db.commit()
        except Exception:  # noqa: BLE001 — cleanup never masks a failure
            db.rollback()
        db.close()


@pytest.fixture(autouse=True)
def _hierarchical_on(monkeypatch):
    monkeypatch.setattr(settings, "HIERARCHICAL_LLM_ENABLED", True)


# ── The core isolation contract ──────────────────────────────────────────


def test_org_a_never_receives_org_bs_credentials(seeded):
    db = SessionLocal()
    try:
        eff_a = resolve_effective_llm(
            db, agent_name=_NAME, org_id=_ORG_A, app_id="app-llm",
        )
        assert eff_a.endpoint is not None, "org A's own agent did not resolve"
        assert eff_a.endpoint.base_url == "https://llm-a.example.invalid/v1"
        assert eff_a.endpoint.api_key == _KEY_A
        # The cross-tenant assertion, explicitly:
        assert _KEY_B not in (eff_a.endpoint.api_key or "")
        assert "llm-b.example.invalid" not in (eff_a.endpoint.base_url or "")
    finally:
        db.close()


def test_org_b_never_receives_org_as_credentials(seeded):
    db = SessionLocal()
    try:
        eff_b = resolve_effective_llm(
            db, agent_name=_NAME, org_id=_ORG_B, app_id="app-llm",
        )
        assert eff_b.endpoint is not None, "org B's own agent did not resolve"
        assert eff_b.endpoint.base_url == "https://llm-b.example.invalid/v1"
        assert eff_b.endpoint.api_key == _KEY_B
        assert _KEY_A not in (eff_b.endpoint.api_key or "")
        assert "llm-a.example.invalid" not in (eff_b.endpoint.base_url or "")
    finally:
        db.close()


def test_org_without_own_agent_never_inherits_another_orgs(seeded):
    """An org with NO same-named agent must fall through to its own
    defaults/legacy — never to another org's agent credentials."""
    db = SessionLocal()
    try:
        eff = resolve_effective_llm(
            db, agent_name=_NAME, org_id=f"org-llm-c-{uuid.uuid4().hex[:8]}",
            app_id="app-llm",
        )
        if eff.endpoint is not None:
            assert "llm-a.example.invalid" not in eff.endpoint.base_url
            assert "llm-b.example.invalid" not in eff.endpoint.base_url
            assert eff.endpoint.api_key not in (_KEY_A, _KEY_B)
    finally:
        db.close()


def test_no_org_context_means_no_name_based_credential_selection(seeded):
    """Without an authenticated org, a NAME cannot select credentials at
    all — the name path is skipped entirely (the PK path is unaffected)."""
    db = SessionLocal()
    try:
        eff = resolve_effective_llm(
            db, agent_name=_NAME, org_id="", app_id="app-llm",
        )
        if eff.endpoint is not None:
            assert eff.endpoint.api_key not in (_KEY_A, _KEY_B)
            assert "llm-a.example.invalid" not in (eff.endpoint.base_url or "")
            assert "llm-b.example.invalid" not in (eff.endpoint.base_url or "")
    finally:
        db.close()


def test_agent_referencing_a_foreign_model_id_cannot_reach_it(seeded):
    """Defense-in-depth: an agent row whose llm_model_id points at ANOTHER
    org's model must not select that model's credentials."""
    db = SessionLocal()
    try:
        rogue = _mk_agent(
            f"agent-llm-rogue-{uuid.uuid4().hex[:8]}", _NAME, _ORG_A,
            seeded["model_b"],  # org A agent → org B's model id
        )
        db.add(rogue)
        db.commit()
        try:
            eff = resolve_effective_llm(
                db, agent_name=_NAME, org_id=_ORG_A, app_id="app-llm",
            )
            if eff.endpoint is not None:
                assert (
                    "llm-b.example.invalid" not in eff.endpoint.base_url
                ), (
                    "an org-A agent reached org B's model credentials via a "
                    "planted llm_model_id"
                )
                assert eff.endpoint.api_key != _KEY_B
        finally:
            db.delete(rogue)
            db.commit()
    finally:
        db.close()


def test_pk_path_still_works_within_same_org(seeded):
    """The agent_id (PK) path is unchanged: same-org id resolves its own
    model."""
    db = SessionLocal()
    try:
        eff = resolve_effective_llm(
            db, agent_id=seeded["agent_a"], org_id=_ORG_A, app_id="app-llm",
        )
        assert eff.endpoint is not None
        assert eff.endpoint.base_url == "https://llm-a.example.invalid/v1"
        assert eff.endpoint.api_key == _KEY_A
    finally:
        db.close()


def test_cross_org_agent_id_resolves_nothing_from_that_agent(seeded):
    """P0-1 closing test (reviewer decision): org A requests ORG B's
    agent ID. The foreign AgentApp row itself must not resolve — org A
    inherits NO configuration, NO model binding, NO credentials, and NO
    resource policy (locked metadata) from it. The PK fetch is scoped at
    the identity-resolution boundary, not only at the credential lookup."""
    db = SessionLocal()
    try:
        # Make the leak OBSERVABLE: an org-B agent whose llm_model_id
        # points at org A's OWN model. If the unscoped PK fetch returned
        # this foreign row, org A would resolve source='agent' through
        # the FOREIGN agent (and could inherit its resource policy).
        foreign = _mk_agent(
            f"agent-llm-xorg-{uuid.uuid4().hex[:8]}", _NAME, _ORG_B,
            seeded["model_a"],
        )
        foreign.resource_type = "company"  # would lock org A if inherited
        db.add(foreign)
        db.commit()
        try:
            eff = resolve_effective_llm(
                db, agent_id=foreign.id, org_id=_ORG_A, app_id="app-llm",
            )
            assert eff.source != "agent", (
                "org A resolved an agent binding through org B's agent row"
            )
            assert eff.locked is False, (
                "org A inherited a resource policy (admin lock) from org "
                "B's agent metadata"
            )
            if eff.endpoint is not None:
                assert "llm-b.example.invalid" not in eff.endpoint.base_url
                assert eff.endpoint.api_key != _KEY_B
        finally:
            db.delete(foreign)
            db.commit()
    finally:
        db.close()


def test_system_agent_access_follows_explicit_ownership_policy(seeded):
    """P0-1 closing test: system agents are NOT a blanket PK-path
    exception. Their access goes through the Rule-0 NAME short-circuit,
    which resolves the CALLING ORG's own catalog default. A system agent
    name never yields another org's model or credentials, and the
    org-scoped PK path cannot be used to smuggle a system agent's row
    across orgs."""
    db = SessionLocal()
    try:
        # (a) Name path: the short-circuit resolves org A's OWN default.
        eff = resolve_effective_llm(
            db, agent_name="agent_builder", org_id=_ORG_A, app_id="app-llm",
        )
        assert eff.source == "system_default"
        if eff.endpoint is not None:
            # Neither seeded org binds a system default; anything
            # resolved here must be org A's own catalog default — never
            # the other tenant's credentials.
            assert "llm-b.example.invalid" not in eff.endpoint.base_url
            assert eff.endpoint.api_key != _KEY_B
            assert eff.endpoint.api_key != _KEY_A

        # (b) PK path: even with a system agent's ID, a cross-org caller
        # gets no agent resolution — the scoped PK fetch returns nothing
        # and the request falls to org A's own default/legacy.
        sys_agent = _mk_agent(
            f"agent-llm-sys-{uuid.uuid4().hex[:8]}", _NAME, _ORG_B,
            seeded["model_b"],
        )
        db.add(sys_agent)
        db.commit()
        try:
            eff_pk = resolve_effective_llm(
                db, agent_id=sys_agent.id, org_id=_ORG_A, app_id="app-llm",
            )
            assert eff_pk.source != "agent"
            if eff_pk.endpoint is not None:
                assert "llm-b.example.invalid" not in eff_pk.endpoint.base_url
                assert eff_pk.endpoint.api_key != _KEY_B
        finally:
            db.delete(sys_agent)
            db.commit()
    finally:
        db.close()


# ── Reviewer adversarial checks 1-5 (2026-10-31) ─────────────────────────


def test_disabled_own_model_never_triggers_cross_org_fallback(seeded):
    """Check 5: when the org's OWN model is disabled, resolution falls to
    the org's catalog default / legacy — NEVER to another org's model."""
    db = SessionLocal()
    try:
        model_a = db.query(LlmModel).filter(
            LlmModel.id == seeded["model_a"]
        ).first()
        model_a.enabled = False
        db.commit()
        try:
            eff = resolve_effective_llm(
                db, agent_name=_NAME, org_id=_ORG_A, app_id="app-llm",
            )
            if eff.endpoint is not None:
                assert "llm-b.example.invalid" not in eff.endpoint.base_url, (
                    "an unavailable own model triggered a cross-org fallback"
                )
                assert eff.endpoint.api_key != _KEY_B
            assert eff.source != "agent" or eff.endpoint is not None
        finally:
            model_a.enabled = True
            db.commit()
    finally:
        db.close()


def test_project_binding_cannot_select_a_foreign_model(seeded):
    """Check 3: a project row whose llm_model_id points at ANOTHER org's
    model must not select that org's credentials (the project step uses
    the same scoped lookup as the agent step)."""
    db = SessionLocal()
    try:
        from app.models.project import Project

        proj = Project(
            id=f"proj-llm-{uuid.uuid4().hex[:8]}",
            name=f"xta_proj_{uuid.uuid4().hex[:6]}",
            org_id=_ORG_A,
            app_id="app-llm",
            status="active",
            is_deleted=False,
            llm_model_id=seeded["model_b"],  # org A project → org B model
        )
        db.add(proj)
        db.commit()
        try:
            eff = resolve_effective_llm(
                db, project_id=proj.id, org_id=_ORG_A, app_id="app-llm",
            )
            if eff.endpoint is not None:
                assert (
                    "llm-b.example.invalid" not in eff.endpoint.base_url
                ), "a project binding reached another org's model credentials"
                assert eff.endpoint.api_key != _KEY_B
        finally:
            db.delete(proj)
            db.commit()
    finally:
        db.close()


def test_legacy_unscoped_rows_never_reachable_and_default_org_matches_directly(seeded):
    """Check 1 (strict-equality contract): the historical NULL/''-org
    fallback is ELIMINATED, not tightened.

    Two facts proven here:
      * An ''-org row (the only creatable un-scoped shape — the DB
        enforces ``org_id NOT NULL``, so NULL is impossible; verified by
        the IntegrityError raised when this test suite first attempted a
        raw ``UPDATE ... SET org_id = NULL``) is unreachable from EVERY
        org. Historical compatibility must not become a second
        tenant-isolation bypass.
      * Pre-tenancy rows carry ``'default-org'`` and are matched
        DIRECTLY by a default-org caller — no fallback path is needed
        for legacy compatibility.
    """
    db = SessionLocal()
    try:
        from sqlalchemy import text as _text

        # (a) ''-org model + same-named ''-org agent: un-scoped shape.
        empty_model_id = f"llm-empty-{uuid.uuid4().hex[:8]}"
        empty_model = _mk_model(empty_model_id,
                                f"empty-{empty_model_id[-4:]}", "default-org",
                                "https://llm-empty.example.invalid/v1",
                                "sk-EMPTY-ORG-shared-key")
        empty_agent = _mk_agent(
            f"agent-llm-empty-{uuid.uuid4().hex[:8]}", _NAME, "default-org",
            empty_model_id,
        )
        db.add_all([empty_model, empty_agent])
        db.commit()
        db.execute(_text(
            "UPDATE llm_models SET org_id = '' WHERE id = :i"
        ), {"i": empty_model_id})
        db.execute(_text(
            "UPDATE agent_apps SET org_id = '' WHERE id = :i"
        ), {"i": empty_agent.id})
        db.commit()
        db.expire_all()

        for caller_org in (_ORG_A, _ORG_B, "default-org"):
            eff = resolve_effective_llm(
                db, agent_name=_NAME, org_id=caller_org, app_id="app-llm",
            )
            if eff.endpoint is not None:
                assert "llm-empty.example.invalid" not in (
                    eff.endpoint.base_url or ""
                ), (
                    f"org {caller_org} reached the un-scoped ''-org row via "
                    "the (removed) historical fallback"
                )
                assert eff.endpoint.api_key != "sk-EMPTY-ORG-shared-key"

        db.delete(empty_agent)
        db.flush()  # pending ORM delete must flush before the raw FK'd delete
        db.execute(_text("DELETE FROM llm_models WHERE id = :i"),
                   {"i": empty_model_id})
        db.commit()

        # (b) Pre-tenancy 'default-org' agent + model: reachable
        # DIRECTLY by a default-org caller (compatibility without
        # fallback). Also confirms an org-less request cannot use it.
        pre_model_id = f"llm-pre-{uuid.uuid4().hex[:8]}"
        pre_model = _mk_model(pre_model_id,
                              f"pre-{pre_model_id[-4:]}", "default-org",
                              "https://llm-pre.example.invalid/v1",
                              "sk-PRE-TENANCY-key")
        pre_agent = _mk_agent(
            f"agent-llm-pre-{uuid.uuid4().hex[:8]}", _NAME, "default-org",
            pre_model_id,
        )
        db.add_all([pre_model, pre_agent])
        db.commit()
        try:
            eff_pre = resolve_effective_llm(
                db, agent_name=_NAME, org_id="default-org", app_id="app-llm",
            )
            assert eff_pre.endpoint is not None, (
                "a default-org caller must reach pre-tenancy 'default-org' "
                "rows directly (strict equality is also the compatibility path)"
            )
            assert eff_pre.endpoint.base_url == "https://llm-pre.example.invalid/v1"
            assert eff_pre.endpoint.api_key == "sk-PRE-TENANCY-key"
            assert eff_pre.source == "agent"

            eff_none = resolve_effective_llm(
                db, agent_name=_NAME, org_id="", app_id="app-llm",
            )
            if eff_none.endpoint is not None:
                assert "llm-pre.example.invalid" not in (
                    eff_none.endpoint.base_url or ""
                ), "an org-less request selected credentials (fail-closed violated)"
        finally:
            db.delete(pre_agent)
            db.flush()
            db.execute(_text("DELETE FROM llm_models WHERE id = :i"),
                       {"i": pre_model_id})
            db.commit()
    finally:
        db.close()
