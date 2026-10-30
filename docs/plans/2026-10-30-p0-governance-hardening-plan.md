# P0 Governance Hardening — Implementation Plan

> **STATUS: IMPLEMENTED & VERIFIED 2026-10-30.** Actual results, review findings + fixes, and live smoke evidence: see `docs/plans/2026-10-30-p0-progress-log.md`. Commits `119ce4684`, `69d19fcbb`, `c332250fc`. Refinements incorporated before implementation: (1) console identity trusted only with `authenticated_user_id` from authenticated HTTP code; (2) failure-safe denial auditing via independent session; (3) extra regression tests (cross-domain binding, impersonation, read-actions-unaffected, nested-identity, agent-read-path); (4) shared `is_write_action` predicate used by both chokepoints.

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task. Each task is TDD: failing test → minimal implementation → green → commit.

**Goal:** Make ontology write-action permission enforcement fail-closed at the two service chokepoints, and register the ontology audit vocabularies.

**Architecture:** New module `backend/app/services/ontology/permissions.py` with one decision function (`check_action_permission`) called from BOTH `execute_action` and `execute_approved_action` in `backend/app/services/ontology/data.py` — these two functions are the only paths that run an ontology action's tool (verified: agent tool handler `ontology_tool.py:75-88`, routers `ontology.py:184,604`, MCP, nested `execute_tool` all funnel through them). No schema migration.

**Tech stack:** SQLAlchemy models (existing), pytest, existing `AuditEvent` vocabulary.

**Security implications (read first):**
- After this change, an agent with NO binding or read-only binding cannot execute write actions, and cannot create ApprovalRequests for them.
- Callers with no identity (empty context, e.g. some MCP invocations) are denied writes. Reads are intentionally unchanged.
- `ontology_tool.py:29` defaults missing `agent_name` to `"general_assistant"` — the new checker deliberately does NOT use that helper; it reads the raw context so an unidentified caller can never inherit general_assistant's bindings.
- Humans via HTTP keep today's behavior (request → approval → execute-approved) via `caller_kind="console_user"`.

---

### Task 1: Whitelist the ontology audit vocabularies

**Objective:** The audit event types/source already written by `fanout.py` and `escalation.py` become registered vocabulary.

**Files:**
- Modify: `backend/app/models/audit_event.py:16-22`
- Test: `backend/tests/test_ontology_p0_governance.py` (new file, this task creates it)

**Step 1: Write failing test**

Create `backend/tests/test_ontology_p0_governance.py`:

```python
"""P0 governance hardening — fail-closed ontology action permissions.

Covers: permission_scope enforcement on both execution paths, fail-closed
behavior for unidentified callers and unbound agents, console-user
exemption, denial auditing, and audit vocabulary registration.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import OntologyAction, OntologyObject

ORG = "org-test"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


def _make_write_action(db, tool_name="some_tool"):
    from app.models.ontology import OntologyDomain
    dom = OntologyDomain(org_id=ORG, code="procurement", name="Procurement")
    db.add(dom)
    db.flush()
    obj = OntologyObject(
        org_id=ORG, domain_id=dom.id, name="PurchaseOrder",
        backing_kind="manual",
    )
    db.add(obj)
    db.flush()
    act = OntologyAction(
        org_id=ORG, object_id=obj.id, name="CreatePO",
        tool_name=tool_name, risk_level="write",
        requires_confirmation=True, params_json={"qty": 5},
    )
    db.add(act)
    db.commit()
    return act


def _add_binding(db, agent_name, scope):
    from app.models.ontology import AgentDomainBinding
    dom = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == ORG, OntologyObject.name == "PurchaseOrder")
        .one()
    )
    db.add(AgentDomainBinding(
        org_id=ORG, agent_name=agent_name,
        domain_id=dom.domain_id, permission_scope=scope,
    ))
    db.commit()


def _async_fake(fn):
    async def _inner(*args, **kwargs):
        return fn(*args, **kwargs)
    return _inner


def test_audit_vocabularies_whitelist_ontology():
    from app.models.audit_event import AUDIT_EVENT_SOURCES, AUDIT_EVENT_TYPES
    assert "ontology" in AUDIT_EVENT_SOURCES
    for t in ("ontology_fanout", "escalation_created", "escalation_resolved"):
        assert t in AUDIT_EVENT_TYPES
```

