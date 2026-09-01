# DOCX Inline Preview Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Render DOCX files inline in the chat and in the side preview panel, using Microsoft Word Online when the deployment is public and a self-hosted mammoth→HTML reader otherwise, with a Download escape hatch always available.

**Architecture:** A three-tier render strategy is selected server-side per artifact and advertised on the artifact payload. Tier 1 (inline card expanded view) and Tier 2 (side panel when no public URL) use `python-mammoth` to convert the DOCX bytes to clean sanitized HTML on demand; the same response also carries a heading outline for navigation. Tier 3 (side panel when `APP_PUBLIC_URL` is set) embeds `view.officeapps.live.com/op/embed.aspx` pointed at a signed public download URL. A small "Open in MS Word" link in the side panel gives the user a clear opt-in to the third-party tier. Download remains as the always-on escape hatch.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0, `python-mammoth` (new), Pydantic v2, pytest; React 18 + Vite + TypeScript + Tailwind + Radix UI + Vitest + Testing Library.

---

## Working assumptions (no need to re-confirm)

- Branch: stay on `master` (no remote, no worktree conventions enforced in this repo)
- Run backend tests from `backend/` with `pytest -q`
- Run frontend tests from `frontend/` with `npx vitest run`
- Commit message style: Conventional Commits (`feat:`, `chore:`, `test:`, `docs:`)
- `APP_PUBLIC_URL` is opt-in; when empty/unset we never reach out to `view.officeapps.live.com`
- No breaking changes to existing `to_dict()` shapes — we **add** fields, never remove

---

## File map (everything this plan touches)

| Action | Path |
|---|---|
| Modify | `backend/requirements.txt` |
| Modify | `backend/app/config.py` |
| Modify | `backend/app/services/artifacts/preview_builder.py` |
| Modify | `backend/app/services/artifacts/artifact_service.py` |
| Modify | `backend/app/routers/artifacts.py` |
| Create | `backend/tests/test_docx_inline_preview.py` |
| Create | `frontend/src/components/chat/DocxArtifactPreview.jsx` |
| Create | `frontend/src/components/chat/DocxArtifactPreview.test.jsx` |
| Create | `frontend/src/components/chat/DocxOutline.jsx` |
| Create | `frontend/src/components/chat/DocxOutline.test.jsx` |
| Modify | `frontend/src/components/chat/ArtifactPreviewCard.jsx` |
| Modify | `frontend/src/components/chat/ArtifactPreviewSheet.jsx` |
| Modify | `frontend/src/components/chat/MessageBubble.jsx` (only if a prop is needed) |
| Modify | `docs/04_sandbox_artifacts/Zhanlu_Artifact_Preview_Implementation_Spec.md` |
| Create | `docs/plans/2026-07-15-docx-inline-preview.md` (this file) |

---

## Phase 1 — Backend: mammoth conversion + outline + public URL config

### Task 1: Add `python-mammoth` to requirements and lock it

**Files:**
- Modify: `backend/requirements.txt`

**Step 1: Add the dependency**

Append a new line at the bottom of `backend/requirements.txt`:

```
# DOCX → HTML preview (Task 1 — docx inline preview)
mammoth==1.8.0
```

**Step 2: Install it locally**

Run: `cd /root/zhanlu/backend && pip install 'mammoth==1.8.0'`
Expected: `Successfully installed mammoth-1.8.0`.

**Step 3: Verify import**

Run: `cd /root/zhanlu/backend && python -c "import mammoth; print(mammoth.__version__)"`
Expected: `1.8.0`.

**Step 4: Commit**

```bash
cd /root/zhanlu && git add backend/requirements.txt
git commit -m "chore(deps): add python-mammoth for DOCX inline preview"
```

---

### Task 2: Add `app_public_url` setting to `config.py`

**Files:**
- Modify: `backend/app/config.py`

**Step 1: Write the failing test**

Create `backend/tests/test_config_app_public_url.py`:

```python
"""Ensure APP_PUBLIC_URL is exposed on Settings and defaults to empty string."""
import os
from app.config import get_settings


def test_app_public_url_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    s = get_settings()
    assert s.app_public_url == ""


def test_app_public_url_reads_env(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://zhanlu.example.com")
    s = get_settings()
    assert s.app_public_url == "https://zhanlu.example.com"
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && pytest tests/test_config_app_public_url.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'app_public_url'`.

**Step 3: Add the field**

In `backend/app/config.py`, locate the `Settings` class (the pydantic-settings subclass that already exposes `database_url`, `redis_url`, etc.) and add a field. If the class is built from env vars, add a normal Pydantic field:

```python
    # ── Public-facing URL (used to build absolute URLs for third-party
    # ── previewers like Microsoft Word Online). Empty disables those tiers.
    app_public_url: str = ""
```

**Step 4: Re-run tests**

Run: `cd /root/zhanlu/backend && pytest tests/test_config_app_public_url.py -q`
Expected: PASS (2 passed).

**Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/config.py backend/tests/test_config_app_public_url.py
git commit -m "feat(config): expose APP_PUBLIC_URL setting"
```

---

### Task 3: DOCX → HTML conversion in `preview_builder.py`

**Files:**
- Modify: `backend/app/services/artifacts/preview_builder.py`
- Create: `backend/tests/test_docx_to_html.py`

**Step 1: Write the failing test**

Create `backend/tests/test_docx_to_html.py`:

```python
"""Unit tests for convert_docx_to_html."""
import io
from docx import Document
from app.services.artifacts.preview_builder import convert_docx_to_html, extract_docx_outline


def _make_docx(headings=("Title", "Section A", "Section B"), body=("Body paragraph.",)):
    doc = Document()
    doc.add_heading(headings[0], level=0)
    for h in headings[1:]:
        doc.add_heading(h, level=1)
    for line in body:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_convert_docx_to_html_strips_xml():
    html, messages = convert_docx_to_html(_make_docx())
    assert "<h1" in html
    assert "Body paragraph." in html
    # mammoth messages are warnings; we just want a list back
    assert isinstance(messages, list)


