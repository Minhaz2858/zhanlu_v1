# Ecisco BI Silent Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ecisco_bi_assistant` a silent, platform-protected system agent — hidden from all UI surfaces, undeletable through any code path, and self-healing on every backend startup. The chat auto-select path must keep working unchanged.

**Architecture:** Three layers of defense in depth:
1. **Data** — flip `is_system: False → True` in the seeder config, back-fill legacy rows via alembic + seeder, so `filterUserAgents()` and `isSystemAgent()` hide it everywhere.
2. **Service** — add a delete guard in `entity_service.soft_delete_record` / `delete_many_records` that refuses to soft-delete any `is_system=True` row.
3. **Self-heal** — `ensure_system_agents()` (already idempotent) back-fills both the row and the flag on every backend startup, so even if a row were somehow deleted, the next restart re-creates it.

The chat auto-select path in `frontend/src/pages/Chat.jsx` uses the raw `AgentApp.list()` output and matches by name — it does not call `filterUserAgents()` — so flipping the flag does not break it. `_apply_owner` already exposes `is_system=True` rows to all users, so the list endpoint still returns the row.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), React + Vitest (frontend), SQLite (tests), Postgres (prod).

## Global Constraints

- All code follows existing patterns in the same file (do not introduce new abstractions).
- TDD discipline: write the failing test first for every behavior change.
- No new public API endpoints. Reuse existing `soft_delete_record` / `delete_many_records` signatures — just add a guard branch.
- Migration revision id format: `042_ecisco_bi_silent_agent` (next after `041_forecast_mlops_hitl`).
- Do not touch uncommitted user work in `backend/app/services/scheduled_tasks.py`, `backend/main.py`, `backend/tests/test_resource_shares_promote.py`.
- Live verification requires `docker restart zhanlu-backend` (no `--reload`) and a 20s sleep before testing.

---

## File Structure

**Modify (4):**
- `backend/app/services/system_agents.py` — flip `is_system: False → True` for `ecisco_bi_assistant`; update outdated comment.
- `backend/app/services/entity_service.py` — add `is_system` guard in `soft_delete_record` and `delete_many_records`.
- `frontend/src/lib/systemAgents.js` — add `ecisco_bi_assistant` to `SYSTEM_AGENT_NAMES`.
- `backend/tests/test_system_agents_is_system_flag.py` — extend expected system set.

**Create (3):**
- `backend/alembic/versions/042_ecisco_bi_silent_agent.py` — back-fill `is_system=TRUE` on existing rows.
- `backend/tests/test_ecisco_bi_silent_agent.py` — TDD coverage for new behavior.
- `frontend/src/lib/systemAgents.test.js` — extend with `ecisco_bi_assistant` cases (already exists; just add to it).

**Modify-test (1):**
- `backend/tests/test_edia_migration.py` — flip the assertion `is_system is False` → `is_system is True` (line 608-612).

**No changes to:**
- `frontend/src/pages/Chat.jsx` — auto-select path uses raw list, unaffected.
- `frontend/src/components/agent/AgentsView.jsx` — `filterUserAgents()` already in scope.
- `frontend/src/components/chat/*` — same.
- Any router or model file.

---

## Task 1: Flip `is_system` to True in the seeder config

**Files:**
- Modify: `backend/app/services/system_agents.py:475-480`

**Interfaces:**
- Consumes: `_build_system_agent_configs()` (no signature change).
- Produces: a config dict for `ecisco_bi_assistant` with `"is_system": True` and an updated rationale comment.

- [ ] **Step 1: Write failing test in `test_ecisco_bi_silent_agent.py`**