**Step 2: Run to verify failure**

Run: `cd /Users/minhazulislam/Desktop/zhanlu_n8n_main_version/backend && python -m pytest tests/test_ontology_p0_governance.py::test_audit_vocabularies_whitelist_ontology -v`
Expected: FAIL — `"ontology" not in AUDIT_EVENT_SOURCES` (AssertionError)

**Step 3: Implement**

In `backend/app/models/audit_event.py` replace lines 16-22:

```python
AUDIT_EVENT_TYPES = [
    "nl2sql_query", "datasource_connect", "metric_access",
    "policy_violation", "skill_create", "skill_publish",
    "agent_config", "template_upload", "learning_apply",
    "system_config",
    # Ontology (written by services/ontology/{fanout,escalation}.py and
    # permissions.py since P0-2; registered 2026-10-30 — gap audit G3-bug)
    "ontology_fanout", "escalation_created", "escalation_resolved",
]
AUDIT_EVENT_SOURCES = ["nl2sql", "studio", "agent", "governance", "system", "user", "ontology"]
```

**Step 4: Run to verify pass**

Run: `python -m pytest tests/test_ontology_p0_governance.py -v`
Expected: 1 passed

**Step 5: Commit**

```bash
git add backend/app/models/audit_event.py backend/tests/test_ontology_p0_governance.py
git commit -m "fix(ontology): register ontology audit event types/source in AuditEvent vocabularies (gap G3-bug)"
```

---

### Task 2: Failing test — write denied for agent without any binding

**Objective:** Prove the fail-closed default before any implementation exists.

**Files:**
- Test: `backend/tests/test_ontology_p0_governance.py` (append)

**Step 1: Write failing test**

Append to `backend/tests/test_ontology_p0_governance.py`:

```python
def test_write_denied_for_agent_without_binding(db):
    """Fail closed: an agent with NO domain binding cannot execute a write
    action, and NO ApprovalRequest is created."""
    from app.models.governance import ApprovalRequest
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("denied") is True
    assert "no binding" in res["reason"]
    assert db.query(ApprovalRequest).count() == 0
```

**Step 2: Run to verify failure**

Run: `python -m pytest tests/test_ontology_p0_governance.py::test_write_denied_for_agent_without_binding -v`
Expected: FAIL — currently the write branch creates an ApprovalRequest, so `res.get("denied") is None` (KeyError/AssertionError on `res["success"] is False` … actually `success=False` and `blocked="confirmation_required"` pass, but `"no binding" in reason` fails).

**Step 3: Do NOT implement yet** (Tasks 3-5 build the module; the remaining failing tests are added first per TDD).

---

### Task 3: Failing tests — read-only binding, write binding, console user, unidentified caller

**Objective:** Pin the full rule matrix in tests.

**Files:**
- Test: `backend/tests/test_ontology_p0_governance.py` (append)

**Step 1: Write failing tests**

Append:

```python
def test_write_denied_for_read_only_binding(db):
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "read")
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("denied") is True
    assert "read-only" in res["reason"]


def test_write_allowed_with_write_binding(db):
    """A write-scope binding lets the normal flow proceed: a pending
    ApprovalRequest is created (risk contract unchanged)."""
    from app.models.governance import ApprovalRequest
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "write")
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("blocked") == "confirmation_required"
    assert res.get("approval_id")
    approval = db.query(ApprovalRequest).filter(
        ApprovalRequest.id == res["approval_id"]).one()
    assert approval.status == "pending"


def test_write_denied_for_unidentified_caller(db):
    """Empty context (e.g. some MCP paths) → denied. The historical
    'general_assistant' default must NOT grant identity here."""
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    res = execute_action(db, ORG, "u1", act.id, params={}, context={})
    assert res["success"] is False
    assert res.get("denied") is True


def test_console_user_requesting_write_still_gets_approval(db):
    """Humans via HTTP keep today's semantics: no binding required, the
    governance approval flow gates the run."""
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"user_id": "u1", "authenticated_user_id": "u1",
                "caller_kind": "console_user"},
    )
    assert res["success"] is False
    assert res.get("blocked") == "confirmation_required"
    assert res.get("approval_id")


def test_multiple_bindings_any_write_scope_grants(db):
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "read")
    _add_binding(db, "some_agent", "write")
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res.get("blocked") == "confirmation_required"


def test_write_denied_for_binding_in_other_domain(db):
    """A write binding on Domain B must NOT authorize a write action of
    Domain A — scope is per-domain, never org-wide."""
    from app.models.ontology import OntologyDomain
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)  # action's domain: procurement
    other = OntologyDomain(org_id=ORG, code="finance", name="Finance")
    db.add(other)
    db.flush()
    from app.models.ontology import AgentDomainBinding
    db.add(AgentDomainBinding(
        org_id=ORG, agent_name="some_agent",
        domain_id=other.id, permission_scope="write",
    ))
    db.commit()
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res["success"] is False
    assert res.get("denied") is True


def test_console_user_impersonation_is_denied(db):
    """caller_kind='console_user' is only trusted when established by
    authenticated HTTP code — it must arrive with a non-empty
    authenticated_user_id. Anything else is unidentified (fail closed)."""
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    for ctx in (
        {"caller_kind": "console_user"},                      # no identity at all
        {"caller_kind": "console_user", "authenticated_user_id": ""},  # empty
    ):
        res = execute_action(db, ORG, "u1", act.id, params={}, context=ctx)
        assert res["success"] is False, ctx
        assert res.get("denied") is True, ctx


def test_read_actions_are_unaffected(db):
    """The read=auto contract is untouched: a read action with NO identity
    context passes the permission gate (failure is the honest 'no bound
    tool' proposal reason, never a permission denial)."""
    from app.models.ontology import OntologyAction, OntologyObject
    from app.services.ontology.data import execute_action

    _make_write_action(db)  # creates the PurchaseOrder object (fixture reuse)
    obj = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == ORG, OntologyObject.name == "PurchaseOrder")
        .one()
    )
    act = OntologyAction(
        org_id=ORG, object_id=obj.id, name="ListPOs",
        tool_name=None, risk_level="read", requires_confirmation=False,
    )
    db.add(act)
    db.commit()
    res = execute_action(db, ORG, "u1", act.id, params={}, context={})
    assert res["success"] is False
    assert "denied" not in res          # NOT a permission refusal
    assert "no bound tool" in res["reason"]
```

**Step 2: Run to verify failures**

Run: `python -m pytest tests/test_ontology_p0_governance.py -v`
Expected: Task-1 test passes; all Task-2/3 tests FAIL (no enforcement yet — read-only/unidentified tests currently reach the approval branch; multiple-bindings test currently passes accidentally? No — currently ANY caller reaches the approval branch, so `test_write_allowed_with_write_binding`, `test_console_user_requesting_write_still_gets_approval`, and `test_multiple_bindings_any_write_scope_grants` may PASS already; they are regression pins. The DENIAL tests are the failing RED set.)

---

### Task 4: Implement `services/ontology/permissions.py`

**Objective:** The single decision point, with denial auditing.

**Files:**
- Create: `backend/app/services/ontology/permissions.py`

**Step 1: Write the module**