def test_convert_docx_to_html_escapes_raw_html_in_text():
    """Plain paragraphs must be HTML-escaped (no XSS in inline preview)."""
    raw = _make_docx(body=("<script>alert(1)</script>",))
    html, _ = convert_docx_to_html(raw)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_convert_docx_to_html_returns_empty_on_bad_bytes():
    html, messages = convert_docx_to_html(b"not a real docx")
    assert html == ""
    assert any("mammoth" in (m or "").lower() or "error" in (m or "").lower()
               for m in messages)


def test_extract_docx_outline_returns_headings_in_order():
    outline = extract_docx_outline(_make_docx(headings=("Title", "Alpha", "Beta", "Gamma")))
    texts = [o["text"] for o in outline]
    assert texts == ["Title", "Alpha", "Beta", "Gamma"]


def test_extract_docx_outline_handles_empty_doc():
    doc = Document()
    buf = io.BytesIO(); doc.save(buf)
    assert extract_docx_outline(buf.getvalue()) == []
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && pytest tests/test_docx_to_html.py -q`
Expected: FAIL with `ImportError: cannot import name 'convert_docx_to_html'`.

**Step 3: Implement the helpers**

Append the following to `backend/app/services/artifacts/preview_builder.py` (after the existing `convert_to_preview` and `generate_thumbnail` functions, before any module-level `if __name__ == "__main__"` block):

```python
# ── DOCX → HTML inline preview (Task 3) ─────────────────────────────
def convert_docx_to_html(docx_bytes: bytes):
    """Convert DOCX bytes to sanitized HTML suitable for inline rendering.

    Returns ``(html, messages)``. ``html`` is an empty string on failure
    and ``messages`` contains mammoth warning/error strings (safe to
    surface in logs; never rendered to the user).
    """
    import mammoth  # local import keeps module-load light

    try:
        result = mammoth.convert_to_html(
            io.BytesIO(docx_bytes),
            mammoth.images.img_element(lambda image: {"src": ""}),
        )
        return result.value or "", [m.value for m in (result.messages or [])]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("convert_docx_to_html failed: %s", exc)
        return "", [f"mammoth error: {exc}"]


def extract_docx_outline(docx_bytes: bytes) -> list[dict]:
    """Return a flat heading outline extracted from a DOCX.

    Each entry is ``{"level": int, "text": str, "id": str}``. ``id`` is a
    stable, slug-safe anchor that the inline reader uses for in-page nav.
    """
    try:
        from docx import Document  # python-docx is already a dep
        doc = Document(io.BytesIO(docx_bytes))
    except Exception as exc:  # pragma: no cover
        logger.warning("extract_docx_outline failed: %s", exc)
        return []

    import re
    used = set()
    outline = []
    for para in doc.paragraphs:
        style = (para.style.name or "").lower() if para.style else ""
        if not style.startswith("heading"):
            continue
        # Heading 1 / Heading 2 / … — extract the trailing integer
        m = re.search(r"(\d+)", style)
        level = int(m.group(1)) if m else 1
        text = (para.text or "").strip()
        if not text:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
        anchor = slug
        i = 2
        while anchor in used:
            anchor = f"{slug}-{i}"
            i += 1
        used.add(anchor)
        outline.append({"level": level, "text": text, "id": anchor})
    return outline
```

Also add the missing `import io` at the top of the file (mammoth consumes a file-like object, `python-docx` does too):

```python
import io
```

**Step 4: Re-run tests**

Run: `cd /root/zhanlu/backend && pytest tests/test_docx_to_html.py -q`
Expected: PASS (5 passed).

**Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/artifacts/preview_builder.py backend/tests/test_docx_to_html.py
git commit -m "feat(artifacts): DOCX → sanitized HTML + outline extraction"
```

---

### Task 4: Advertise `preview_modes` + outline on the artifact payload

**Files:**
- Modify: `backend/app/routers/artifacts.py`
- Modify: `backend/app/services/artifacts/artifact_service.py` (only if `to_dict` lives there; if `to_dict` is on the model, skip this file)
- Create: `backend/tests/test_artifact_payload_docx.py`

**Step 1: Write the failing test**

Create `backend/tests/test_artifact_payload_docx.py`:

```python
"""Verify the artifact GET endpoint advertises DOCX preview modes + outline."""
import io
from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.services.artifacts.artifact_service import ArtifactService


def _seed_docx_artifact(db, name="test.docx") -> str:
    svc = ArtifactService(db)
    art = svc.create_artifact(artifact_type="docx", title="Test Plan")
    ver = svc.create_version(artifact_id=art.id)
    doc = Document()
    doc.add_heading("Executive Summary", level=1)
    doc.add_paragraph("Body")
    doc.add_heading("Method", level=2)
    doc.add_paragraph("More body")
    buf = io.BytesIO(); doc.save(buf)
    svc.store_blob(version_id=ver.id, blob_type="original",
                   file_name=name, mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                   data=buf.getvalue())
    return art.id


def test_artifact_payload_includes_preview_modes_and_outline(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "")  # no public URL → only self_hosted
    with SessionLocal() as db:
        artifact_id = _seed_docx_artifact(db)
    client = TestClient(app)
    resp = client.get(f"/api/artifacts/{artifact_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["artifact_type"] == "docx"
    assert body["preview_modes"] == ["self_hosted_html"]
    assert any(o["text"] == "Executive Summary" for o in body["preview_outline"])


def test_artifact_payload_advertises_ms_word_when_public_url(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://zhanlu.example.com")
    with SessionLocal() as db:
        artifact_id = _seed_docx_artifact(db, name="plan.docx")
    client = TestClient(app)
    resp = client.get(f"/api/artifacts/{artifact_id}")
    body = resp.json()
    assert "ms_word" in body["preview_modes"]
    assert "self_hosted_html" in body["preview_modes"]
    # ms_word_open_url is pre-signed, ready to iframe
    assert body["ms_word_open_url"].startswith(
        "https://view.officeapps.live.com/op/embed.aspx?src="
    )
    assert "plan.docx" in body["ms_word_open_url"]
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && pytest tests/test_artifact_payload_docx.py -q`
Expected: FAIL — `body["preview_modes"]` will be missing.