```python
"""Regression: ecisco_bi_assistant is a silent platform system agent.

Why this exists
---------------
After the EDIA migration, ecisco_bi_assistant became a platform-critical
orchestrator that frontend/src/pages/Chat.jsx auto-selects silently when
the user opens a chat in the "Ecisco BI" project. End users must never
see this agent in any UI surface (My Space, Company Agents, agent picker),
and it must be undeletable through any code path. These tests guard the
contract:

  1. The seeder marks ecisco_bi_assistant as is_system=True.
  2. ensure_system_agents() back-fills is_system=True on legacy rows.
  3. soft_delete_record() refuses to delete a system agent.
  4. delete_many_records() excludes system agents from bulk deletion.
  5. soft_delete_record() still works for non-system agents.
  6. ensure_system_agents() is idempotent w.r.t. is_system for the agent.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_app import AgentApp
from app.services import entity_service
from app.services.system_agents import ensure_system_agents


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# --- 1. Seeder marks ecisco_bi_assistant as is_system=True ---

def test_ecisco_bi_assistant_is_marked_system_in_seeder(db):
    """After seeding, ecisco_bi_assistant must have is_system=True so
    the frontend's filterUserAgents() / isSystemAgent() hide it from
    every user-facing list."""
    ensure_system_agents(db)
    row = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert row is not None, "ensure_system_agents did not create ecisco_bi_assistant"
    assert row.is_system is True, (
        f"ecisco_bi_assistant should be is_system=True, "
        f"got is_system={row.is_system}"
    )


# --- 2. ensure_system_agents() back-fills is_system=True on legacy rows ---

def test_ensure_system_agents_backfills_legacy_ecisco_bi_assistant(db):
    """A row that exists from before this change was made must be
    promoted to is_system=True on the next ensure_system_agents() call.
    Without this backfill, the frontend would still surface the agent
    in Company Agents / My Space because is_system would remain False."""
    legacy = AgentApp(
        name="ecisco_bi_assistant",
        description="legacy pre-migration row",
        is_system=False,  # the bug case
    )
    db.add(legacy)
    db.commit()

    ensure_system_agents(db)

    refreshed = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert refreshed is not None
    assert refreshed.is_system is True, (
        "Legacy ecisco_bi_assistant row was not promoted to is_system=True"
    )


# --- 3. soft_delete_record() refuses system agents ---

class _StubSystemAgentError(Exception):
    """Raised by soft_delete_record when the row is a system agent."""


def test_soft_delete_record_refuses_system_agent(db, monkeypatch):
    """A row with is_system=True must NOT be soft-deletable through the
    standard entity_service path. The guard raises so the router can
    translate it to HTTP 403."""
    # Seed the agent via the same code path the seeder uses.
    ensure_system_agents(db)
    agent = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert agent.is_system is True, "precondition: agent must be system"

    # soft_delete_record returns False instead of deleting (and the row
    # is left untouched, with is_deleted still False).
    success = entity_service.soft_delete_record(AgentApp, agent.id, db)
    assert success is False, (
        "soft_delete_record returned True for a system agent; the guard "
        "should have refused and returned False"
    )
    db.refresh(agent)
    assert agent.is_deleted is False, (
        "system agent was soft-deleted despite is_system=True"
    )


# --- 4. delete_many_records() excludes system agents from bulk deletion ---

def test_delete_many_records_skips_system_agents(db):
    """A bulk-delete with a query that matches the system agent must
    exclude that agent — the count of deleted rows must not include
    it, and the row must remain is_deleted=False after the call."""
    ensure_system_agents(db)
    user_agent = AgentApp(
        name="User Agent",
        description="not a system agent",
        is_system=False,
    )
    db.add(user_agent)
    db.commit()

    # Bulk-delete: match all non-deleted AgentApp rows.
    count = entity_service.delete_many_records(
        AgentApp, {}, db
    )
    # Only the user agent is deleted; the system agent is excluded.
    assert count == 1, f"expected 1 deletion (user agent), got {count}"

    system_row = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).first()
    assert system_row.is_deleted is False, (
        "ecisco_bi_assistant was bulk-deleted despite is_system=True"
    )


# --- 5. soft_delete_record() still works for non-system agents ---

def test_soft_delete_record_works_for_non_system_agent(db):
    """Regression guard: the new is_system guard must not block deletion
    of normal user-created agents. A row with is_system=False is still
    soft-deletable as before."""
    user_agent = AgentApp(
        name="User Agent",
        description="not a system agent",
        is_system=False,
    )
    db.add(user_agent)
    db.commit()

    success = entity_service.soft_delete_record(AgentApp, user_agent.id, db)
    assert success is True, "soft_delete_record should still delete user agents"
    db.refresh(user_agent)
    assert user_agent.is_deleted is True


# --- 6. ensure_system_agents() is idempotent for ecisco_bi_assistant ---

def test_ensure_system_agents_idempotent_for_ecisco_bi(db):
    """Running ensure_system_agents() twice must keep ecisco_bi_assistant
    at is_system=True and must not create a second row."""
    ensure_system_agents(db)
    ensure_system_agents(db)

    rows = db.query(AgentApp).filter(
        AgentApp.name == "ecisco_bi_assistant"
    ).all()
    assert len(rows) == 1, (
        f"ensure_system_agents created {len(rows)} ecisco_bi_assistant rows, "
        f"expected exactly 1"
    )
    assert rows[0].is_system is True
```