```python
"""Ontology action permission enforcement — fail closed (PRD P0, 2026-10-30).

Single decision point for ontology action execution. The ONLY two
functions that run an ontology action's tool are execute_action and
execute_approved_action (services/ontology/data.py); every caller —
agent tool handler, HTTP router, MCP, nested execute_tool — funnels
through them, so checking there covers all paths.

Rules (fail closed):
- WRITE actions (ONE shared predicate: risk_level=='write' OR
  requires_confirmation) require an explicit permission_scope='write'
  AgentDomainBinding on the action's OWN domain; multiple bindings
  grant if ANY row is write-scope; read-only rows never do.
- Unidentified callers (no agent_name, no caller_kind) are denied
  writes. The historical 'general_assistant' default in
  ontology_tool.py:29 is deliberately NOT inherited here.
- console_user callers may REQUEST only when the context was
  established by authenticated HTTP code: caller_kind='console_user'
  AND a non-empty authenticated_user_id. Anything else (tool args,
  MCP payloads, user-controlled context keys) can never impersonate a
  console user — it is treated as unidentified.
- READ actions are unchanged (read=auto risk contract).
- Denial auditing is failure-safe: it writes via an INDEPENDENT
  session/transaction, so an audit persistence failure can neither
  mask the refusal nor commit unrelated pending changes on the
  caller's session.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.ontology import AgentDomainBinding, OntologyAction, OntologyObject

logger = logging.getLogger(__name__)


def is_write_action(action: OntologyAction) -> bool:
    """THE write predicate — the single source of truth used by
    execute_action, execute_approved_action and the checker below, so
    every path applies an identical definition."""
    return action.risk_level == "write" or bool(action.requires_confirmation)


def resolve_caller(context: dict | None) -> dict:
    """Classify the caller WITHOUT any identity default (no masquerade).

    console_user is trusted only with proof of authenticated HTTP
    identity (non-empty authenticated_user_id)."""
    ctx = context or {}
    if ctx.get("caller_kind") == "console_user":
        user_id = str(ctx.get("authenticated_user_id") or "").strip()
        if user_id:
            return {"kind": "console_user", "agent_name": None,
                    "user_id": user_id}
        return {"kind": "unidentified", "agent_name": None, "user_id": None}
    agent_name = ctx.get("agent_name") or None
    if agent_name:
        return {"kind": "agent", "agent_name": str(agent_name), "user_id": None}
    return {"kind": "unidentified", "agent_name": None, "user_id": None}


def _domain_id_for_action(db: Session, org_id: str, action: OntologyAction) -> str | None:
    obj = (
        db.query(OntologyObject)
        .filter(
            OntologyObject.id == action.object_id,
            OntologyObject.org_id == org_id,
            OntologyObject.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    return obj.domain_id if obj else None


def check_action_permission(
    db: Session, org_id: str, action: OntologyAction, context: dict | None
) -> dict | None:
    """Return {'denied': True, 'reason': ...} when the caller may not
    proceed, else None. The caller audits denials via
    audit_permission_denial and returns an honest refusal."""
    if not is_write_action(action):
        return None

    caller = resolve_caller(context)
    if caller["kind"] == "console_user":
        return None

    if caller["kind"] == "unidentified":
        return {
            "denied": True,
            "reason": (
                f"write action {action.name!r} requires an identified "
                "caller (agent_name) or authenticated console session — "
                "request refused"
            ),
        }

    domain_id = _domain_id_for_action(db, org_id, action)
    if not domain_id:
        return {
            "denied": True,
            "reason": f"action {action.name!r} has no resolvable object/domain",
        }

    bindings = (
        db.query(AgentDomainBinding)
        .filter(
            AgentDomainBinding.org_id == org_id,
            AgentDomainBinding.agent_name == caller["agent_name"],
            AgentDomainBinding.domain_id == domain_id,
            AgentDomainBinding.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    if not bindings:
        return {
            "denied": True,
            "reason": (
                f"agent {caller['agent_name']!r} has no binding on this "
                "domain — write actions are refused (fail closed)"
            ),
        }
    if not any(b.permission_scope == "write" for b in bindings):
        return {
            "denied": True,
            "reason": (
                f"agent {caller['agent_name']!r} is bound read-only to "
                "this domain — write actions are refused"
            ),
        }
    return None


def audit_permission_denial(
    org_id: str, action: OntologyAction, context: dict | None, reason: str,
    *, session_factory=None,
) -> None:
    """Failure-safe structured denial audit.

    Writes via an INDEPENDENT session/transaction (default
    app.database.SessionLocal) so that:
    - an audit persistence failure NEVER turns the security refusal
      into an execution failure, and
    - the caller's session is never committed as a side effect (no
      unrelated pending changes get flushed by the audit).
    Tests inject session_factory to point at the test engine."""
    from datetime import datetime, timezone

    from app.models.audit_event import AuditEvent

    caller = resolve_caller(context)
    if session_factory is None:
        from app.database import SessionLocal as session_factory
    try:
        s = session_factory()
        try:
            s.add(AuditEvent(
                org_id=org_id,
                event_type="policy_violation",
                event_source="ontology",
                actor_type="agent" if caller["kind"] == "agent" else caller["kind"],
                actor_id=caller["agent_name"] or caller.get("user_id"),
                resource_type="ontology_action",
                resource_id=action.id,
                outcome="denied",
                detail_json={
                    "action_name": action.name,
                    "tool_name": action.tool_name,
                    "caller_kind": caller["kind"],
                    "reason": reason,
                },
                occurred_at=datetime.now(timezone.utc),
            ))
            s.commit()
        finally:
            s.close()
    except Exception as exc:  # noqa: BLE001 — audit must never mask the refusal
        logger.warning(
            "ontology permission denial audit write failed (refusal stands): %s",
            exc,
        )
```