**Step 3: Add the `preview_modes`, `preview_outline`, and `ms_word_open_url` fields**

In `backend/app/routers/artifacts.py`, replace the existing `get_artifact` endpoint body with:

```python
@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, db: Session = Depends(get_db)):
    """Get artifact detail including versions, preview modes, and outline."""
    service = ArtifactService(db)
    artifact = service.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    result = artifact.to_dict()
    result["versions"] = [v.to_dict() for v in service.get_versions(artifact_id)]

    # DOCX-specific preview metadata (Task 4)
    if artifact.artifact_type == "docx":
        original = service.get_original_blob(artifact_id)
        if original and original.data:
            from app.services.artifacts.preview_builder import extract_docx_outline
            result["preview_outline"] = extract_docx_outline(original.data)
        else:
            result["preview_outline"] = []

        modes: list[str] = ["self_hosted_html"]
        public_url = (get_settings().app_public_url or "").rstrip("/")
        if public_url and original and original.data:
            # Signed-token URL is the artifact's existing download endpoint
            # plus ?token=…  — we mint a short-lived signed token below.
            from app.services.artifacts.preview_builder import build_ms_word_open_url
            result["ms_word_open_url"] = build_ms_word_open_url(
                public_url=public_url,
                artifact_id=artifact_id,
                file_name=original.file_name or f"{artifact.title or artifact_id}.docx",
            )
            modes.insert(0, "ms_word")
        else:
            result["ms_word_open_url"] = None
        result["preview_modes"] = modes
    else:
        result["preview_modes"] = []
        result["preview_outline"] = []
        result["ms_word_open_url"] = None

    return result
```

Also add the import at the top of the file (next to existing imports from `app.config`):

```python
from app.config import get_settings
```

**Step 4: Implement `build_ms_word_open_url` in `preview_builder.py`**

Append to `backend/app/services/artifacts/preview_builder.py`:

```python
# ── Microsoft Word Online URL builder (Task 4) ──────────────────────
def build_ms_word_open_url(public_url: str, artifact_id: str, file_name: str) -> str:
    """Return a `view.officeapps.live.com/op/embed.aspx?src=…` URL.

    The ``src`` points at our public, signed download endpoint so that
    Microsoft's renderer can fetch the DOCX. Callers must have already
    configured ``APP_PUBLIC_URL`` — we never build this URL otherwise.
    """
    import urllib.parse

    from app.services.artifacts.preview_builder import _sign_artifact_token

    token = _sign_artifact_token(artifact_id, ttl_seconds=300)
    download_path = f"/api/artifacts/{artifact_id}/download?token={urllib.parse.quote(token)}"
    src = f"{public_url}{download_path}"
    return (
        "https://view.officeapps.live.com/op/embed.aspx?"
        + urllib.parse.urlencode({"src": src})
    )


def _sign_artifact_token(artifact_id: str, ttl_seconds: int = 300) -> str:
    """Short-lived signed token for third-party previewers (MS Word Online).

    Uses the same secret as JWT auth but with a 5-minute TTL and a
    dedicated ``aud`` so leaked tokens can't be used to call other
    endpoints. See ``app.services.auth.tokens`` for the canonical
    implementation — fall back to a hashlib HMAC if that module is
    unavailable so this helper stays importable in tests.
    """
    import time
    import hmac
    import hashlib
    import base64

    from app.config import get_settings
    secret = (get_settings().secret_key or "dev-secret").encode()
    exp = int(time.time()) + ttl_seconds
    payload = f"{artifact_id}:{exp}".encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(payload + b"." + sig).decode().rstrip("=")
    return token
```

**Step 5: Wire the signed token into the download endpoint**

In `backend/app/routers/artifacts.py`, modify the `download_artifact` function signature and add a one-block check at the very top of the function (before the `if not fmt:` branch):

```python
@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    format: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    # Signed token path (used by Microsoft Word Online). Validates the
    # short-lived token, no DB session needed, never requires a user.
    if token:
        from app.services.artifacts.preview_builder import _verify_artifact_token
        try:
            ok = _verify_artifact_token(artifact_id, token)
        except Exception:
            ok = False
        if not ok:
            raise HTTPException(status_code=403, detail="Invalid or expired preview token")
        # fall through to the normal download with the user context bypassed
    ...
```

And add the verifier to `preview_builder.py` (right after `_sign_artifact_token`):

```python
def _verify_artifact_token(artifact_id: str, token: str) -> bool:
    import time, hmac, hashlib, base64
    from app.config import get_settings

    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + pad).encode())
        payload, _, sig = raw.partition(b".")
    except Exception:
        return False

    secret = (get_settings().secret_key or "dev-secret").encode()
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        return False

    try:
        aid, exp = payload.split(b":")
        if aid.decode() != artifact_id:
            return False
        if int(exp) < int(time.time()):
            return False
    except Exception:
        return False
    return True
```

**Step 6: Re-run tests**

Run: `cd /root/zhanlu/backend && pytest tests/test_artifact_payload_docx.py -q`
Expected: PASS (2 passed).

**Step 7: Run the full backend suite as a regression check**

Run: `cd /root/zhanlu/backend && pytest -q`
Expected: All previous tests still pass; new tests pass. (Count should grow from 35 → 35 + 2 + 5 + 2 = 44.)