- [ ] **Step 2: Run the new test file to verify it fails**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && \
  venv/bin/python -m pytest tests/test_ecisco_bi_silent_agent.py -v
```

Expected: 5 of 6 tests fail.
- `test_ecisco_bi_assistant_is_marked_system_in_seeder` — FAIL: seeder still has `is_system: False`.
- `test_ensure_system_agents_backfills_legacy_ecisco_bi_assistant` — FAIL: seeder doesn't include `ecisco_bi_assistant` in its protected list.
- `test_soft_delete_record_refuses_system_agent` — FAIL: no guard yet, so success returns True.
- `test_delete_many_records_skips_system_agents` — FAIL: no guard, deletes the system row.
- `test_ensure_system_agents_idempotent_for_ecisco_bi` — FAIL: the config flip is what makes is_system=True.
- `test_soft_delete_record_works_for_non_system_agent` — PASS (regression guard; behavior is unchanged).

If the regression-guard test fails too, that means we accidentally broke existing delete behavior — stop and fix.

- [ ] **Step 3: Flip the flag in the seeder config**

In `backend/app/services/system_agents.py` line 475-480, replace the existing comment + value:

```python
            # ecisco_bi_assistant is a silent platform system agent.
            # Frontend/src/pages/Chat.jsx auto-selects it when the user
            # opens a chat in the "Ecisco BI" project, so users never need
            # to see it. Hiding it via is_system=True means
            # filterUserAgents() drops it from My Space, the agent picker,
            # the active-agent chip, and the Company Agents section. The
            # flag is also enforced at the API layer (see
            # entity_service.soft_delete_record) so it cannot be removed
            # through any HTTP path. ensure_system_agents() back-fills
            # this flag on every backend startup.
            "is_system": True,
```

- [ ] **Step 4: Add the delete guard in `entity_service`**

In `backend/app/services/entity_service.py`, modify `soft_delete_record` (line 351-365):

```python
def soft_delete_record(model, record_id: str, db: Session, owner_id: str | None = None) -> bool:
    """Soft-delete a record by ID (set is_deleted=True).

    ``owner_id`` (optional) scopes the delete to the identity's own rows —
    a non-owner gets False (record hidden → router returns 404).

    If the record has ``is_system=True`` on a model that carries the
    column, the delete is refused and ``False`` is returned. System
    agents (e.g. ``ecisco_bi_assistant``) are platform-critical
    orchestrators that the chat auto-select path depends on; deleting
    them would break silent auto-selection for the Ecisco BI project
    and every other place the runtime auto-injects them. The router
    treats False as 404, so callers cannot distinguish a hidden
    system agent from a missing one — which is the intended behavior.
    """
    record = _apply_owner(model, _apply_tenant(model, db.query(model).filter(
        model.id == record_id, model.is_deleted == False
    )), owner_id).first()
    if not record:
        return False
    if getattr(record, "is_system", False) is True:
        return False
    record.is_deleted = True
    record.updated_date = datetime.utcnow()
    db.commit()
    return True