**Step 2: Run tests**

Run: `python -m pytest tests/test_ontology_p0_governance.py -v`
Expected: UNCHANGED (module not yet wired into data.py — denial tests still RED).

**Step 3: Commit** the module together with Task 5's wiring (next task) — do not commit a red suite. Proceed to Task 5.

---

### Task 5: Wire the checker into both execution chokepoints

**Objective:** Both `execute_action` and `execute_approved_action` enforce the permission before doing anything else on the write path.

**Files:**
- Modify: `backend/app/services/ontology/data.py` — `execute_action` write branch (lines ~256-260, BEFORE `ApprovalService`), and `execute_approved_action` (after the `action.tool_name` check at ~360-361, BEFORE approval validation).

**Step 1: Wire `execute_action`**

Replace the branch condition with the shared predicate and insert the check at its very top (before the `ApprovalService` import):

```python
    from app.services.ontology.permissions import is_write_action

    if is_write_action(action):
        # P0 governance (fail closed): scope check BEFORE any approval is
        # created — an unbound or read-only agent can never originate a write.
        from app.services.ontology.permissions import (
            audit_permission_denial,
            check_action_permission,
        )

        denial = check_action_permission(db, org_id, action, context)
        if denial:
            audit_permission_denial(org_id, action, context, denial["reason"])
            return {"success": False, "denied": True, "reason": denial["reason"]}

        # P0-3: writes go through the governance approval flow — ... (existing code)
```

Note: NO `db.commit()` on the denial path — the audit uses its own session, and the caller's session must not be committed as a side effect of a refusal.

**Step 2: Wire `execute_approved_action`**

Insert after the `if not action.tool_name:` early return (before the `ApprovalRequest` lookup):

```python
    from app.services.ontology.permissions import (
        audit_permission_denial,
        check_action_permission,
    )

    denial = check_action_permission(db, org_id, action, context)
    if denial:
        audit_permission_denial(org_id, action, context, denial["reason"])
        return {"success": False, "denied": True, "reason": denial["reason"]}
```

**Step 3: Run the suite**

Run: `python -m pytest tests/test_ontology_p0_governance.py -v`
Expected: ALL PASS (denials enforced; write-binding/console-user flows unchanged).

**Step 4: Run the full ontology regression**

Run: `python -m pytest tests/test_ontology_core.py tests/test_ontology_p1_batch.py tests/test_ontology_sensing.py tests/test_p3_batch2.py tests/test_p3_batch3.py tests/test_escalations.py -q`
Expected: all green. If `test_write_action_creates_governance_approval` (test_ontology_core.py:149) fails, it is because it calls `execute_action` WITHOUT `agent_name`/`caller_kind` — update that test's call to pass `context={"user_id": "u1", "caller_kind": "console_user"}` (it simulates the human/console path) — that is a legitimate test-contract update, not a code regression. Same for any other existing test that exercises the write branch with empty context.