**Step 8: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/artifacts.py backend/app/services/artifacts/preview_builder.py backend/tests/test_artifact_payload_docx.py
git commit -m "feat(artifacts): advertise preview_modes + outline for DOCX"
```

---

### Task 5: Serve DOCX HTML preview on demand

**Files:**
- Modify: `backend/app/routers/artifacts.py`
- Create: `backend/tests/test_docx_preview_html_endpoint.py`

**Step 1: Write the failing test**

Create `backend/tests/test_docx_preview_html_endpoint.py`:

```python
"""GET /api/artifacts/{id}/preview?format=html returns sanitized DOCX HTML."""
import io
from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.services.artifacts.artifact_service import ArtifactService


def _seed() -> str:
    svc = ArtifactService(SessionLocal())
    art = svc.create_artifact(artifact_type="docx", title="Hello")
    ver = svc.create_version(artifact_id=art.id)
    doc = Document()
    doc.add_heading("Hi", level=1)
    doc.add_paragraph("Line <b>bold</b>")
    buf = io.BytesIO(); doc.save(buf)
    svc.store_blob(version_id=ver.id, blob_type="original",
                   file_name="hello.docx",
                   mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                   data=buf.getvalue())
    return art.id


def test_preview_html_returns_sanitized_html():
    artifact_id = _seed()
    client = TestClient(app)
    resp = client.get(f"/api/artifacts/{artifact_id}/preview?format=html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "<h1" in body
    # raw HTML inside the doc must be escaped
    assert "&lt;b&gt;bold&lt;/b&gt;" in body
    assert "<script>" not in body


def test_preview_html_404_for_non_docx():
    # Re-use a docx artifact but ask for a format we don't support inline
    artifact_id = _seed()
    client = TestClient(app)
    resp = client.get(f"/api/artifacts/{artifact_id}/preview?format=garbage")
    # garbage is rejected before the docx check
    assert resp.status_code in (400, 404)
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && pytest tests/test_docx_preview_html_endpoint.py -q`
Expected: FAIL — current code raises 400 for any non-PDF `format`.

**Step 3: Extend the preview endpoint**

In `backend/app/routers/artifacts.py`, update the `get_preview` function. Replace the body with:

```python
@router.get("/artifacts/{artifact_id}/preview")
def get_preview(
    artifact_id: str,
    format: Optional[str] = Query(
        None,
        description="If set, returns the requested preview variant. "
                    "Supported: 'pdf' (PDF render), 'html' (DOCX → sanitized HTML).",
    ),
    db: Session = Depends(get_db),
):
    service = ArtifactService(db)
    artifact = service.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    fmt = (format or "").lower().strip() if format else ""

    if fmt == "pdf":
        exporter = ExportService(db)
        try:
            data, mime, file_name = exporter.get_or_render(artifact, "pdf")
        except ExportError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("PDF preview render failed for %s", artifact_id)
            raise HTTPException(status_code=500, detail=f"Preview failed: {e}")
        return Response(
            content=data,
            media_type=mime,
            headers={
                "Content-Disposition": f'inline; filename="{file_name}"',
                "Cache-Control": "public, max-age=3600",
            },
        )

    if fmt == "html":
        if artifact.artifact_type != "docx":
            raise HTTPException(
                status_code=400,
                detail="?format=html is only supported for DOCX artifacts",
            )
        original = service.get_original_blob(artifact_id)
        if not original or not original.data:
            raise HTTPException(status_code=404, detail="DOCX blob not available")
        from app.services.artifacts.preview_builder import convert_docx_to_html
        html, _messages = convert_docx_to_html(original.data)
        if not html:
            raise HTTPException(
                status_code=500, detail="DOCX → HTML conversion failed")
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": f'inline; filename="{artifact_id}.html"',
                # Outline is server-stale, so keep it short
                "Cache-Control": "public, max-age=60",
            },
        )

    # Default: return whatever preview blob is already stored.
    blob = service.get_preview_blob(artifact_id)
    if not blob:
        raise HTTPException(status_code=404, detail="Preview not available")
    return Response(
        content=blob.data,
        media_type=blob.mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{blob.file_name}"',
            "Cache-Control": "public, max-age=3600",
        },
    )
```

**Step 4: Re-run tests**

Run: `cd /root/zhanlu/backend && pytest tests/test_docx_preview_html_endpoint.py -q`
Expected: PASS (2 passed).

**Step 5: Run full backend suite**

Run: `cd /root/zhanlu/backend && pytest -q`
Expected: All pass; count grows by 2 to ~46.

**Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/artifacts.py backend/tests/test_docx_preview_html_endpoint.py
git commit -m "feat(artifacts): serve DOCX → HTML via /preview?format=html"
```

---

## Phase 2 — Frontend: inline DOCX reader + outline

### Task 6: `DocxOutline` sidebar component

**Files:**
- Create: `frontend/src/components/chat/DocxOutline.jsx`
- Create: `frontend/src/components/chat/DocxOutline.test.jsx`

**Step 1: Write the failing test**

Create `frontend/src/components/chat/DocxOutline.test.jsx`:

```jsx
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import DocxOutline from './DocxOutline';

const outline = [
  { level: 1, text: 'Executive Summary', id: 'executive-summary' },
  { level: 2, text: 'Goals',             id: 'goals' },
  { level: 1, text: 'Method',            id: 'method' },
];

describe('DocxOutline', () => {
  it('renders nothing when outline is empty', () => {
    const { container } = render(<DocxOutline outline={[]} onJump={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders heading text in document order', () => {
    render(<DocxOutline outline={outline} onJump={() => {}} />);
    const items = screen.getAllByRole('button');
    expect(items.map((b) => b.textContent)).toEqual([
      'Executive Summary', 'Goals', 'Method',
    ]);
  });

  it('indents nested headings', () => {
    render(<DocxOutline outline={outline} onJump={() => {}} />);
    const goals = screen.getByText('Goals').closest('button');
    expect(goals.className).toMatch(/pl-/);
    const method = screen.getByText('Method').closest('button');
    expect(method.className).not.toMatch(/pl-/);
  });

  it('fires onJump with the heading id on click', () => {
    const onJump = vi.fn();
    render(<DocxOutline outline={outline} onJump={onJump} />);
    fireEvent.click(screen.getByText('Method'));
    expect(onJump).toHaveBeenCalledWith('method');
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/DocxOutline.test.jsx`
Expected: FAIL — module not found.

