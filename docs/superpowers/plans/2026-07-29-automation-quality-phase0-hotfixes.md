# Automation Quality Phase 0 — Hotfix Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four user-visible automation defects: 401 deliverable previews, world-readable generated files, mid-sentence notification truncation, and log-style (unstructured) run output.

**Architecture:** Signed one-time preview tokens (stdlib HMAC over `settings.JWT_SECRET`) let iframes load the authenticated preview endpoint via `?token=`. Generated files move out of the public `/api/uploads` static mount into `settings.generated_path`; `AutomationFile.file_url` becomes the authenticated download route. Notification preview switches to first-N-sentences. The run prompt gains a mandatory report-structure template for prose formats.

**Tech Stack:** FastAPI, SQLAlchemy, pydantic-settings, stdlib hmac/hashlib/base64, React + vitest.

**Spec:** `docs/superpowers/specs/2026-07-29-automation-quality-reliability-design.md` (Phase 0 only; Phases 1–2 get their own plans).

## Global Constraints

- No new dependencies (stdlib HMAC; existing services only).
- Test DB (Postgres unreachable): `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"`, then prefix pytest with `DATABASE_URL="sqlite:///./test_runtime.db"`.
- sqlite enforces FKs: `AutomationFile` requires real `AutomationTask` + `AutomationExecution` rows.
- **RAM limited**: run pytest with explicit file paths only; never the full suite; no `-n` workers.
- Do NOT touch the line `pool.submit(_run_agent_in_conversation, ...)` in `automation_executor.py:690` — a gate test asserts it as a single-line literal.
- `TimestampedBase` provides `org_id`/`app_id`/`is_deleted` on all models.
- Frontend test cmd: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/InlineArtifactPreview.test.jsx`.

---

### Task 1: Signed preview-token service

**Files:**
- Create: `backend/app/services/preview_tokens.py`
- Test: `backend/tests/test_preview_tokens.py`

**Interfaces:**
- Produces: `mint_preview_token(*, file_id: str, user_id: str, ttl_seconds: int = 900) -> str` and `verify_preview_token(token: str, file_id: str) -> Optional[str]` (returns `user_id` or `None`). Task 2 consumes both.

- [ ] **Step 1: Write the failing test**

```python
"""Signed preview tokens — mint/verify round-trip and rejection paths."""
import time

from app.services.preview_tokens import mint_preview_token, verify_preview_token


def test_round_trip_returns_user_id():
    token = mint_preview_token(file_id="f1", user_id="u1")
    assert verify_preview_token(token, "f1") == "u1"


def test_rejects_tampered_signature():
    token = mint_preview_token(file_id="f1", user_id="u1")
    bad = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert verify_preview_token(bad, "f1") is None


def test_rejects_token_minted_for_a_different_file():
    token = mint_preview_token(file_id="f1", user_id="u1")
    assert verify_preview_token(token, "f2") is None


def test_rejects_expired_token():
    token = mint_preview_token(file_id="f1", user_id="u1", ttl_seconds=-1)
    assert verify_preview_token(token, "f1") is None