**Step 5: Commit**

```bash
git add backend/app/services/ontology/permissions.py backend/app/services/ontology/data.py backend/tests/test_ontology_p0_governance.py backend/tests/test_ontology_core.py
git commit -m "feat(ontology): fail-closed permission_scope enforcement on both action execution paths (gap G7)"
```

---

### Task 6: Failing test + fix — approved execution re-checks scope

**Objective:** A valid approval must NOT override a read-only/no binding (user requirement: both paths protected).

**Files:**
- Test: `backend/tests/test_ontology_p0_governance.py` (append)

**Step 1: Write failing test**

Append:

```python
def test_approved_execution_still_checks_scope(db, monkeypatch):
    """A read-scope agent with a VALID approval is still denied — an
    approval proves a human OK'd THIS write; it does not upgrade the
    agent's domain scope."""
    import app.services.agent_tools as agent_tools
    from app.models.governance import ApprovalRequest
    from app.services.governance.approval_service import ApprovalService
    from app.services.ontology.data import execute_action, execute_approved_action

    act = _make_write_action(db, tool_name="po_tool")
    _add_binding(db, "some_agent", "read")
    res = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    # Ordinary execution denied; but simulate the approval existing anyway
    # (e.g. created earlier by a different path) via a console request:
    res2 = execute_action(
        db, ORG, "u1", act.id, params={},
        context={"user_id": "u1", "authenticated_user_id": "u1",
                "caller_kind": "console_user"},
    )
    approval_id = res2["approval_id"]
    ApprovalService(db).approve(approval_id, reviewed_by="boss")

    calls: list = []

    def fake_execute_tool(name, args, session, user_id, context=None):
        calls.append((name, context.get("approval_id")))
        return {"success": True}

    monkeypatch.setattr(agent_tools, "execute_tool", _async_fake(fake_execute_tool))

    # The read-scope agent re-runs with the approval → DENIED
    res3 = execute_approved_action(
        db, ORG, "u1", act.id, approval_id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    assert res3["success"] is False
    assert res3.get("denied") is True
    assert calls == []

    # A console user re-runs the same approval → runs
    res4 = execute_approved_action(
        db, ORG, "u1", act.id, approval_id, params={},
        context={"user_id": "u1", "authenticated_user_id": "u1",
                "caller_kind": "console_user"},
    )
    assert res4["success"] is True
    assert calls and calls[0][1] == approval_id
```

**Step 2: Run**

Run: `python -m pytest tests/test_ontology_p0_governance.py::test_approved_execution_still_checks_scope -v`
Expected: PASS already (Task 5 wired the checker into `execute_approved_action`) — this test pins the contract against regression. If it FAILS, Task 5's `execute_approved_action` wiring is misplaced (must be BEFORE approval validation).

**Step 3: Commit (only if changes were needed)**

---

### Task 7: Failing test + router marking — console paths declare themselves

**Objective:** The HTTP endpoints mark their caller so humans keep today's behavior. (Without this, `ontology.py:195` and `:619` send only `user_id` → unidentified → humans get denied — the ONE breaking change this task prevents.)

**Files:**
- Modify: `backend/app/routers/ontology.py:195` and `:619`
- Test: `backend/tests/test_ontology_p0_governance.py` (append)