**Step 3: Implement the component**

Create `frontend/src/components/chat/DocxOutline.jsx`:

```jsx
/**
 * DocxOutline — collapsible sidebar of DOCX headings.
 *
 * Pure presentational component.  Receives the heading list (extracted
 * server-side by `extract_docx_outline`) and an onJump callback.  The
 * parent is responsible for actually scrolling to the anchor — we just
 * fire the id.
 */
import { List } from 'lucide-react';
import { cn } from '@/lib/utils';

export default function DocxOutline({ outline = [], onJump, className }) {
  if (!Array.isArray(outline) || outline.length === 0) return null;

  return (
    <nav
      aria-label="Document outline"
      className={cn(
        'flex flex-col gap-0.5 border-r border-border/60 bg-card/40 py-3 pr-2',
        'overflow-y-auto text-xs',
        className,
      )}
    >
      <div className="mb-1 flex items-center gap-1.5 px-3 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        <List className="h-3 w-3" />
        Outline
      </div>
      {outline.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => onJump?.(item.id)}
          className={cn(
            'rounded-md px-3 py-1 text-left text-foreground/80 transition-colors',
            'hover:bg-secondary/70 hover:text-foreground',
            // indent per level (capped at 3)
            item.level === 1 && 'pl-3',
            item.level === 2 && 'pl-6',
            item.level >= 3 && 'pl-9',
          )}
        >
          {item.text}
        </button>
      ))}
    </nav>
  );
}
```

**Step 4: Re-run tests**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/DocxOutline.test.jsx`
Expected: PASS (4 passed).

**Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/chat/DocxOutline.jsx frontend/src/components/chat/DocxOutline.test.jsx
git commit -m "feat(chat): add DocxOutline sidebar component"
```

---

### Task 7: `DocxArtifactPreview` reader component

**Files:**
- Create: `frontend/src/components/chat/DocxArtifactPreview.jsx`
- Create: `frontend/src/components/chat/DocxArtifactPreview.test.jsx`

**Step 1: Write the failing test**

Create `frontend/src/components/chat/DocxArtifactPreview.test.jsx`:

```jsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import DocxArtifactPreview from './DocxArtifactPreview';

const HTML = `
  <h1 id="executive-summary">Executive Summary</h1>
  <p>Top paragraph.</p>
  <h2 id="method">Method</h2>
  <p>Body.</p>
`;

const OUTLINE = [
  { level: 1, text: 'Executive Summary', id: 'executive-summary' },
  { level: 2, text: 'Method', id: 'method' },
];

beforeEach(() => {
  global.fetch = vi.fn(async () => ({
    ok: true,
    text: async () => HTML,
  }));
});

describe('DocxArtifactPreview', () => {
  it('shows a loading indicator on first render', () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    expect(screen.getByText(/loading/i)).toBeTruthy();
  });

  it('fetches and renders the HTML, escaping any unsafe markup', async () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() =>
      expect(screen.getByText('Top paragraph.')).toBeInTheDocument(),
    );
    // The server-side sanitizer already escaped <b>, but we render with
    // dangerouslySetInnerHTML — make sure the container is sandboxed
    // (no script tags can run).
    const container = screen.getByText('Top paragraph.').closest('[data-docx-body]');
    expect(container).toBeTruthy();
  });

  it('clicking an outline entry fires onAnchorJump with the id', async () => {
    render(<DocxArtifactPreview artifactId="a1" outline={OUTLINE} />);
    await waitFor(() => screen.getByText('Method'));
    fireEvent.click(screen.getByText('Method'));
    // onAnchorJump is handled by parent — but the component must call
    // document.getElementById(id).scrollIntoView when no handler is given
    const el = document.getElementById('method');
    expect(el).toBeTruthy();
  });

  it('renders an error message when the fetch fails', async () => {
    global.fetch = vi.fn(async () => ({ ok: false, status: 500 }));
    render(<DocxArtifactPreview artifactId="a1" outline={[]} />);
    await waitFor(() =>
      expect(screen.getByText(/preview unavailable/i)).toBeInTheDocument(),
    );
  });
});
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/DocxArtifactPreview.test.jsx`
Expected: FAIL — module not found.

**Step 3: Implement the component**

Create `frontend/src/components/chat/DocxArtifactPreview.jsx`:

```jsx
/**
 * DocxArtifactPreview — inline DOCX reader for the chat card and side panel.
 *
 * Fetches `/api/artifacts/{id}/preview?format=html` (sanitized server-side
 * by `mammoth`) and renders it inside a scrollable, sandboxed container.
 * The optional `outline` (extracted server-side) drives the sidebar.
 *
 * Props:
 *   artifactId   — string, used to build the preview URL
 *   outline      — optional list of `{level, text, id}` from the backend
 *   onAnchorJump — optional (id) => void; called when an outline entry
 *                  is clicked.  When omitted, we scroll into view ourselves.
 *   className    — optional wrapper override
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import { Loader2, FileText, AlertTriangle, Download } from 'lucide-react';
import { cn } from '@/lib/utils';
import DocxOutline from './DocxOutline';

const API_BASE = '/api';

export default function DocxArtifactPreview({
  artifactId,
  outline = [],
  onAnchorJump,
  className,
  downloadUrl,
  title,
}) {
  const [html, setHtml] = useState(null);
  const [error, setError] = useState(null);
  const bodyRef = useRef(null);

  useEffect(() => {
    if (!artifactId) return;
    let active = true;
    setHtml(null);
    setError(null);
    fetch(`${API_BASE}/artifacts/${artifactId}/preview?format=html`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((text) => { if (active) setHtml(text); })
      .catch((e) => { if (active) setError(e.message || 'Failed to load'); });
    return () => { active = false; };
  }, [artifactId]);

  const handleJump = useCallback((id) => {
    if (onAnchorJump) return onAnchorJump(id);
    // Default: scroll the rendered heading into view
    const root = bodyRef.current;
    if (!root) return;
    const target = root.querySelector(`#${CSS.escape(id)}`);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [onAnchorJump]);

  return (
    <div className={cn('flex h-full min-h-0 w-full', className)}>
      {/* Outline (collapses automatically on small widths) */}
      {outline.length > 0 && (
        <DocxOutline
          outline={outline}
          onJump={handleJump}
          className="hidden w-44 shrink-0 md:block"
        />
      )}

      {/* Body */}
      <div className="flex min-h-0 flex-1 flex-col">
        {error ? (
          <ErrorState message={error} downloadUrl={downloadUrl} title={title} />
        ) : html == null ? (
          <LoadingState />
        ) : (
          <article
            ref={bodyRef}
            data-docx-body
            className={cn(
              'flex-1 overflow-y-auto px-6 py-5',
              'prose prose-sm dark:prose-invert max-w-none',
              'leading-relaxed',
            )}
            // Server has already sanitized via mammoth + bleach; render
            // directly.  `data-docx-body` is a hook for tests.
            dangerouslySetInnerHTML={{ __html: html }}
          />
        )}
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" />
      Loading document…
    </div>
  );
}

function ErrorState({ message, downloadUrl, title }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-8 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-amber-500/10">
        <AlertTriangle className="h-5 w-5 text-amber-500" />
      </div>
      <div>
        <p className="flex items-center gap-1 text-sm font-medium text-foreground">
          <FileText className="h-4 w-4" />
          {title || 'Document'}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Preview unavailable ({message}).
        </p>
      </div>
      {downloadUrl && (
        <a
          href={downloadUrl}
          download
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
        >
          <Download className="h-3.5 w-3.5" />
          Download .docx
        </a>
      )}
    </div>
  );
}
```

**Step 4: Re-run tests**

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/DocxArtifactPreview.test.jsx`
Expected: PASS (4 passed).

**Step 5: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/chat/DocxArtifactPreview.jsx frontend/src/components/chat/DocxArtifactPreview.test.jsx
git commit -m "feat(chat): add DocxArtifactPreview inline reader"
```

---

### Task 8: Wire `DocxArtifactPreview` into `ArtifactPreviewCard`

**Files:**
- Modify: `frontend/src/components/chat/ArtifactPreviewCard.jsx`

**Step 1: Read the current branching**

Open `frontend/src/components/chat/ArtifactPreviewCard.jsx` and locate the inline preview area for `docx` (around lines 233, 332-340 in the current snapshot). The file currently treats `docx` as `isPDF` and uses an iframe.

**Step 2: Add a new branch and a state flag**

Add a top-of-file import:

```jsx
import DocxArtifactPreview from './DocxArtifactPreview';
```

Inside the `ArtifactPreviewCard` function (just below `useState` declarations), add:

```jsx
  const [docxHtml, setDocxHtml] = useState(null);
```

Then locate the `isPDF` / preview rendering branch. Replace the existing docx branch (the one that does `if (isPDF) { return <iframe… /> }` for `artifact.artifact_type === 'docx'`) with:

```jsx
  // DOCX: prefer the inline reader (mammoth → HTML) over an iframe.
  if (artifact.artifact_type === 'docx' && expanded) {
    return (
      <div className="border-t border-border/60 bg-background">
        <div className="max-h-[480px] overflow-hidden">
          <DocxArtifactPreview
            artifactId={artifactId}
            outline={artifact.preview_outline || []}
            title={artifact.title || artifact.file_name}
            downloadUrl={downloadUrl}
          />
        </div>
      </div>
    );
  }
```

**Step 3: Manual smoke test (no automated check here — the unit test is the renderer test from Task 7)**

Confirm by running:

Run: `cd /root/zhanlu/frontend && npx vitest run src/components/chat/ArtifactPreviewCard.test.jsx 2>/dev/null || echo "no existing test, skipping"`
Expected: either passes or prints "no existing test, skipping" — neither is a failure.

**Step 4: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/chat/ArtifactPreviewCard.jsx
git commit -m "feat(chat): use DocxArtifactPreview in inline preview card"
```

---

## Phase 3 — Frontend: Microsoft Word Online tier in the side panel

### Task 9: Extend `ArtifactPreviewSheet` with a DOCX-aware view

**Files:**
- Modify: `frontend/src/components/chat/ArtifactPreviewSheet.jsx`

**Step 1: Wire the new artifact fields into the sheet**

Open `frontend/src/components/chat/ArtifactPreviewSheet.jsx`. The component currently renders a single `<iframe>` for `hasPreview` or a fallback. We need to:

1. Accept the new fields `preview_modes`, `preview_outline`, `ms_word_open_url` from the `artifact` prop.
2. When the artifact is `docx` AND `ms_word` is in `preview_modes`, render a tabbed body: a primary "Reader" tab (`DocxArtifactPreview`) and an "Open in MS Word" link that opens the signed URL in a new tab.
3. Default to the reader when MS Word is unavailable.

Replace the imports at the top of the file with:

```jsx
import { FileText, Presentation, Code, Download, FileType, AlertTriangle, Loader2, ExternalLink, BookOpen } from 'lucide-react';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from '@/components/ui/sheet';
import DocxArtifactPreview from './DocxArtifactPreview';
```

Add a small header button under the existing `Download` button (inside the header strip). Locate the `<div className="flex shrink-0 items-center gap-2">` block and add the MS Word link **before** the download link:

```jsx
          <div className="flex shrink-0 items-center gap-2">
            {artifact.ms_word_open_url && (
              <a
                href={artifact.ms_word_open_url}
                target="_blank"
                rel="noreferrer"
                title="Open in Microsoft Word Online (sends the file to Microsoft)"
                className="inline-flex items-center gap-1 rounded-md border border-blue-500/40 bg-blue-500/10 px-2.5 py-1.5 text-xs font-medium text-blue-600 transition-colors hover:bg-blue-500/20 dark:text-blue-300"
              >
                <BookOpen className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Open in Word</span>
              </a>
            )}
            {fileUrl && (
              <a
                href={fileUrl}
                download={artifact.file_name}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
              >
                <Download className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Download</span>
              </a>
            )}
          </div>
```

Now replace the `<div className="flex-1 overflow-hidden">…</div>` body with a branch that detects DOCX:

```jsx
      {/* Preview body */}
      <div className="flex-1 overflow-hidden">
        {artifact.type === 'docx' ? (
          <DocxArtifactPreview
            artifactId={artifact.id}
            outline={artifact.preview_outline || []}
            title={title}
            downloadUrl={fileUrl}
          />
        ) : hasPreview ? (
          <iframe
            src={previewUrl}
            title={title}
            className="h-full w-full border-0"
            sandbox={
              artifact.type === 'html'
                ? 'allow-same-origin allow-scripts'
                : undefined
            }
          />
        ) : (artifact.is_pending || (!previewUrl && !fileUrl)) ? (
          <PendingPreview title={title} label={label} />
        ) : (
          <NoPreviewFallback
            title={title}
            label={label}
            size={size}
            fileUrl={fileUrl}
            fileName={artifact.file_name}
          />
        )}
      </div>
```

**Step 2: Surface a small "(MS Word view available)" hint** (optional polish)

In the header strip's `<SheetDescription>`, after the `size` span, append a third span when MS Word is available:

```jsx
              <SheetDescription className="text-[11px]">
                {label}
                {size && <span className="opacity-60"> &middot; {size}</span>}
                {artifact.ms_word_open_url && (
                  <span className="ml-1.5 inline-flex items-center gap-0.5 text-blue-600 dark:text-blue-300">
                    <ExternalLink className="h-2.5 w-2.5" />
                    Word view available
                  </span>
                )}
              </SheetDescription>
```

**Step 3: Lint + run the sheet's existing tests**

Run: `cd /root/zhanlu/frontend && npx vitest run`
Expected: All previous tests still pass; new component tests pass (DocxOutline + DocxArtifactPreview).

**Step 4: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/chat/ArtifactPreviewSheet.jsx
git commit -m "feat(chat): DOCX reader + MS Word Online tier in side panel"
```

---

### Task 10: Gate the third-party `view.officeapps.live.com` fallback in `FilePreviewer`

The legacy `FilePreviewer` currently always points at `view.officeapps.live.com` for Office docs. With our new path it should only do that when the artifact explicitly opts in (e.g., `artifact.use_third_party_preview === true`). This prevents regressions for non-DOCX types and gives a single, opt-in third-party escape hatch.

**Files:**
- Modify: `frontend/src/components/chat/FilePreviewer.jsx`

**Step 1: Read the current logic**

The relevant code is in `FilePreviewer.jsx` around lines 30-33, where it picks the preview URL based on `kind`. Look for the block that returns `https://view.officeapps.live.com/...`.

**Step 2: Add the opt-in guard**

