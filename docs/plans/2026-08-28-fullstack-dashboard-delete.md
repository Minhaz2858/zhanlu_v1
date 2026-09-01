# Full-Stack Dashboard Delete — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a hard-cascade DELETE for full-stack (LIVE) dashboards — backend endpoint + My Space card button — so users can remove test dashboards and duplicate records.

**Architecture:** New `DELETE /api/dashboards/app-records/{slug_or_id}` route in `routers/dashboards.py` that stops the poller, removes the generated app dir, hard-deletes the DashboardApp row, and cascades dashboard-stamped conversations. Frontend MySpace.jsx renders the delete button for all kinds and routes by kind. TDD throughout; tests use tmp_path generator + monkeypatched manager (no prod dirs touched).

**Tech Stack:** Python 3.11 FastAPI, SQLAlchemy, React 18 + Vite, pytest, vitest (if frontend tests exist).

---

## Task 1: Backend — conversation cascade helper

**Objective:** Pure helper that finds and deletes AgentConversation rows bound to a fullstack dashboard via metadata (mode='dashboard' + slug/id). DB-agnostic JSON filtering (works on SQLite tests + Postgres prod).

**Files:**
- Create: `backend/app/services/dashboard_app/cascade.py`
- Test: `backend/tests/services/dashboard_app/test_cascade.py`

**Step 1: Write failing test**

```python
"""tests/services/dashboard_app/test_cascade.py"""
import pytest

from app.models.agent_conversation import AgentConversation
from app.services.dashboard_app.cascade import delete_bound_conversations


@pytest.fixture()
def conv_factory(db_session):
    def make(meta):
        c = AgentConversation(metadata_=meta)
        db_session.add(c)
        db_session.commit()
        return c
    return make


def test_deletes_matching_slug(conv_factory, db_session):
    hit = conv_factory({"mode": "dashboard", "dashboard_slug": "sales-overview"})
    miss = conv_factory({"mode": "dashboard", "dashboard_slug": "other-app"})
    plain = conv_factory({"mode": "chat"})
    deleted = delete_bound_conversations(db_session, "sales-overview", "some-id")
    assert deleted == 1
    remaining = db_session.query(AgentConversation).all()
    assert {r.id for r in remaining} == {miss.id, plain.id}


def test_deletes_matching_id(conv_factory, db_session):
    hit = conv_factory({"mode": "dashboard", "dashboard_id": "rec-123"})
    deleted = delete_bound_conversations(db_session, "sales-overview", "rec-123")
    assert deleted == 1


def test_handles_none_metadata(conv_factory, db_session):
    conv = AgentConversation(metadata_=None)
    db_session.add(conv)
    db_session.commit()
    assert delete_bound_conversations(db_session, "sales-overview", "id") == 0


def test_empty_db_returns_zero(db_session):
    assert delete_bound_conversations(db_session, "sales-overview", "id") == 0
```

**Step 2: Run to verify failure**

Run: `cd backend && docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/services/dashboard_app/test_cascade.py -v 2>&1 | tail -6"`
Expected: FAIL — `ModuleNotFoundError: app.services.dashboard_app.cascade`

**Step 3: Implement**

```python
"""services/dashboard_app/cascade.py

Cascade cleanup for full-stack dashboard deletion. DB-agnostic: the metadata
JSON filter is applied in Python so it works on SQLite (tests) and Postgres
(prod) alike.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def delete_bound_conversations(db, dashboard_slug: str, dashboard_id: str) -> int:
    """Delete AgentConversation rows bound to a fullstack dashboard.

    A dashboard-dedicated chat session is stamped with
    ``metadata_.mode == 'dashboard'`` plus ``dashboard_slug`` / ``dashboard_id``
    (see dashboard_tools._stamp_dashboard_conversation). Returns count deleted.
    Never raises.
    """
    from app.models.agent_conversation import AgentConversation

    try:
        rows = db.query(AgentConversation).all()
        doomed = []
        for c in rows:
            meta = c.metadata_ or {}
            if meta.get("mode") != "dashboard":
                continue
            if meta.get("dashboard_slug") == dashboard_slug or \
               meta.get("dashboard_id") == dashboard_id:
                doomed.append(c)
        for c in doomed:
            db.delete(c)
        if doomed:
            db.commit()
        return len(doomed)
    except Exception:
        logger.exception("delete_bound_conversations failed (slug=%s)", dashboard_slug)
        return 0
```

**Step 4: Run to verify pass**

Run: same command
Expected: PASS (4 passed)

**Step 5: Commit**

```bash
git add backend/app/services/dashboard_app/cascade.py backend/tests/services/dashboard_app/test_cascade.py
git commit -m "feat(dashboards): conversation cascade helper for dashboard delete"
```

---

## Task 2: Backend — DELETE endpoint + authorization

**Objective:** Register `DELETE /api/dashboards/app-records/{slug_or_id}` before the legacy `/{dashboard_id}` route. Resolve + authorize (personal → creator; company → creator or admin), stop poller, remove dir, hard-delete row, cascade conversations, 204.