def test_rejects_garbage():
    assert verify_preview_token("not-a-token", "f1") is None
    assert verify_preview_token("", "f1") is None
    assert verify_preview_token("a.b.c", "f1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_preview_tokens.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.preview_tokens`

- [ ] **Step 3: Implement `backend/app/services/preview_tokens.py`**

```python
"""Signed, short-lived preview tokens for iframe-friendly file previews.

Iframes and <img> tags cannot send Authorization headers, so the
authenticated preview endpoint also accepts a ``?token=`` query param
carrying one of these HMAC-signed tokens. Tokens are stateless: the
payload is bound to (file_id, user_id) and carries an expiry; the
endpoint still runs the tenant check against the resolved user.

Signed with ``settings.JWT_SECRET`` (stdlib hmac — no new dependency).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from app.config import settings

_DEFAULT_TTL_SECONDS = 900  # 15 minutes


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(payload_b64: str) -> str:
    digest = hmac.new(
        settings.JWT_SECRET.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64url_encode(digest)


def mint_preview_token(
    *, file_id: str, user_id: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS
) -> str:
    payload = {
        "fid": str(file_id),
        "uid": str(user_id),
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_preview_token(token: str, file_id: str) -> Optional[str]:
    """Return the user_id bound to ``token`` for ``file_id``, else None."""
    if not token or token.count(".") != 1:
        return None
    payload_b64, sig = token.split(".", 1)
    if not hmac.compare_digest(_sign(payload_b64), sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if payload.get("fid") != str(file_id):
        return None
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if exp < int(time.time()):
        return None
    uid = payload.get("uid")
    return str(uid) if uid else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_preview_tokens.py -v`
Expected: 5 passed

---

### Task 2: Preview-token endpoints (mint + token-accepting preview)

**Files:**
- Modify: `backend/app/routers/automation_api.py` (imports ~35, new endpoint before line 750, preview signature at 750-763)
- Test: `backend/tests/test_automation_preview_endpoint.py`

**Interfaces:**
- Consumes: `mint_preview_token`, `verify_preview_token` (Task 1); `get_current_user_optional` (`app.deps`); `_assert_tenant` (existing, module-level).
- Produces: `POST /api/automations/files/{file_id}/preview-token` → `{token, url}`; `GET /api/automations/files/{file_id}/preview?token=...` accepted alongside Bearer. Task 6 (frontend) consumes the mint endpoint.

- [ ] **Step 1: Write the failing test**

```python
"""Preview-token endpoints: mint + iframe-friendly token auth on preview."""
import os
import sys
import uuid
from pathlib import Path

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.deps import get_current_user_optional
from app.models.automation_execution import AutomationExecution
from app.models.automation_file import AutomationFile
from app.models.automation_task import AutomationTask
from app.models.user import User
from app.routers import automation_api
from app.services.preview_tokens import mint_preview_token


def _make_app(db):
    app = FastAPI()
    app.include_router(automation_api.router, prefix="/api")

    def _override_db():
        yield db

    app.dependency_overrides[automation_api.get_db] = _override_db
    return app


def _seed(db, tmp_path, org_id="org1"):
    user = User(
        id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.dev",
        full_name="T", role="user", password_hash="x", org_id=org_id,
    )
    task = AutomationTask(
        id=str(uuid.uuid4()), name="T", type="data_sync", org_id=org_id,
    )
    db.add_all([user, task])
    db.flush()
    execution = AutomationExecution(
        id=str(uuid.uuid4()), automation_task_id=task.id, status="completed",
        org_id=org_id,
    )
    db.add(execution)
    db.flush()
    f = tmp_path / "report.html"
    f.write_text("<html><body>hi</body></html>", encoding="utf-8")
    file_row = AutomationFile(
        id=str(uuid.uuid4()), execution_id=execution.id,
        automation_task_id=task.id, name="report.html", file_type="html",
        size=f.stat().st_size, file_path=str(f), mime_type="text/html",
        org_id=org_id,
    )
    db.add(file_row)
    db.commit()
    return user, file_row


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_mint_requires_auth(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    resp = client.post(f"/api/automations/files/{file_row.id}/preview-token")
    assert resp.status_code == 401


def test_mint_returns_token_and_url(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: user
    client = TestClient(app)
    resp = client.post(f"/api/automations/files/{file_row.id}/preview-token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["url"].endswith(f"/preview?token={body['token']}")


def test_preview_accepts_valid_token_without_bearer(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    token = mint_preview_token(file_id=file_row.id, user_id=user.id)
    resp = client.get(f"/api/automations/files/{file_row.id}/preview?token={token}")
    assert resp.status_code == 200
    assert b"hi" in resp.content


def test_preview_rejects_bad_or_missing_token(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    assert client.get(f"/api/automations/files/{file_row.id}/preview").status_code == 401
    assert client.get(
        f"/api/automations/files/{file_row.id}/preview?token=garbage.sig"
    ).status_code == 401
    other = mint_preview_token(file_id="different-file", user_id=user.id)
    assert client.get(
        f"/api/automations/files/{file_row.id}/preview?token={other}"
    ).status_code == 401


def test_preview_token_still_enforces_tenant(db, tmp_path):
    # Token is valid, but the minting user is in a different org than the file.
    user, file_row = _seed(db, tmp_path, org_id="org1")
    stranger = User(
        id=str(uuid.uuid4()), email=f"{uuid.uuid4().hex[:8]}@t.dev",
        full_name="S", role="user", password_hash="x", org_id="org2",
    )
    db.add(stranger)
    db.commit()
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: None
    client = TestClient(app)
    token = mint_preview_token(file_id=file_row.id, user_id=stranger.id)
    resp = client.get(f"/api/automations/files/{file_row.id}/preview?token={token}")
    assert resp.status_code == 404  # _assert_tenant hides existence


def test_preview_bearer_path_unchanged(db, tmp_path):
    user, file_row = _seed(db, tmp_path)
    app = _make_app(db)
    app.dependency_overrides[get_current_user_optional] = lambda: user
    client = TestClient(app)
    resp = client.get(f"/api/automations/files/{file_row.id}/preview")
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_automation_preview_endpoint.py -v`
Expected: FAIL — 404 on POST preview-token (route missing); preview token tests 401 (no token support).

- [ ] **Step 3: Implement**

In `backend/app/routers/automation_api.py`, line 35, change the deps import:

```python
from app.deps import get_current_user_optional, get_current_user_required
```

Add the mint endpoint immediately before `@router.get("/files/{file_id}/preview")` (line 750):

```python
@router.post("/files/{file_id}/preview-token")
def mint_file_preview_token(
    file_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Mint a short-lived signed token so iframe previews (which cannot send
    Authorization headers) can load the preview endpoint via ``?token=``."""
    from app.services.preview_tokens import mint_preview_token

    file_row = db.query(AutomationFile).filter(
        AutomationFile.id == file_id,
        AutomationFile.is_deleted == False,  # noqa: E712
    ).first()
    if not file_row:
        raise HTTPException(status_code=404, detail="File not found")
    _assert_tenant(file_row, user)
    token = mint_preview_token(file_id=file_id, user_id=str(user.id))
    return {
        "token": token,
        "url": f"/api/automations/files/{file_id}/preview?token={token}",
    }
```

Change the preview endpoint signature and auth resolution (lines 750-763):

```python
@router.get("/files/{file_id}/preview")
def preview_automation_file(
    file_id: str,
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Render a generated automation file inline without exposing disk paths.

    Accepts either the Bearer header (fetch/XHR) or a signed ``?token=``
    minted by ``POST /files/{id}/preview-token`` (iframe/img navigation).
    """
    from app.services.preview_tokens import verify_preview_token

    file_row = db.query(AutomationFile).filter(
        AutomationFile.id == file_id,
        AutomationFile.is_deleted == False,  # noqa: E712
    ).first()
    if not file_row:
        raise HTTPException(status_code=404, detail="File not found")

    if user is None and token:
        uid = verify_preview_token(token, file_id)
        if uid:
            candidate = db.get(User, uid)
            if candidate is not None and not getattr(candidate, "is_anonymous", False):
                user = candidate
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_tenant(file_row, user)
```

(The remainder of the function — path check, type check, conversion, responses — is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: same command as Step 2
Expected: 6 passed

---

### Task 3: Relocate generated files out of the public static mount

**Files:**
- Modify: `backend/app/config.py` (after line 142, and after `upload_path` property ~373)
- Modify: `backend/app/services/document_generator.py:49` (`_automation_root`) and `:123-131` (`_make_url`)
- Modify: `backend/app/services/automation_executor.py:1599-1625` (`_render_and_save_files`)
- Create: `backend/scripts/relocate_automation_files.py`
- Test: `backend/tests/test_document_generator_paths.py`

**Interfaces:**
- Produces: `settings.GENERATED_DIR: str = "data/generated"` and `settings.generated_path -> Path`; `generate_document` now returns `file_url=""` (callers must use the authenticated route); `_render_and_save_files` persists `file_url=f"/api/automations/files/{file_id}/download"`; `relocate(db) -> dict` (idempotent). Task 6 relies on the download route being the only file_url shape.

- [ ] **Step 1: Write the failing test**

```python
"""Generated files live outside the public uploads tree; file_url is the
authenticated download route; the relocation script is idempotent."""
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings
from app.database import SessionLocal
from app.models.automation_execution import AutomationExecution
from app.models.automation_file import AutomationFile
from app.models.automation_task import AutomationTask
from app.services import automation_executor as ax
from app.services import document_generator as dg


def test_generate_document_writes_outside_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GENERATED_DIR", str(tmp_path / "gen"))
    path, url, mime = dg.generate_document(
        output_format="html", content="# Hi", title="R", task_id="t1", exec_id="e1",
    )
    assert str(path).startswith(str(tmp_path / "gen"))
    assert not str(path).startswith(str(settings.upload_path.resolve()))
    assert url == ""  # no public URL — authenticated route only
    assert mime == "text/html"


def test_render_and_save_files_persists_authenticated_download_url():
    task = MagicMock()
    task.id = "t1"; task.name = "R"; task.output_format = "html"
    task.org_id = "o"; task.app_id = "a"; task.created_by_id = "u"
    execution = MagicMock(); execution.id = "exec1"
    fake_path = MagicMock()
    fake_path.exists.return_value = True
    fake_path.stat.return_value = MagicMock(st_size=1)
    db = MagicMock()
    with patch(
        "app.services.document_generator.generate_document",
        return_value=(fake_path, "", "text/html"),
    ):
        files, _ = ax._render_and_save_files(
            db, task, execution, "## R\nx", "prompt", fsm_meta=None,
        )
    assert files[0].file_url == f"/api/automations/files/{files[0].id}/download"


def _seed_file_row(db, path: Path, org_id="o"):
    task = AutomationTask(id=str(uuid.uuid4()), name="T", type="data_sync", org_id=org_id)
    db.add(task); db.flush()
    execution = AutomationExecution(
        id=str(uuid.uuid4()), automation_task_id=task.id, status="completed", org_id=org_id,
    )
    db.add(execution); db.flush()
    row = AutomationFile(
        id=str(uuid.uuid4()), execution_id=execution.id, automation_task_id=task.id,
        name="r.html", file_type="html", size=1, file_path=str(path),
        file_url="/api/uploads/automation/t1/e1/r.html", mime_type="text/html",
        org_id=org_id,
    )
    db.add(row); db.commit()
    return row


def test_relocate_moves_files_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GENERATED_DIR", str(tmp_path / "gen"))
    old_root = tmp_path / "up" / "automation"
    old_file = old_root / "t1" / "e1" / "r.html"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "up"))

    from scripts.relocate_automation_files import relocate

    db = SessionLocal()
    try:
        row = _seed_file_row(db, old_file)
        stats1 = relocate(db)
        assert stats1["moved"] == 1
        assert not old_file.exists()
        db.refresh(row)
        new_path = Path(row.file_path)
        assert str(new_path).startswith(str(tmp_path / "gen"))
        assert new_path.exists()
        assert row.file_url == f"/api/automations/files/{row.id}/download"
        stats2 = relocate(db)
        assert stats2["moved"] == 0 and stats2["skipped"] >= 1
        # cleanup so other tests don't see the row
        db.delete(row); db.commit()
    finally:
        db.rollback(); db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest tests/test_document_generator_paths.py -v`
Expected: FAIL — `GENERATED_DIR` attribute missing; `relocate` module missing.

- [ ] **Step 3: Implement**

`backend/app/config.py` — after `UPLOAD_DIR: str = "uploads"` (line 142):

```python
    # Generated deliverables (private — served only via authenticated routes,
    # never via the public /api/uploads static mount)
    GENERATED_DIR: str = "data/generated"
```

After the `upload_path` property (~line 373):

```python
    @property
    def generated_path(self) -> Path:
        p = Path(self.GENERATED_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p
```

`backend/app/services/document_generator.py` — `_automation_root` line 49:

```python
    root = settings.generated_path / "automation"
```

`_make_url` (lines 123-131) — generated files are private now:

```python
def _make_url(path: Path) -> str:
    """Generated files are private: no public URL exists. Callers must link
    the authenticated route ``/api/automations/files/{id}/download`` instead."""
    return ""
```

Module docstring line 16: change `All files are written to ``uploads/automation/<task_id>/<exec_id>/<name>.<ext>`` and served via the existing ``/api/uploads`` static mount.` to `All files are written to ``<generated_path>/automation/<task_id>/<exec_id>/<name>.<ext>`` and served only via the authenticated ``/api/automations/files/{id}/...`` routes.`

`backend/app/services/automation_executor.py` — `_render_and_save_files` (lines 1599-1625):

```python
    file_id = str(uuid.uuid4())
    file_path, _public_url, mime_type = generate_document(
        output_format=output_format,
        content=payload,
        title=title,
        task_id=task.id,
        exec_id=execution.id,
    )

    size = file_path.stat().st_size if file_path.exists() else 0
    file_row = AutomationFile(
        id=file_id,
        execution_id=execution.id,
        automation_task_id=task.id,
        name=f"{title}.{output_format}",
        file_type=output_format,
        size=size,
        # Authenticated route only — generated files are not in the public
        # /api/uploads static mount.
        file_url=f"/api/automations/files/{file_id}/download",
        file_path=str(file_path),
        mime_type=mime_type,
        org_id=task.org_id,
        app_id=task.app_id,
        created_by_id=task.created_by_id,
    )
```

`backend/scripts/relocate_automation_files.py` (new):

```python
"""One-off migration: move automation-generated files out of the public
uploads tree and rewrite AutomationFile rows to the authenticated download
route. Idempotent — safe to run repeatedly.

Usage: python3 scripts/relocate_automation_files.py  (from backend/)
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import SessionLocal
from app.models.automation_file import AutomationFile


def relocate(db) -> dict:
    old_root = (settings.upload_path / "automation").resolve()
    new_root = (settings.generated_path / "automation").resolve()
    stats = {"moved": 0, "updated": 0, "skipped": 0, "missing": 0}
    rows = db.query(AutomationFile).filter(AutomationFile.is_deleted == False).all()  # noqa: E712
    for row in rows:
        p = Path(row.file_path or "")
        resolved = p.resolve() if str(p) else None
        if resolved is not None and str(resolved).startswith(str(new_root)):
            stats["skipped"] += 1
            continue
        try:
            rel = resolved.relative_to(old_root) if resolved else None
        except ValueError:
            rel = None
        if rel is None:
            stats["missing"] += 1
            row.file_url = f"/api/automations/files/{row.id}/download"
            stats["updated"] += 1
            continue
        target = new_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and not target.exists():
            shutil.move(str(p), str(target))
            stats["moved"] += 1
        row.file_path = str(target)
        row.file_url = f"/api/automations/files/{row.id}/download"
        stats["updated"] += 1
    db.commit()
    return stats


def main() -> None:
    db = SessionLocal()
    try:
        print(relocate(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: same as Step 2
Expected: 3 passed

---

### Task 4: Sentence-boundary notification summary

**Files:**
- Modify: `backend/app/services/automation_executor.py` (new helper near `_notify_chat`, replace lines 1677-1680)
- Test: `backend/tests/test_notify_summary.py`

**Interfaces:**
- Produces: `ax._summarize_preview(text: str, cap: int = 600, max_sentences: int = 3) -> str`.

- [ ] **Step 1: Write the failing test**

```python
"""_summarize_preview: first-N-sentences, never mid-sentence truncation."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def test_short_text_returned_unchanged():
    assert ax._summarize_preview("All good.") == "All good."


def test_keeps_whole_sentences_up_to_cap():
    text = "Revenue grew 12% this week. " + ("Filler sentence here. " * 40)
    out = ax._summarize_preview(text, cap=120)
    assert out.startswith("Revenue grew 12% this week.")
    assert out.endswith("…")
    assert len(out) <= 130
    # never cut mid-word/mid-sentence
    assert out.rstrip(" …").endswith(".")


def test_single_long_sentence_falls_back_to_word_boundary():
    text = "word " * 300
    out = ax._summarize_preview(text, cap=100)
    assert len(out) <= 102
    assert out.endswith("…")
    assert not out[:-1].endswith("wor")


def test_cjk_sentence_endings():
    text = "连接正常。" + ("这是填充内容。" * 100)
    out = ax._summarize_preview(text, cap=30)
    assert out.startswith("连接正常。")


def test_empty_input():
    assert ax._summarize_preview("") == ""
    assert ax._summarize_preview(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_notify_summary.py -v`
Expected: FAIL — `AttributeError: _summarize_preview`

- [ ] **Step 3: Implement**

In `backend/app/services/automation_executor.py`, immediately before `def _notify_chat(`:

```python
_SENTENCE_ENDINGS = (". ", "! ", "? ", "。", "！", "？", "\n")


def _summarize_preview(text: str, cap: int = 600, max_sentences: int = 3) -> str:
    """First-N-sentences summary that never cuts mid-sentence.

    Falls back to a word-boundary cut only when a single sentence exceeds
    ``cap`` on its own.
    """
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    sentences: list[str] = []
    consumed = 0
    rest = text
    while rest and len(sentences) < max_sentences:
        cut = -1
        for end in _SENTENCE_ENDINGS:
            idx = rest.find(end)
            if idx != -1 and (cut == -1 or idx + len(end.rstrip()) < cut):
                cut = idx + len(end.rstrip())
        if cut <= 0:
            break
        sentence = rest[:cut].strip()
        if not sentence or consumed + len(sentence) > cap:
            break
        sentences.append(sentence)
        consumed += len(sentence)
        rest = rest[cut:].lstrip()
    if sentences:
        return " ".join(sentences) + " …"
    return text[:cap].rsplit(" ", 1)[0] + "…"
```

Replace `_notify_chat`'s preview block (lines 1677-1680):

```python
    # Build the preview: first few whole sentences (never mid-sentence).
    preview = _summarize_preview(assistant_text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_notify_summary.py -v`
Expected: 5 passed

---

### Task 5: Report-structure run prompt

**Files:**
- Modify: `backend/app/services/automation_executor.py` (`_FORMAT_GUIDANCE`/`_format_guidance`, lines 98-141)
- Test: `backend/tests/test_format_guidance.py`

**Interfaces:**
- Produces: `ax._REPORT_STRUCTURE_GUIDANCE: str`; `_format_guidance(fmt)` appends it for `{"html","docx","pdf","md"}` only.

- [ ] **Step 1: Write the failing test**

```python
"""Report-structure guidance is injected for prose formats only."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def test_prose_formats_get_report_structure():
    for fmt in ("html", "docx", "pdf", "md", ""):
        guidance = ax._format_guidance(fmt)
        assert "Executive summary" in guidance, fmt
        assert "Key metrics" in guidance
        assert "one-page summary" in guidance
        assert "Never narrate tool calls" in guidance


def test_rigid_formats_unchanged():
    for fmt in ("pptx", "xlsx", "csv", "json"):
        guidance = ax._format_guidance(fmt)
        assert "Executive summary" not in guidance, fmt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_format_guidance.py -v`
Expected: FAIL — assertions on missing report structure.

- [ ] **Step 3: Implement**

In `backend/app/services/automation_executor.py`, after the `_FORMAT_GUIDANCE` dict (line 133), add:

```python
_REPORT_FORMATS = {"html", "docx", "pdf", "md"}

_REPORT_STRUCTURE_GUIDANCE = (
    "Structure the deliverable as a professional report:\n"
    "1. `## Executive summary` — at most 3 sentences, outcome first.\n"
    "2. `## Key metrics` — a markdown table: metric | value | delta vs. previous run.\n"
    "3. `## Changes since last run` — concrete bullets (or 'No changes detected').\n"
    "4. `## Anomalies and issues` — or 'None detected'.\n"
    "5. `## Recommended actions` — or 'None required'.\n"
    "Rules: if nothing changed since the previous run, ship a one-page summary, "
    "not a padded report. Never narrate tool calls, connection attempts, or "
    "internal steps in the deliverable body."
)
```

Replace `_format_guidance` (lines 136-141):

```python
def _format_guidance(output_format: str) -> str:
    """Per-format output instruction for the run prompt — aligned with what
    document_generator's parsers expect for each output_format. Prose
    formats also get the mandatory report-structure template."""
    fmt = (output_format or "html").strip().lower()
    base = _FORMAT_GUIDANCE.get(fmt, _FORMAT_GUIDANCE["html"])
    if fmt in _REPORT_FORMATS or not fmt:
        return f"{base}\n\n{_REPORT_STRUCTURE_GUIDANCE}"
    return base
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python3 -m pytest tests/test_format_guidance.py -v`
Expected: 2 passed

---

### Task 6: Frontend — signed-token preview + authenticated download

**Files:**
- Modify: `frontend/src/components/chat/InlineArtifactPreview.jsx`
- Test: `frontend/src/components/chat/InlineArtifactPreview.test.jsx`

**Interfaces:**
- Consumes: `POST /api/automations/files/{id}/preview-token` → `{token, url}` (Task 2); `file_url` = authenticated download route (Task 3); `authFetch` from `@/api/authFetch` (returns a fetch-like Response with `.ok/.json()/.blob()`; handles one silent 401 refresh-retry internally).

**Deviation note (locked):** the existing test file contains two RED tests written for an authFetch→blob preview design. The approved spec locked the signed-token design instead because blob URLs discard the backend's CSP sandbox headers (`_PREVIEW_RESPONSE_HEADERS`), and a `blob:` iframe with `allow-same-origin allow-scripts` would run deliverable HTML with full parent-origin privileges (XSS into the app — the exact vector C4 hardened against). Those two tests are rewritten here to the token flow.

- [ ] **Step 1: Rewrite the failing tests**

In `frontend/src/components/chat/InlineArtifactPreview.test.jsx`, replace the three tests "exposes a download link…", "loads the preview through authFetch into a blob-url iframe", and "shows an error state…" with:

```jsx
  it('downloads through authFetch as a blob (authenticated route)', async () => {
    authFetch.mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['x'], { type: 'text/html' })),
    });
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /Download deliverable/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalledWith('/api/automations/files/file-1/download');
      expect(URL.createObjectURL).toHaveBeenCalled();
    });
  });

  it('mints a signed token and points the iframe at the signed url', async () => {
    // iframes cannot send Authorization headers; the component mints a
    // short-lived signed token and uses the returned ?token= url so the
    // backend CSP sandbox headers still apply (unlike a blob: iframe).
    authFetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        token: 'signed-token',
        url: '/api/automations/files/file-1/preview?token=signed-token',
      }),
    });
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /Open preview/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalledWith(
        '/api/automations/files/file-1/preview-token',
        { method: 'POST' },
      );
      const iframe = document.querySelector('iframe');
      expect(iframe).toBeTruthy();
      expect(iframe.getAttribute('src')).toBe(
        '/api/automations/files/file-1/preview?token=signed-token',
      );
    });
  });

  it('shows an error state (not a 401 iframe) when token minting fails', async () => {
    authFetch.mockResolvedValue({ ok: false, status: 401 });
    render(<InlineArtifactPreview artifact={baseArtifact} />);
    fireEvent.click(screen.getByRole('button', { name: /Open preview/i }));
    await waitFor(() => {
      expect(authFetch).toHaveBeenCalled();
      expect(document.querySelector('iframe')).toBeNull();
      expect(screen.getByText(/could not load preview/i)).toBeTruthy();
    });
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/InlineArtifactPreview.test.jsx`
Expected: FAIL — component never calls authFetch; download is an `<a>` not a button.

- [ ] **Step 3: Implement `InlineArtifactPreview.jsx`**

Change line 1:

```jsx
import { useEffect, useState } from 'react';
```

After the existing imports add:

```jsx
import { authFetch } from '@/api/authFetch';
```

Inside the component, after the existing `const [open, setOpen] = useState(defaultOpen);`:

```jsx
  const [signedPreviewUrl, setSignedPreviewUrl] = useState(null);
  const [previewFailed, setPreviewFailed] = useState(false);
```

After the derived values (`const sandboxValue = ...` block), add the token-mint effect and download handler:

```jsx
  useEffect(() => {
    if (!open || !hasPreview || !artifact?.id) return undefined;
    let cancelled = false;
    setPreviewFailed(false);
    setSignedPreviewUrl(null);
    authFetch(`/api/automations/files/${artifact.id}/preview-token`, { method: 'POST' })
      .then((res) => {
        if (!res || !res.ok) throw new Error(`mint failed: ${res && res.status}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled && data?.url) setSignedPreviewUrl(data.url);
        else if (!cancelled) setPreviewFailed(true);
      })
      .catch(() => {
        if (!cancelled) setPreviewFailed(true);
      });
    return () => { cancelled = true; };
  }, [open, hasPreview, artifact?.id]);

  async function handleDownload() {
    if (!fileUrl) return;
    try {
      const res = await authFetch(fileUrl);
      if (!res || !res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = artifact.file_name || title;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch {
      /* download failure is non-fatal; the Open action remains available */
    }
  }
```

Change `handleOpen`'s fallback (lines 92-100) — `fileUrl` is an authenticated route now, so `window.open` would 401:

```jsx
  function handleOpen() {
    if (onOpen) {
      onOpen(artifact);
      return;
    }
    handleDownload();
  }
```

Replace the download `<a>` (lines 145-156) with a button:

```jsx
          {fileUrl && (
            <button
              type="button"
              onClick={handleDownload}
              aria-label="Download deliverable"
              className="inline-flex items-center justify-center rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            >
              <Download className="h-4 w-4" />
            </button>
          )}
```

Replace the preview pane block (lines 159-176):

```jsx
      {hasPreview && open && (
        <div className="border-t border-border bg-background">
          {previewFailed ? (
            <div className="flex items-center gap-2 px-4 py-3 text-xs text-muted-foreground">
              <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
              Could not load preview. Use Open or Download instead.
            </div>
          ) : signedPreviewUrl ? (
            <iframe
              title={`Preview of ${title}`}
              src={signedPreviewUrl}
              className="h-[360px] w-full border-0"
              sandbox={sandboxValue}
              loading="lazy"
            />
          ) : (
            <div className="flex items-center gap-2 px-4 py-3 text-xs text-muted-foreground">
              Loading preview…
            </div>
          )}
        </div>
      )}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/InlineArtifactPreview.test.jsx`
Expected: 5 passed

---

### Task 7: Phase-0 verification sweep

**Files:** none (verification only)

- [ ] **Step 1: Backend — new tests + executor-regression files, one pytest run**

Run:
```bash
cd /root/zhanlu/backend && rm -f test_runtime.db && \
DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)" && \
DATABASE_URL="sqlite:///./test_runtime.db" python3 -m pytest \
  tests/test_preview_tokens.py \
  tests/test_automation_preview_endpoint.py \
  tests/test_document_generator_paths.py \
  tests/test_notify_summary.py \
  tests/test_format_guidance.py \
  tests/test_automation_phase5_quality_and_delta.py \
  tests/test_automation_ds_preflight.py \
  tests/test_automation_truthful_failure.py \
  -v
```
Expected: all pass (5 + 6 + 3 + 5 + 2 + 7 + 7 + 16 = 51).

- [ ] **Step 2: Frontend**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/InlineArtifactPreview.test.jsx`
Expected: 5 passed.

- [ ] **Step 3: Lint check**

Run: `cd /root/zhanlu/frontend && npx eslint src/components/chat/InlineArtifactPreview.jsx` and `cd /root/zhanlu/backend && python3 -m py_compile app/services/preview_tokens.py app/routers/automation_api.py app/services/automation_executor.py app/services/document_generator.py app/config.py scripts/relocate_automation_files.py`
Expected: clean.

## Self-Review

- **Spec coverage:** §3.1 → Tasks 1, 2, 6 ✓; §3.2 → Tasks 3, 6 (download flow) ✓; §3.3 → Task 4 ✓; §3.4 → Task 5 ✓. Migration script → Task 3 ✓.
- **Placeholder scan:** every step has runnable code/commands ✓.
- **Type consistency:** `mint_preview_token(*, file_id, user_id, ttl_seconds)` / `verify_preview_token(token, file_id) -> Optional[str]` used identically in Tasks 1, 2, and the Task 2 tests ✓; `_summarize_preview(text, cap, max_sentences)` consistent ✓; frontend expects `{token, url}` exactly as the mint endpoint returns ✓.
- **Known deviations:** two pre-existing RED tests rewritten from blob-flow to signed-token flow (rationale in Task 6). `/api/uploads` static mount kept for genuinely public assets (spec §3.2 decision).