```

And modify `delete_many_records` (line 368-394) — add the same guard inside the loop:

```python
def delete_many_records(model, query_data: dict, db: Session, owner_id: str | None = None) -> int:
    """Soft-delete multiple records matching the query. Returns count deleted.

    ``owner_id`` (optional) scopes the delete to the identity's own rows —
    a non-owner's matching rows are invisible (never deleted). This closes
    the mass-IDOR gap on user-scoped entities.

    Records with ``is_system=True`` are silently excluded from the bulk
    delete — they are platform-managed and the count returned does not
    include them. (See soft_delete_record for the full rationale.)
    """
    query = db.query(model).filter(model.is_deleted == False)
    query = _apply_tenant(model, query)
    query = _apply_owner(model, query, owner_id)

    if query_data and isinstance(query_data, dict):
        import json
        q_str = json.dumps(query_data)
        conditions = parse_query(model, q_str)
        for cond in conditions:
            query = query.filter(cond)

    records = query.all()
    count = 0
    for record in records:
        if getattr(record, "is_system", False) is True:
            continue
        record.is_deleted = True
        record.updated_date = datetime.utcnow()
        count += 1

    db.commit()
    return count
```

- [ ] **Step 5: Run the new test file to verify all 6 pass**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && \
  venv/bin/python -m pytest tests/test_ecisco_bi_silent_agent.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Run the existing system-agents test to verify no regression**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && \
  venv/bin/python -m pytest tests/test_system_agents_is_system_flag.py -v
```

Expected: all 4 existing tests PASS.

- [ ] **Step 7: Run the existing EDIA migration test to see what breaks**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && \
  venv/bin/python -m pytest tests/test_edia_migration.py::TestSystemAgent -v
```

Expected: `test_ecisco_bi_assistant_not_is_system` now FAILS because the seeder sets `is_system=True`. This is expected — the test needs to be updated in Task 2.

- [ ] **Step 8: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add \
  backend/app/services/system_agents.py \
  backend/app/services/entity_service.py \
  backend/tests/test_ecisco_bi_silent_agent.py && \
  git commit -m "feat(ecisco-bi): make ecisco_bi_assistant a silent system agent

- Flip is_system: False -> True in the seeder so the frontend hides it
  from My Space, the agent picker, and the Company Agents section.
- Add is_system guard in entity_service.soft_delete_record and
  delete_many_records so the agent is undeletable through any HTTP
  path. Returns False (router -> 404) so callers cannot distinguish a
  hidden system agent from a missing one.
- ensure_system_agents() back-fills is_system=True on legacy rows on
  every startup, so existing databases self-heal.
- Add tests/test_ecisco_bi_silent_agent.py with 6 regression tests."
```

---

## Task 2: Update the EDIA migration test that asserts `is_system=False`

**Files:**
- Modify: `backend/tests/test_edia_migration.py:608-612`

**Interfaces:**
- Consumes: `_build_system_agent_configs()` (no change).
- Produces: updated assertion that matches the new seeder contract.

- [ ] **Step 1: Confirm the test currently fails**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && \
  venv/bin/python -m pytest tests/test_edia_migration.py::TestSystemAgent::test_ecisco_bi_assistant_not_is_system -v
```

Expected: FAIL with `assert True is False` (because we flipped the flag in Task 1).

- [ ] **Step 2: Update the test to assert the new contract**

In `backend/tests/test_edia_migration.py` line 608-612, replace the test:

```python
    def test_ecisco_bi_assistant_is_silent_system(self):
        """ecisco_bi_assistant is a silent platform system agent (is_system=True).

        The agent is auto-selected by Chat.jsx when the user opens a chat
        in the Ecisco BI project, so end users must never see it in any
        UI surface. Hiding is done at the frontend via filterUserAgents()
        matching on the is_system flag, and at the API layer via
        entity_service.soft_delete_record refusing to delete system
        agents. This test guards the seeder contract; the
        entity-service guard is tested in test_ecisco_bi_silent_agent.py.
        """
        configs = _build_system_agent_configs()
        orch = next(c for c in configs if c["name"] == "ecisco_bi_assistant")
        assert orch.get("is_system") is True, (
            f"ecisco_bi_assistant should be is_system=True so the frontend "
            f"hides it, got is_system={orch.get('is_system')}"
        )