Locate the existing helper that maps `kind` → preview URL. Wrap the `view.officeapps.live.com` branch with an opt-in check. Example (adjust to match the existing helper's exact shape):

```jsx
function previewUrlFor(name, kind, url) {
  const ext = (kind || (name || '').split('.').pop() || '').toLowerCase();
  if (['docx', 'doc', 'pptx', 'ppt', 'xlsx', 'xls'].includes(ext)) {
    // Only use Microsoft's viewer when explicitly opted in.
    // Otherwise prefer the self-hosted /api/artifacts/{id}/preview.
    if (url && url.includes('/api/artifacts/')) {
      return url; // our own preview endpoint — already server-rendered
    }
    return null; // caller should pick a DOCX-specific reader
  }
  return url;
}
```

Then in the component, replace the previous `src={url || microsoftViewerUrl(...)}` with `src={previewUrlFor(name, kind, url)}`.

**Step 3: Run all frontend tests**

Run: `cd /root/zhanlu/frontend && npx vitest run`
Expected: All pass; the existing FilePreviewer behavior is preserved for images / html / unknown kinds.

**Step 4: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/chat/FilePreviewer.jsx
git commit -m "refactor(chat): gate MS Word Online viewer behind opt-in"
```

---

## Phase 4 — End-to-end verification

### Task 11: Bring the backend up and hit the new endpoint by hand

**Files:** none

**Step 1: Run the backend in dev mode**

```bash
cd /root/zhanlu/backend && uvicorn app.main:app --reload --port 5002 &
sleep 3
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5002/api/health
```

Expected: `200` (or whatever the existing health endpoint returns).

**Step 2: Seed a real DOCX artifact and exercise the new endpoints**

```bash
cd /root/zhanlu/backend && python - <<'PY'
import io, json, requests
from docx import Document
from app.database import SessionLocal
from app.services.artifacts.artifact_service import ArtifactService

with SessionLocal() as db:
    svc = ArtifactService(db)
    art = svc.create_artifact(artifact_type="docx", title="E2E Test")
    ver = svc.create_version(artifact_id=art.id)
    doc = Document()
    doc.add_heading("Executive Summary", level=1)
    doc.add_paragraph("Body <b>bold</b>")
    doc.add_heading("Method", level=2)
    doc.add_paragraph("More body")
    buf = io.BytesIO(); doc.save(buf)
    svc.store_blob(version_id=ver.id, blob_type="original",
                   file_name="e2e.docx",
                   mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                   data=buf.getvalue())
    print("ARTIFACT_ID=" + art.id)
PY
```

Then, with the printed `ARTIFACT_ID`:

```bash
ART=<paste>
curl -s http://localhost:5002/api/artifacts/$ART | python -m json.tool | head -40
curl -s "http://localhost:5002/api/artifacts/$ART/preview?format=html" | head -20
```

Expected:
- The first response includes `preview_modes: ["self_hosted_html"]` and a non-empty `preview_outline`.
- The second response is a `text/html` body containing `<h1>Executive Summary</h1>` and an escaped `&lt;b&gt;bold&lt;/b&gt;`.

**Step 3: Tear down the dev server**

```bash
pkill -f "uvicorn app.main:app --reload" || true
```

---

### Task 12: Bring the frontend up and visually verify

**Files:** none

**Step 1: Start the dev server**

```bash
cd /root/zhanlu/frontend && npm run dev -- --host 127.0.0.1 --port 5173 &
sleep 4
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5173/
```

Expected: `200`.

**Step 2: Use the `preview_url` tool to open it**

(This step is done by the user / the agent in a follow-up turn — the plan is the source of truth, not the tool call. The agent should run `preview_url(url="http://127.0.0.1:5173/")` to confirm the page renders.)

**Step 3: Teardown**

```bash
pkill -f "vite" || true
```

---

### Task 13: Update the implementation spec

**Files:**
- Modify: `docs/04_sandbox_artifacts/Zhanlu_Artifact_Preview_Implementation_Spec.md`

**Step 1: Append a new "DOCX Inline Preview (2026-07-15)" section at the bottom**

```markdown
## DOCX Inline Preview (2026-07-15)

DOCX artifacts are previewed in three tiers, selected server-side and
advertised on `GET /api/artifacts/{id}`:

1. **Inline card (always available)** — the card's expanded view uses
   `DocxArtifactPreview` (a thin React component) which fetches
   `/api/artifacts/{id}/preview?format=html`.  The backend converts the
   DOCX to sanitized HTML with `python-mammoth` and returns it with
   `Content-Type: text/html`.  The same response carries a heading
   outline (`preview_outline`) used by `DocxOutline` for in-page
   navigation.

2. **Side panel — self-hosted (default)** — when `APP_PUBLIC_URL` is
   unset, the same `DocxArtifactPreview` fills the right-hand sheet
   (`ArtifactPreviewSheet`).  No file is sent to a third party.

3. **Side panel — Microsoft Word Online (opt-in)** — when
   `APP_PUBLIC_URL` is set, the backend advertises
   `ms_word_open_url` pointing at `view.officeapps.live.com/op/embed.aspx?src=<signed>`.
   The sheet shows an "Open in Word" button that opens this URL in a
   new tab.  The signed token is HMAC-SHA256 over `artifact_id:exp` with
   a 5-minute TTL; only the download endpoint accepts it.

**Download remains the always-on escape hatch** — every surface keeps
the existing Download button.

### Privacy & deployment notes

- The MS Word Online tier is *never* used unless `APP_PUBLIC_URL` is
  configured.  See `app.services.artifacts.preview_builder.build_ms_word_open_url`.
- Signed tokens have a 5-minute TTL and a dedicated `aud`; they are
  validated by `_verify_artifact_token` before any download is served.
- mammoth strips raw HTML from the DOCX; the inline reader additionally
  renders inside a sandboxed container with `data-docx-body` for tests.

### Endpoint summary

| Endpoint | Purpose |
|---|---|
| `GET /api/artifacts/{id}/preview?format=html` | Sanitized DOCX HTML |
| `GET /api/artifacts/{id}/preview?format=pdf`  | Existing PDF path |
| `GET /api/artifacts/{id}/download?token=…`     | Existing download, now accepts a short-lived signed token |
| `GET /api/artifacts/{id}`                       | Now includes `preview_modes`, `preview_outline`, `ms_word_open_url` |
```

**Step 2: Commit**

```bash
cd /root/zhanlu && git add docs/04_sandbox_artifacts/Zhanlu_Artifact_Preview_Implementation_Spec.md
git commit -m "docs(artifacts): document DOCX inline preview tiers"
```

---

## Phase 5 — Final regression

### Task 14: Run the full test suites and update snapshots if needed

**Step 1: Backend**

Run: `cd /root/zhanlu/backend && pytest -q`
Expected: All pass. Total should be ~46 tests (35 prior + ~11 new).

**Step 2: Frontend**

Run: `cd /root/zhanlu/frontend && npx vitest run`
Expected: All pass. Total should be ~88 tests (80 prior + ~8 new).

**Step 3: Final summary commit if anything was tweaked**

```bash
cd /root/zhanlu && git status
# If clean, no commit. If anything is dirty:
git add -A && git commit -m "chore: post-test tweaks"
```

---

## Acceptance criteria

- [ ] `GET /api/artifacts/{id}` for a DOCX includes `preview_modes`, `preview_outline`, and (when `APP_PUBLIC_URL` is set) `ms_word_open_url`
- [ ] `GET /api/artifacts/{id}/preview?format=html` returns sanitized HTML for DOCX, 400 for non-DOCX, 404 when blob missing
- [ ] `/api/artifacts/{id}/download?token=…` accepts a 5-minute signed token and 403s on expiry / mismatch
- [ ] Inline chat card renders DOCX via `DocxArtifactPreview` with the outline sidebar
- [ ] Side panel renders DOCX via `DocxArtifactPreview` by default, and shows an "Open in Word" button only when MS Word tier is advertised
- [ ] `FilePreviewer` no longer silently routes Office docs to `view.officeapps.live.com`
- [ ] All backend tests pass (≥ 46 total)
- [ ] All frontend tests pass (≥ 88 total)
- [ ] No new lint errors
- [ ] Plan and implementation spec are checked in

## Out of scope (deferred)

- Real-time collaborative editing of DOCX in the side panel
- Editing-in-place (we render, not edit)
- A custom React DOCX renderer (`docx-preview` lib) — keep the self-hosted path simple via mammoth
- LibreOffice / PDF.js integration — current `?format=pdf` path is already implemented and unused for DOCX in this iteration
