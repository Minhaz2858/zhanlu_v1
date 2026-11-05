"""R3 — Canonical creation lifecycle: Control Plane service + confirm transaction.

Implements/verifies TS §4.2 behaviors 1–7 (SQLite covers 1–4, 6–7
single-threaded; behavior 5's concurrent semantics are a Postgres-profile
concern and are documented, not faked), draft CRUD with If-Match versioning,
and the authorize() policy.
"""
from __future__ import annotations

import os
import sys
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.automation_task import AutomationTask  # noqa: E402
from app.services.automation import control_plane  # noqa: E402
from app.services.automation.control_plane import (  # noqa: E402
    ControlPlaneError,
    authorize,
    confirm_draft,
    create_draft,
    discard_draft,
    get_draft,
    patch_draft,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        # A real project row so confirm's server-side existence check passes.
        from app.models.project import Project

        s.add(Project(id="prj-1", name="R3 Test Project"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _user(org="default-org", uid=None, role="user"):
    from types import SimpleNamespace

    return SimpleNamespace(id=uid or f"u-{uuid.uuid4().hex[:6]}", org_id=org, role=role)


def _valid_spec() -> dict:
    """Fresh deep copy per call — tests mutate nested sections."""
    import copy

    return copy.deepcopy({
        "name": "Daily ERP Sales Sync Report",
        "project_id": "prj-1",
        "objective": "Synchronize ERP sales data and detect anomalies",
        "trigger": {
            "type": "schedule",
            "cron": "0 8 * * *",
            "timezone": "Asia/Shanghai",
            "misfire_policy": "fire_once_late",
            "grace_window_seconds": 3600,
        },
        "output": {"format": "html", "artifact_byte_cap": 10485760},
        "delivery": {"chat": True, "email": False},
        "notifications": {"notify_on": "on_failure", "emails": []},
        "approvals": {"policy": "block", "pre_approved_scope": ["db.read"], "approval_ttl_seconds": 900},
        "retry": {"max_attempts": 2, "backoff_base_seconds": 60, "backoff_cap_seconds": 1800},
        "budgets": {"wall_clock_seconds": 600, "max_tool_calls": 60, "max_llm_calls": 40},
    })


VALID_SPEC = None  # removed: use _valid_spec() (deep copy) everywhere


def _make_draft(db, user, spec=None):
    return create_draft(
        db, user, spec or _valid_spec(),
        origin_session_id=None, origin_message_id=None,
    )


# ---------------------------------------------------------------------------
# Draft CRUD
# ---------------------------------------------------------------------------


def test_create_draft_stores_validation(db):
    user = _user()
    draft = _make_draft(db, user)
    assert draft.status == "draft"
    assert draft.validation is not None and draft.validation["ok"] is True
    assert draft.spec_version == 1
    assert draft.org_id == user.org_id
    assert draft.created_by_id == user.id


def test_create_draft_with_invalid_spec_stores_errors(db):
    user = _user()
    spec = _valid_spec()
    spec["trigger"] = {"type": "schedule", "cron": "garbage !!", "timezone": "Asia/Shanghai"}
    draft = create_draft(db, user, spec)
    assert draft.validation["ok"] is False
    assert any(e["field"] == "trigger.cron" for e in draft.validation["errors"])


def test_patch_draft_increments_version_and_revalidates(db):
    user = _user()
    draft = _make_draft(db, user)
    patched = patch_draft(db, user, draft.id, if_match=1, spec_patch={"name": "Renamed Sync"})
    assert patched.spec_version == 2
    assert patched.spec["name"] == "Renamed Sync"
    assert patched.validation["ok"] is True


def test_patch_draft_stale_version_rejected(db):
    user = _user()
    draft = _make_draft(db, user)
    with pytest.raises(ControlPlaneError) as exc:
        patch_draft(db, user, draft.id, if_match=99, spec_patch={"name": "X"})
    assert exc.value.code == "stale_version"


def test_patch_draft_confirmed_draft_rejected(db):
    user = _user()
    draft = _make_draft(db, user)
    confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=1)
    with pytest.raises(ControlPlaneError) as exc:
        patch_draft(db, user, draft.id, if_match=1, spec_patch={"name": "X"})
    assert exc.value.code == "already_confirmed"


def test_discard_draft(db):
    user = _user()
    draft = _make_draft(db, user)
    discarded = discard_draft(db, user, draft.id)
    assert discarded.status == "discarded"


def test_get_draft_not_found(db):
    with pytest.raises(ControlPlaneError) as exc:
        get_draft(db, _user(), "no-such-draft")
    assert exc.value.code == "not_found"


# ---------------------------------------------------------------------------
# §4.2 confirm behaviors
# ---------------------------------------------------------------------------


def test_b4_confirm_creates_exactly_one_definition(db):
    user = _user()
    draft = _make_draft(db, user)
    key = str(uuid.uuid4())
    task, resp = confirm_draft(db, user, draft.id, key, if_match=1)
    assert resp["already_confirmed"] is False
    assert task.id
    assert task.confirmation_key == key
    assert task.spec_version == 1
    assert task.org_id == user.org_id
    assert task.created_by_id == user.id
    db.refresh(draft)
    assert draft.status == "confirmed"
    assert draft.confirmed_definition_id == task.id
    # Exactly one definition for this org+name.
    rows = db.query(AutomationTask).filter(AutomationTask.name == _valid_spec()["name"]).all()
    assert len(rows) == 1


def test_b1_replay_same_key_returns_original(db):
    user = _user()
    draft = _make_draft(db, user)
    key = str(uuid.uuid4())
    task1, r1 = confirm_draft(db, user, draft.id, key, if_match=1)
    task2, r2 = confirm_draft(db, user, draft.id, key, if_match=1)
    assert r2["already_confirmed"] is True
    assert task2.id == task1.id


def test_b2_replay_different_key_rejected(db):
    user = _user()
    draft = _make_draft(db, user)
    confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=1)
    with pytest.raises(ControlPlaneError) as exc:
        confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=1)
    assert exc.value.code == "already_confirmed_different_key"


