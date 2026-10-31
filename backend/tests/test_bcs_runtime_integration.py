"""Phase 2B — BCS runtime integration (flag-gated, fail-open).

Covers: flag disabled → zero BCS invocation and unchanged prompts; flag
enabled → delimited context injected at the system-prompt choke point and
structured context in the data-source runtime extras; missing/cross-org
projects degrade gracefully; rule tiers survive rendering; BCS failure
never breaks chat; subagents (no bcs_block) see nothing; budget stays
bounded. Runtime files themselves are integration-tested only through
these seams — no ReAct/FSM/NL2SQL changes.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models.business_rule import BusinessRule
from app.models.enterprise_process import OrgUnit
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.project import Project
from app.models.user import User
from app.services.agent_prompts import build_bcs_block, get_system_prompt
from app.services.data_source_runtime import prepare_data_source_runtime

ORG = "org-acme"
ORG2 = "org-other"


def _user(uid="u-owner", org=ORG):
    return User(id=uid, email=f"{uid}@t.dev", full_name=uid.title(),
                role="user", password_hash="x", org_id=org)


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.models.enterprise_process import (
        BusinessProcess, ProcessActivity, ProcessEvidence)
    from app.models.metric_definition import MetricDefinition
    from app.models.ontology import OntologyLink, OntologyObject
    from app.models.terminology import Terminology
    Base.metadata.create_all(eng, tables=[
        Organization.__table__, Project.__table__, OrgUnit.__table__,
        BusinessRule.__table__, KnowledgeBase.__table__, User.__table__,
        BusinessProcess.__table__, ProcessActivity.__table__,
        ProcessEvidence.__table__, OntologyObject.__table__,
        OntologyLink.__table__, Terminology.__table__,
        MetricDefinition.__table__,
    ])
    s = sessionmaker(bind=eng)()
    s.add(User(id="u-owner", email="o@t.dev", full_name="Owner",
               role="user", password_hash="x", org_id=ORG))
    s.add(Organization(id=ORG, name="ACME Corp", slug="acme"))
    s.add(OrgUnit(id="ou-mfg", org_id=ORG, name="Manufacturing",
                  unit_type="department"))
    s.add(Project(id="p-1", name="Factory rollout", org_id=ORG,
                  created_by_id="u-owner",
                  department_org_unit_id="ou-mfg"))
    s.add(KnowledgeBase(id="kb-1", name="Factory DB", org_id=ORG,
                        source_kind="database", project_id="p-1"))
    s.add(BusinessRule(id="br-on", org_id=ORG,
                       statement="Exclude cancelled orders from revenue",
                       status="enabled", applies_to="sql"))
    s.add(BusinessRule(id="br-prop", org_id=ORG,
                       statement="Maybe prioritize VIP customers",
                       status="proposed", applies_to="sql"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "BCS_ENABLED", True)


@pytest.fixture()
def flag_off(monkeypatch):
    monkeypatch.setattr(settings, "BCS_ENABLED", False)


# 1. Flag disabled → no BCS invocation, unchanged prompts --------------------

def test_flag_disabled_no_bcs_invocation(db, flag_off, monkeypatch):
    calls = []
    import app.services.enterprise.business_context as bc
    orig = bc.build_business_context
    monkeypatch.setattr(
        bc, "build_business_context",
        lambda *a, **k: calls.append(1) or orig(*a, **k))
    block, ctx = build_bcs_block(db, _user(), ORG, project_id="p-1",
                                 query="factory")
    assert block is None and ctx is None
    assert calls == []  # zero queries, zero latency
    # get_system_prompt without a block is byte-identical to before
    p_plain = get_system_prompt(None)
    assert "<enterprise_business_context>" not in p_plain


# 2. Flag enabled → context injected ------------------------------------------

def test_flag_enabled_block_built_and_injected(db, flag_on):
    block, ctx = build_bcs_block(db, _user(), ORG, project_id="p-1",
                                 query="factory orders")
    assert block and "<enterprise_business_context>" not in block
    assert ctx["project"]["authorized"] is True
    p = get_system_prompt(None, bcs_block=block)
    assert p.endswith("\n</enterprise_business_context>")
    assert p.count("<enterprise_business_context>") == 1
    assert "Factory rollout" in p and "Manufacturing" in p
    assert "Enabled business rules (executable guidance)" in p
    # flag on but no block passed (e.g. subagent path) → nothing injected
    assert "<enterprise_business_context>" not in get_system_prompt(None)


# 3. Missing project → graceful ------------------------------------------------

def test_missing_project_graceful(db, flag_on):
    block, ctx = build_bcs_block(db, _user(), ORG, project_id="p-missing",
                                 query="orders")
    assert ctx["project"]["authorized"] is False
    assert "unavailable" in (block or "").lower()
    # ordinary fallback prompt still produced
    assert "Business Context" in get_system_prompt(None, bcs_block=block)


# 4. Cross-org project → denied --------------------------------------------------

def test_cross_org_project_denied(db, flag_on):
    db.add(Project(id="p-evil", name="Evil project", org_id=ORG2,
                   created_by_id="u-owner"))
    db.add(KnowledgeBase(id="kb-evil", name="Evil DB", org_id=ORG2,
                         source_kind="database", project_id="p-evil"))
    db.commit()
    block, ctx = build_bcs_block(db, _user(), ORG, project_id="p-evil")
    assert ctx["project"]["authorized"] is False
    assert ctx["project"]["resources"] == []
    assert "Evil DB" not in (block or "")
    assert "Evil project" not in (block or "")


# 5-6. Rule tiers survive into the rendered block --------------------------------

def test_rule_tiers_in_rendered_block(db, flag_on):
    block, _ = build_bcs_block(db, _user(), ORG, project_id="p-1",
                               query="orders revenue customers")
    assert "Exclude cancelled orders" in block.split(
        "Enabled business rules (executable guidance)")[1].split("##")[0]
    if "Maybe prioritize VIP customers" in block:
        segment = block.split("Maybe prioritize VIP customers")[0]
        assert "UNVERIFIED" in segment  # under the proposed/hypotheses header


# 7. BCS failure → ordinary chat still works --------------------------------------

def test_bcs_failure_fail_open(db, flag_on, monkeypatch):
    import app.services.enterprise.business_context as bc
    monkeypatch.setattr(
        bc, "build_business_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    block, ctx = build_bcs_block(db, _user(), ORG, project_id="p-1")
    assert block is None and ctx is None
    # the prompt pipeline continues unchanged
    assert get_system_prompt(None, bcs_block=block) == get_system_prompt(None)


# 8. Subagent authorization unchanged ----------------------------------------------

def test_subagent_path_receives_no_context(db, flag_on):
    # Subagents call get_system_prompt WITHOUT bcs_block — they must never
    # see enterprise context, and their authorization surfaces are untouched.
    p = get_system_prompt("general_assistant")
    assert "<enterprise_business_context>" not in p
    # even an explicitly-built block cannot widen permissions: the payload
    # carries no grants, only labels + metadata
    block, ctx = build_bcs_block(db, _user(), ORG, project_id="p-1")
    assert ctx["authorization_summary"]["department_is_context_only"] is True
    assert ctx["authorization_summary"]["resource_policy"].startswith(
        "ResourceAccessPolicy")


# 9. Prompt budget remains bounded ---------------------------------------------------

def test_budget_bounded(db, flag_on):
    block, ctx = build_bcs_block(db, _user(), ORG, project_id="p-1",
                                 query="orders")
    tb = ctx["token_budget"]
    assert tb["estimated_tokens"] <= tb["budget_tokens"]
    assert tb["budget_tokens"] == settings.BCS_BUDGET_TOKENS
    # wrapper extras path: structured context attached, compact
    tools, sp, extras = prepare_data_source_runtime(
        db, None, [], "", bcs_context=ctx)
    bc_extras = extras["business_context"]
    assert bc_extras["enabled_rules"] == [
        "Exclude cancelled orders from revenue"]
    assert bc_extras["department"]["name"] == "Manufacturing"
    assert bc_extras["project_authorized"] is True
    # flag-off extras path: unchanged
    _, _, extras_off = prepare_data_source_runtime(db, None, [], "")
    assert "business_context" not in extras_off