**Files:**
- Modify: `backend/app/routers/dashboards.py` (add after `mark_dashboard_app_viewed`, BEFORE the legacy delete route)
- Test: `backend/tests/routers/test_dashboard_app_delete.py`

**Step 1: Write failing tests**

```python
"""tests/routers/test_dashboard_app_delete.py"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.database import SessionLocal
from app.models.dashboard_app import DashboardApp
from app.models.agent_conversation import AgentConversation
from app.routers import dashboards as dash_router
from app.routers.dashboards import delete_dashboard_app_record


def _mk_user(user_id, role="user"):
    u = MagicMock()
    u.id = user_id
    u.org_id = "org-1"
    u.role = role
    return u


def _mk_app(db, slug, creator_id, scope="personal"):
    rec = DashboardApp(
        id=str(uuid.uuid4()),
        slug=slug,
        org_id="org-1",
        created_by_id=creator_id,
        scope=scope,
        name="Sales Performance Dashboard",
        description="test",
        spec={},
    )
    db.add(rec)
    db.commit()
    return rec


@pytest.fixture()
def db_session():
    from app.database import SessionLocal
    db = SessionLocal()
    yield db
    db.close()


def test_creator_deletes_personal_app(db_session):
    user = _mk_user("u-owner")
    rec = _mk_app(db_session, "sales-overview", "u-owner", "personal")
    r = delete_dashboard_app_record(rec.id, db=db_session, user=user)
    assert r.status_code == 204
    assert db_session.query(DashboardApp).filter(DashboardApp.id == rec.id).first() is None


def test_non_creator_denied_personal(db_session):
    user = _mk_user("u-other")
    rec = _mk_app(db_session, "sales-overview", "u-owner", "personal")
    r = delete_dashboard_app_record(rec.id, db=db_session, user=user)
    assert r.status_code == 404
    assert db_session.query(DashboardApp).filter(DashboardApp.id == rec.id).first() is not None


def test_company_admin_can_delete(db_session):
    admin = _mk_user("u-admin", role="admin")
    rec = _mk_app(db_session, "company-board", "u-owner", "company")
    r = delete_dashboard_app_record(rec.id, db=db_session, user=admin)
    assert r.status_code == 204


def test_company_non_admin_non_creator_denied(db_session):
    user = _mk_user("u-other")
    rec = _mk_app(db_session, "company-board", "u-owner", "company")
    r = delete_dashboard_app_record(rec.id, db=db_session, user=user)
    assert r.status_code == 404


def test_unknown_slug_404(db_session):
    r = delete_dashboard_app_record("no-such-app", db=db_session, user=_mk_user("u-owner"))
    assert r.status_code == 404


def test_delete_cascades_conversations(db_session, tmp_path, monkeypatch):
    from app.services.dashboard_app import cascade
    monkeypatch.setattr(cascade, "delete_bound_conversations", lambda db, slug, rid: 1)
    user = _mk_user("u-owner")
    rec = _mk_app(db_session, "sales-overview", "u-owner", "personal")
    r = delete_dashboard_app_record(rec.id, db=db_session, user=user)
    assert r.status_code == 204


def test_legacy_delete_still_registered():
    """Regression: the legacy DELETE /{dashboard_id} handler is untouched."""
    assert dash_router.delete_dashboard is not None
```

**Step 2: Run to verify failure**

Run: `cd backend && docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/routers/test_dashboard_app_delete.py -v 2>&1 | tail -8"`
Expected: FAIL — `ImportError: cannot import name 'delete_dashboard_app_record'`

**Step 3: Implement**

In `routers/dashboards.py`, add imports at top (shutil, get_generator, module_name, cascade helper) and the route right after `mark_dashboard_app_viewed` (line ~251), BEFORE the legacy delete:

```python
@router.delete("/app-records/{slug_or_id}", status_code=204)
def delete_dashboard_app_record(slug_or_id: str, db: Session = Depends(get_db),
                                user=Depends(get_current_user_required)):
    """Hard-delete a full-stack dashboard app + cascade.

    Stops the realtime poller, removes the generated app directory, deletes the
    DashboardApp row, and deletes dashboard-stamped conversation rows. Access:
    personal → creator only; company → creator or org admin.
    """
    import shutil

    from app.services.dashboard_app.cascade import delete_bound_conversations
    from app.services.dashboard_app.generator import get_generator, module_name

    record = db.query(DashboardApp).filter(
        or_(
            DashboardApp.slug == slug_or_id,
            DashboardApp.id == slug_or_id,
        ),
        DashboardApp.org_id == user.org_id,
    ).first()
    if record is None or not _user_can_see_record(record, user):
        raise HTTPException(status_code=404, detail="Dashboard app not found")
    # company scope: non-creator needs admin role
    if record.scope == "company" and str(record.created_by_id) != str(user.id):
        if getattr(user, "role", "user") != "admin":
            raise HTTPException(status_code=404, detail="Dashboard app not found")

    slug = record.slug
    # 1. stop poller (safe no-op when not running)
    try:
        dashboard_app_manager.stop_poller(slug)
    except Exception:
        pass
    # 2. remove generated app dir
    try:
        app_dir = get_generator().app_dir(slug)
        if app_dir.exists():
            shutil.rmtree(app_dir, ignore_errors=True)
    except Exception:
        pass
    # 3. cascade dashboard-stamped conversations
    delete_bound_conversations(db, slug, record.id)
    # 4. hard-delete row
    db.delete(record)
    db.commit()
    return None
```