def test_b3_stale_if_match_rejected_with_current_version(db):
    user = _user()
    draft = _make_draft(db, user)
    with pytest.raises(ControlPlaneError) as exc:
        confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=42)
    assert exc.value.code == "stale_version"
    assert exc.value.details.get("current_version") == 1


def test_key_bound_to_draft_at_first_attempt(db):
    user = _user()
    draft = _make_draft(db, user)
    key = str(uuid.uuid4())
    confirm_draft(db, user, draft.id, key, if_match=1)
    db.refresh(draft)
    assert draft.confirmation_key == key


def test_confirmation_key_mismatch_before_confirm(db):
    user = _user()
    draft = _make_draft(db, user)
    # Bind a key via a failed confirm (stale version) — key ownership happens
    # BEFORE version validation per §4.2 step 3.
    with pytest.raises(ControlPlaneError):
        confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=42)
    db.expire_all()
    draft = get_draft(db, user, draft.id)
    bound_key = draft.confirmation_key
    assert bound_key
    # A different key on the same draft is rejected.
    with pytest.raises(ControlPlaneError) as exc:
        confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=1)
    assert exc.value.code == "confirmation_key_mismatch"


def test_confirm_invalid_spec_rejected_draft_stays_draft(db):
    user = _user()
    spec = _valid_spec()
    spec["budgets"]["wall_clock_seconds"] = 999999
    draft = _make_draft(db, user, spec)
    with pytest.raises(ControlPlaneError) as exc:
        confirm_draft(db, user, draft.id, str(uuid.uuid4()), if_match=1)
    assert exc.value.code == "validation_failed"
    db.expire_all()
    assert get_draft(db, user, draft.id).status == "draft"
    assert db.query(AutomationTask).count() == 0


def test_edge_two_drafts_same_key_loser_discarded(db):
    user = _user()
    key = str(uuid.uuid4())
    d1 = _make_draft(db, user)
    d2 = _make_draft(db, user)
    task, r = confirm_draft(db, user, d1.id, key, if_match=1)
    assert r["already_confirmed"] is False
    with pytest.raises(ControlPlaneError) as exc:
        confirm_draft(db, user, d2.id, key, if_match=1)
    assert exc.value.code == "confirmation_key_already_used"
    assert exc.value.details.get("definition_id") == task.id
    db.expire_all()
    assert get_draft(db, user, d2.id).status == "discarded"


def test_edge_winner_soft_deleted_replay_archived(db):
    user = _user()
    draft = _make_draft(db, user)
    key = str(uuid.uuid4())
    task, _ = confirm_draft(db, user, draft.id, key, if_match=1)
    task.is_deleted = True
    db.commit()
    task2, r2 = confirm_draft(db, user, draft.id, key, if_match=1)
    assert r2["already_confirmed"] is True
    assert r2.get("definition_archived") is True
    assert task2.id == task.id


# ---------------------------------------------------------------------------
# authorize() policy (§3.4)
# ---------------------------------------------------------------------------


def test_authorize_read_allows_org_member(db):
    owner = _user(uid="owner-1")
    other = _user(uid="member-2", org=owner.org_id)
    task = AutomationTask(name="pol", type="report", prompt="p", org_id=owner.org_id,
                          created_by_id=owner.id)
    db.add(task)
    db.commit()
    assert authorize(other, task, "read") is True


def test_authorize_destructive_owner_only_by_default(db):
    owner = _user(uid="owner-1")
    member = _user(uid="member-2", org=owner.org_id, role="user")
    editor = _user(uid="editor-3", org=owner.org_id, role="editor")
    task = AutomationTask(name="pol2", type="report", prompt="p", org_id=owner.org_id,
                          created_by_id=owner.id)
    db.add(task)
    db.commit()
    assert authorize(owner, task, "delete") is True
    assert authorize(member, task, "delete") is False
    assert authorize(editor, task, "delete") is True  # role ≥ editor


def test_authorize_cross_org_denied(db):
    owner = _user(uid="owner-1")
    outsider = _user(uid="outsider", org="other-org")
    task = AutomationTask(name="pol3", type="report", prompt="p", org_id=owner.org_id,
                          created_by_id=owner.id)
    db.add(task)
    db.commit()
    for action in ("read", "run", "delete"):
        assert authorize(outsider, task, action) is False