**Step 1: Write failing test** (router-level, using FastAPI TestClient if the repo's test conventions support it; otherwise service-level contract is already covered by `test_console_user_requesting_write_still_gets_approval` and this task is a one-line code change verified by the full suite + live smoke test). Prefer the smoke test if TestClient fixtures for auth do not already exist in the repo:

**Step 2: Implement**

In `backend/app/routers/ontology.py` line 195:

```python
        body.params, context={"user_id": getattr(user, "id", None),
                              "authenticated_user_id": str(getattr(user, "id", "") or ""),
                              "caller_kind": "console_user"},
```

and line 619:

```python
        body.approval_id, body.params,
        context={"user_id": getattr(user, "id", None),
                 "authenticated_user_id": str(getattr(user, "id", "") or ""),
                 "caller_kind": "console_user"},
```

Only these two authenticated HTTP endpoints may set `caller_kind` — tool handlers, MCP and user payloads never do, and `resolve_caller` ignores an unproven `console_user` claim.

**Step 3: Run full ontology suite**

Run: `python -m pytest tests/test_ontology_core.py tests/test_ontology_p0_governance.py -q`
Expected: all green.

**Step 4: Commit**

```bash
git add backend/app/routers/ontology.py backend/tests/test_ontology_p0_governance.py
git commit -m "feat(ontology): HTTP action endpoints declare console_user caller kind"
```

---

### Task 8: Denial-audit test + full regression + live smoke

**Objective:** Prove denials are audited and nothing else regressed.

**Files:**
- Test: `backend/tests/test_ontology_p0_governance.py` (append)

**Step 1: Write test**

Append:

```python
def test_denial_writes_structured_audit_event(db):
    from app.models.audit_event import AuditEvent
    from app.services.ontology.data import execute_action

    act = _make_write_action(db)
    _add_binding(db, "some_agent", "read")
    execute_action(
        db, ORG, "u1", act.id, params={},
        context={"org_id": ORG, "agent_name": "some_agent"},
    )
    evt = (
        db.query(AuditEvent)
        .filter(
            AuditEvent.org_id == ORG,
            AuditEvent.event_type == "policy_violation",
            AuditEvent.event_source == "ontology",
            AuditEvent.outcome == "denied",
        )
        .one()
    )
    assert evt.detail_json["caller_kind"] == "agent"
    assert evt.detail_json["action_name"] == "CreatePO"
    assert "read-only" in evt.detail_json["reason"]
```

**Step 2: Full backend ontology regression**

Run: `python -m pytest tests/test_ontology_core.py tests/test_ontology_p0_governance.py tests/test_ontology_p1_batch.py tests/test_ontology_sensing.py tests/test_p3_batch2.py tests/test_p3_batch3.py tests/test_escalations.py -q`
Expected: all pass (59 existing + ~10 new).

**Step 3: Live smoke test (requires running backend)**

```bash
cd /Users/minhazulislam/Desktop/zhanlu_n8n_main_version/backend
TOKEN=$(curl -s -X POST http://localhost:5002/api/apps/local-zhanlu-app/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
# pick any write action id from /api/ontology/actions, then:
curl -s -X POST http://localhost:5002/api/ontology/actions/<ACTION_ID>/execute \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}'
```
Expected: `blocked: confirmation_required` (console user path intact) — NOT `denied`.

**Step 4: Commit**

```bash
git add backend/tests/test_ontology_p0_governance.py
git commit -m "test(ontology): denial auditing + P0 governance regression coverage"
```

---

## Verification checklist (acceptance criteria)

- [ ] Agent, no binding, write action → denied, zero ApprovalRequests, audited
- [ ] Agent, read-only binding, write action → denied, audited
- [ ] Agent, any write-scope binding (even with other read rows) → approval flow as today
- [ ] Empty context (MCP) write → denied; empty-context READ actions → unchanged
- [ ] Console user (HTTP, with authenticated_user_id) → unchanged request→approval→execute-approved behavior
- [ ] `console_user` claim WITHOUT authenticated_user_id → denied (impersonation refused)
- [ ] Write binding on Domain B does not authorize write in Domain A
- [ ] Read actions with no identity context pass the permission gate unchanged
- [ ] Audit persistence failure never masks the refusal (independent session)
- [ ] Valid approval + read-scope agent → still denied at execute-approved
- [ ] Denials appear as `policy_violation`/`ontology`/`denied` AuditEvents
- [ ] `AUDIT_EVENT_SOURCES`/`AUDIT_EVENT_TYPES` whitelists registered
- [ ] Full existing ontology suite green (read flows untouched)