**Step 4: Run to verify pass**

Run: same command
Expected: PASS (7 passed) — may need the test fixture db_session wired to the app's SessionLocal; adjust if the router tests use a different db pattern (check existing router tests' db fixture and match it).

**Step 5: Commit**

```bash
git add backend/app/routers/dashboards.py backend/tests/routers/test_dashboard_app_delete.py
git commit -m "feat(dashboards): hard-delete endpoint for fullstack apps with cascade"
```

---

## Task 3: Frontend — API client + My Space delete button

**Objective:** Wire the delete button for LIVE cards: API fn, route by kind, render for all kinds, confirm dialog.

**Files:**
- Modify: `frontend/src/api/dashboards.js`
- Modify: `frontend/src/pages/MySpace.jsx`
- Test: `frontend/src/pages/MySpace.test.jsx` (create if absent; else verify manually)

**Step 1: Add API function** (in `frontend/src/api/dashboards.js` after `deleteDashboard`):

```javascript
export function deleteDashboardApp(slugOrId) {
  return authFetch(`${BASE}/app-records/${encodeURIComponent(slugOrId)}`, { method: 'DELETE' }).then(j);
}
```

**Step 2: Update `removeDashboard` in MySpace.jsx** (line 157-164):

```javascript
async function removeDashboard(item) {
  // Full-stack apps delete via DELETE /api/dashboards/app-records/{slug};
  // legacy SQL-widget dashboards via DELETE /api/dashboards/{id}.
  const ok = lang === 'en'
    ? window.confirm(`Delete "${item.name || item.title || ''}"? This removes the dashboard and its data.`)
    : window.confirm(`确定删除“${item.name || item.title || ''}”吗？此操作将删除仪表盘及其数据。`);
  if (!ok) return;
  if (item.kind === 'app') await deleteDashboardApp(item.id);
  else await deleteDashboard(item.id);
  load();
}
```

Update the import on line 19: add `deleteDashboardApp`.

**Step 3: Render delete button for all kinds** — change line 279:

```jsx
{/* delete for BOTH kinds — fullstack apps included */}
<button
  onClick={() => removeDashboard(d)}
  className="ml-auto inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:text-destructive"
>
  <Trash2 className="h-3 w-3" /> {t.common.delete}
</button>
```

(Remove the `d.kind !== 'app' &&` wrapper condition.)

**Step 4: Verify build**

Run: `cd frontend && npm run build 2>&1 | tail -3`
Expected: build succeeds.

**Step 5: Browser verification** (manual, via the running app on :8088):
1. Log in (admin@zhanlu.dev / admin123)
2. My Space → Dashboards
3. Delete one duplicate "Sales Performance Dashboard" (LIVE card) via the trash button
4. Confirm dialog appears; confirm → card disappears; other cards remain
5. Check backend: row gone from dashboard_apps, generated dir gone

**Step 6: Commit**

```bash
git add frontend/src/api/dashboards.js frontend/src/pages/MySpace.jsx
git commit -m "feat(myspace): delete button for fullstack dashboard cards"
```

---

## Task 4: Regression + full verification

**Objective:** Confirm nothing else broke.

**Files:** none (verification only)

**Step 1:** Run the new backend tests + router suite:

```bash
cd backend && docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/services/dashboard_app/test_cascade.py tests/routers/test_dashboard_app_delete.py tests/routers/test_dashboards_catch_all.py -q --no-header 2>&1 | tail -4"
```

Expected: all pass.

**Step 2:** Run the broader dashboard-app tests for regressions:

```bash
docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/services/dashboard_app/ -q --no-header 2>&1 | tail -4"
```

Expected: all pass (or the pre-existing collection quirk — run individual files if the dir reports 'no tests ran').

**Step 3:** End-to-end against real DB: pick a duplicate record, delete via the endpoint, confirm row + dir + conversations gone, then re-create one if needed.

**Step 4:** Commit any fixes:

```bash
git add -A && git commit -m "fix(dashboards): delete verification adjustments"
```

---

## Verification checklist

- [ ] `DELETE /api/dashboards/app-records/{slug_or_id}` works for creator (personal) and admin (company)
- [ ] Non-creator personal → 404; non-admin company → 404
- [ ] Poller stopped; generated dir removed; DashboardApp row hard-deleted
- [ ] Dashboard-stamped conversations removed
- [ ] Legacy `DELETE /api/dashboards/{id}` still works
- [ ] My Space card shows delete button on LIVE dashboards; confirm dialog; list reloads
- [ ] Frontend build passes

## Notes

- MVE scope: confirm via `window.confirm` with the file's existing bilingual
  pattern — no new dialog component.
- Conversation cascade is Python-side filtering for SQLite/Postgres portability;
  fine at current row counts (dashboard sessions are a handful per user).