```

- [ ] **Step 3: Run the updated test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && \
  venv/bin/python -m pytest tests/test_edia_migration.py::TestSystemAgent -v
```

Expected: all `TestSystemAgent` tests PASS, including the renamed one.

- [ ] **Step 4: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add backend/tests/test_edia_migration.py && \
  git commit -m "test(ecisco-bi): assert ecisco_bi_assistant is is_system=True

The agent is now a silent platform system agent; the previous test
asserted the user-facing contract that we are deliberately walking
back. See feat(ecisco-bi) commit for full rationale."
```

---

## Task 3: Add the data migration that back-fills legacy rows

**Files:**
- Create: `backend/alembic/versions/042_ecisco_bi_silent_agent.py`

**Interfaces:**
- Consumes: existing alembic chain. Revises: `041_forecast_mlops_hitl`.
- Produces: data-only migration that stamps `is_system=TRUE` on existing `ecisco_bi_assistant` rows.

- [ ] **Step 1: Write the migration file**

Create `backend/alembic/versions/042_ecisco_bi_silent_agent.py`:

```python
"""Back-fill is_system=TRUE on ecisco_bi_assistant rows.

The agent is now a silent platform system agent — see
app/services/system_agents.py for the rationale. This migration is
data-only and idempotent: it sets is_system=TRUE on every live
(soft-deleted=False) row whose name is 'ecisco_bi_assistant', closing
the window between this commit landing and the next backend restart
(where ensure_system_agents() would do the same back-fill anyway).

The seeder's own backfill is the source of truth in steady state; this
migration is belt-and-suspenders so the agent is hidden from the UI
immediately after deploy, without waiting for a backend restart.

Revision ID: 042_ecisco_bi_silent_agent
Revises: 041_forecast_mlops_hitl
Create Date: 2026-08-05
"""
from alembic import op  # type: ignore[import-untyped]
import sqlalchemy as sa  # type: ignore[import-untyped]


revision = "042_ecisco_bi_silent_agent"
down_revision = "041_forecast_mlops_hitl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent_apps SET is_system = TRUE "
            "WHERE name = 'ecisco_bi_assistant' AND is_deleted = FALSE"
        )
    )


def downgrade() -> None:
    # Walk the flag back to its previous value (False) so the agent
    # becomes user-facing again. Downgrade is intentionally narrow —
    # only ecisco_bi_assistant is touched, so the other system
    # agents stamped by migration 026 are unaffected.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE agent_apps SET is_system = FALSE "
            "WHERE name = 'ecisco_bi_assistant' AND is_deleted = FALSE"
        )
    )
```

- [ ] **Step 2: Verify the alembic chain is correct**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/backend && \
  grep -E "^(revision|down_revision)" alembic/versions/04*.py | head -10
```

Expected: confirm 042's `down_revision = "041_forecast_mlops_hitl"` matches 041's `revision = "041_forecast_mlops_hitl"`.

- [ ] **Step 3: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add backend/alembic/versions/042_ecisco_bi_silent_agent.py && \
  git commit -m "feat(ecisco-bi): alembic 042 back-fills is_system on ecisco_bi_assistant

Data-only migration. Runs alongside the seeder's own backfill on the
next backend restart, so the agent is hidden from the UI immediately
after deploy without waiting for a restart."
```

---

## Task 4: Add `ecisco_bi_assistant` to the frontend system agent set

**Files:**
- Modify: `frontend/src/lib/systemAgents.js:19-30`

**Interfaces:**
- Consumes: the existing `SYSTEM_AGENT_NAMES` constant.
- Produces: a constant that also includes `ecisco_bi_assistant`, so `filterUserAgents()` / `isSystemAgent()` drop the agent from user-facing lists on legacy rows that have not yet been migrated to `is_system=TRUE`.

- [ ] **Step 1: Write a failing test in `systemAgents.test.js`**

In `frontend/src/lib/systemAgents.test.js`, add a new `describe` block at the end of the file (after line 79):

```js
describe('ecisco_bi_assistant silent-agent coverage', () => {
  it('treats ecisco_bi_assistant as a system agent via the name fallback', () => {
    // Even if the row hasn't been migrated to is_system=TRUE yet,
    // the name-match in SYSTEM_AGENT_NAMES must hide it.
    expect(isSystemAgent({ id: '1', name: 'ecisco_bi_assistant' })).toBe(true);
  });

  it('filters ecisco_bi_assistant out of a user-facing list', () => {
    const list = [
      { id: '1', name: 'ecisco_bi_assistant' },  // system (name fallback)
      { id: '2', name: 'My Custom Agent' },      // user
      { id: '3', name: 'ecisco_bi_assistant', is_system: true }, // system (flag)
    ];
    expect(filterUserAgents(list).map((a) => a.id)).toEqual(['2']);
  });

  it('includes ecisco_bi_assistant in SYSTEM_AGENT_NAMES (regression guard)', () => {
    expect(SYSTEM_AGENT_NAMES).toContain('ecisco_bi_assistant');
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/frontend && \
  node_modules/.bin/vitest run src/lib/systemAgents.test.js
```

Expected: the new `ecisco_bi_assistant silent-agent coverage` block FAILS (because the name is not yet in `SYSTEM_AGENT_NAMES`).

- [ ] **Step 3: Add `ecisco_bi_assistant` to `SYSTEM_AGENT_NAMES`**

In `frontend/src/lib/systemAgents.js` line 19-30, extend the array:

```js
export const SYSTEM_AGENT_NAMES = Object.freeze([
  'agent_builder',
  'skill_agent',
  'automation_agent',
  'general_assistant',
  'power_user',
  // data_agent is code-only (BUILTIN_AGENTS, not a row in
  // agent_apps) and is never returned by the list endpoint, but
  // include it for defense in depth in case a future change
  // surfaces it.
  'data_agent',
  // ecisco_bi_assistant is the silent platform orchestrator that
  // Chat.jsx auto-selects when the user opens a chat in the
  // "Ecisco BI" project. The backend also stamps is_system=TRUE on
  // the row (seeder + alembic 042), but the name-match is the
  // fallback that hides the agent on legacy rows that pre-date the
  // migration, and on any code path that constructs an AgentApp
  // dict locally without round-tripping the database. Keeping both
  // checks makes the UI robust to stale data.
  'ecisco_bi_assistant',
]);
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
cd /home/ysk2025/zhanlu_7_30/frontend && \
  node_modules/.bin/vitest run src/lib/systemAgents.test.js
```

Expected: all tests PASS, including the new `ecisco_bi_assistant silent-agent coverage` block.

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add frontend/src/lib/systemAgents.js frontend/src/lib/systemAgents.test.js && \
  git commit -m "feat(ecisco-bi): add ecisco_bi_assistant to frontend SYSTEM_AGENT_NAMES

The backend now stamps is_system=TRUE on the row (seeder + alembic
042), but the frontend keeps a name-match fallback so legacy rows
that pre-date the migration are still hidden from the UI."
```

---

## Task 5: Live verification (after docker restart)

**Goal:** Confirm the four real-world behaviors end-to-end against the running app.

- [ ] **Step 1: Restart the backend**

```bash
cd /home/ysk2025/zhanlu_7_30 && docker restart zhanlu-backend
sleep 20  # wait for prestart.sh to finish
```

Expected: backend container stays up. Check with `docker ps --filter name=zhanlu-backend` — status should be `Up`.

- [ ] **Step 2: Verify the seeder backfill ran (is_system=TRUE in DB)**

```bash
PGPASSWORD=***DB- password-REDACTED*** \
  psql -h localhost -p 5400 -U zhanlu -d zhanlu -c \
  "SELECT name, is_system, is_deleted FROM agent_apps WHERE name = 'ecisco_bi_assistant';"
```

Expected: one row, `is_system = t`, `is_deleted = f`. If `is_system = f`, the seeder backfill didn't run — check container logs with `docker logs zhanlu-backend --tail 100`.

- [ ] **Step 3: Verify the agent is hidden from the user-facing list**

Open a chat in a browser with `?project=Ecisco%20BI`. Confirm the active agent chip does NOT show "ecisco_bi_assistant" as a selectable item. Then check the My Space / Company Agents section — the card in the original screenshot should no longer appear.

- [ ] **Step 4: Verify the auto-select path still works**

Open a chat in a browser with `?project=Ecisco%20BI` and send a message. The response should come back without an "agent not found" error, and EDIA tools (ask_orchestrator, ask_perception, etc.) should still be reachable. Watch the backend log to confirm `effective_agent` resolves to `ecisco_bi_assistant`.

- [ ] **Step 5: Verify the API delete guard**

```bash
# Find the agent id
AGENT_ID=$(PGPASSWORD=***DB- password-REDACTED*** \
  psql -h localhost -p 5400 -U zhanlu -d zhanlu -t -A -c \
  "SELECT id FROM agent_apps WHERE name = 'ecisco_bi_assistant' AND is_deleted = FALSE LIMIT 1;")

# Login as admin
TOKEN=$(curl -sS -X POST \
  http://localhost:5002/api/apps/local-zhanlu-app/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token') or json.load(sys.stdin).get('token',''))")

# Try to delete — should be refused
curl -sS -o /tmp/del.json -w "HTTP %{http_code}\n" -X DELETE \
  "http://localhost:5002/api/apps/local-zhanlu-app/entities/AgentApp/${AGENT_ID}" \
  -H "Authorization: Bearer ${TOKEN}"
cat /tmp/del.json
```

Expected: `HTTP 404` with `{"detail": "AgentApp not found"}` (the router returns 404 when `soft_delete_record` returns False, so callers cannot distinguish hidden system agents from missing ones).

- [ ] **Step 6: Verify a non-system agent is still deletable**

```bash
# Create a throwaway agent
TMP_AGENT=$(curl -sS -X POST \
  "http://localhost:5002/api/apps/local-zhanlu-app/entities/AgentApp" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"name":"__silence_test__","description":"throwaway"}' | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")

# Delete it — should succeed
curl -sS -o /tmp/del2.json -w "HTTP %{http_code}\n" -X DELETE \
  "http://localhost:5002/api/apps/local-zhanlu-app/entities/AgentApp/${TMP_AGENT}" \
  -H "Authorization: Bearer ${TOKEN}"
cat /tmp/del2.json
```

Expected: `HTTP 200` with `{"deleted": true, "id": "..."}`. The throwaway agent is gone.

- [ ] **Step 7: Commit verification notes (optional)**

If any of the verifications needed fixes, commit the fix separately with a clear message. If everything passes, no commit is needed for this task.

---

## Self-Review Notes

**Spec coverage check:**
- ✅ Backend seeder flip → Task 1 step 3.
- ✅ Backend delete guard (`soft_delete_record` + `delete_many_records`) → Task 1 step 4.
- ✅ Backend migration 042 → Task 3.
- ✅ Frontend `SYSTEM_AGENT_NAMES` extension → Task 4.
- ✅ Tests (new file + extensions) → Tasks 1, 2, 4.
- ✅ Live verification → Task 5.
- ✅ Out of scope (per user decision): other system agents don't get extra treatment, but the flag-based guard incidentally protects them at the API layer — disclosed in code comment.

**Placeholder scan:** No "TODO" or "implement later" strings. Every code block contains the actual code. Every test step contains the actual test code.

**Type consistency:** No cross-task function name changes. The router signatures are unchanged. The migration `revision` / `down_revision` strings match.

**Risk acknowledged:** The `delete_many_records` change returns a lower `count` than the API user might expect when their query matches a system row. This is the intended behavior (per design: "protected rows are excluded from bulk deletion, no error, just not deleted") and matches the spec.
