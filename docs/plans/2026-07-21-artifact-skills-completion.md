# Artifact Skills Completion — DOCX + PPTX Inline Preview & Marker Runtime

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the remaining gaps in the Zhanlu artifact-skill stack: (1) verify/complete the DOCX inline-preview track, (2) add a parallel PPTX inline-preview track, (3) wire the marker-parser so `◤MD_DOCX◤` / `◤HTML_DOCX◤` / `◤PPTX◤` actually trigger `create_artifact`, (4) tighten the default-skills prompt block to mention `run_sandbox_skill` for file generation, and (5) adopt two specific MiniMax ideas (slide-type conventions in `pptx` skill, XSD validation invocation in `docx` skill).

**Architecture:** The existing artifact pipeline (`artifact_tool.create_artifact` → `ArtifactService` → `store_blob` → `convert_to_preview`) stays the single source of truth. We add: (a) a new message-stream interceptor that scans assistant text for the marker contract and routes to `create_artifact`, (b) a PPTX→HTML preview helper (LibreOffice first, `python-pptx` fallback), (c) a small patch to `default_skills.py` + `agent_prompts.py` to mention `run_sandbox_skill`, and (d) two surgical skill-body upgrades.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, python-docx, python-pptx, mammoth (already pinned), LibreOffice headless, Pydantic v2, pytest; React 18 + Vite + Tailwind + Radix UI + Vitest.

---

## Status snapshot (what's already done, verified 2026-07-21)

- [x] `mammoth==1.8.0` in `backend/requirements.txt`
- [x] `app_public_url` in `backend/app/config.py` (test `test_config_app_public_url.py` exists)
- [x] `convert_docx_to_html` + `extract_docx_outline` in `backend/app/services/artifacts/preview_builder.py` (test `test_docx_to_html.py` exists)
- [x] `preview_modes` + `preview_outline` + `ms_word_open_url` advertised on `GET /api/artifacts/{id}` for DOCX (test `test_artifact_payload_docx.py` exists)
- [x] `GET /api/artifacts/{id}/preview?format=html` DOCX endpoint (test `test_docx_preview_html_endpoint.py` exists)
- [x] `DocxOutline.jsx` + `DocxOutline.test.jsx` in frontend
- [x] `DocxArtifactPreview.jsx` + `DocxArtifactPreview.test.jsx` in frontend
- [x] `ArtifactPreviewCard.jsx` imports `DocxArtifactPreview` and uses it for DOCX
- [x] `ArtifactPreviewSheet.jsx` DOCX-aware (per plan; verify visually)
- [x] `run_sandbox_skill` tool exists (`backend/app/services/tool_handlers/sandbox_tool.py`)

## Real gaps this plan closes

1. **Marker contract is documented but not parsed** — `◤MD_DOCX◤`, `◤HTML_DOCX◤`, `◤PPTX◤` appear in skill bodies but the backend has no interceptor. The LLM can emit them and nothing happens.
2. **PPTX has no inline preview** — `convert_to_preview` only does PPTX→PDF via LibreOffice; no PPTX→HTML helper exists, no React reader.
3. **Default-skills prompt block doesn't mention `run_sandbox_skill`** — agents are told to use `skill_view` but not the sandbox execution tool for file generation.
4. **`pptx` skill body lacks slide-type conventions** (MiniMax `pptx-generator` has explicit cover/section/TOC/content/summary patterns).
5. **`docx` skill body doesn't invoke the XSD validators** it ships with (`scripts/office/validators/` + 39 `.xsd` files).
6. **`◤PPTX◤` marker contract doesn't exist** in the pptx skill body — needed for symmetric marker-driven generation.

---

## Phase 1 — Marker runtime (the missing interceptor)

### Task 1: Add a marker parser + integration test

**Files:**
- Create: `backend/app/services/artifact_markers.py`
- Create: `backend/tests/test_artifact_markers.py`

**Step 1: Write the failing test**

Create `backend/tests/test_artifact_markers.py`:

```python
"""Marker parser: extract ◤FMT◤{...}◤END_FMT◤ blocks from assistant text."""
import json
from app.services.artifact_markers import (
    Marker,
    find_markers,
    strip_markers,
    MARKER_PATTERN,
)


def test_find_md_docx_marker():
    text = (
        "Here is your report.\n\n"
        "◤MD_DOCX◤{\"md_path\": \"outputs/report.md\", \"filename\": \"Report.docx\"}◤END_MD_DOCX◤\n"
    )
    markers = find_markers(text)
    assert len(markers) == 1
    assert markers[0].kind == "MD_DOCX"
    assert markers[0].payload["filename"] == "Report.docx"


def test_find_html_docx_marker():
    text = '◤HTML_DOCX◤{"html_path": "outputs/r.html", "filename": "R.docx"}◤END_HTML_DOCX◤'
    markers = find_markers(text)
    assert len(markers) == 1
    assert markers[0].kind == "HTML_DOCX"
    assert markers[0].payload["html_path"] == "outputs/r.html"


def test_find_pptx_marker():
    text = '◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Deck.pptx"}◤END_PPTX◤'
    markers = find_markers(text)
    assert len(markers) == 1
    assert markers[0].kind == "PPTX"


def test_find_multiple_markers():
    text = (
        "Intro\n"
        "◤MD_DOCX◤{\"md_path\":\"a.md\",\"filename\":\"A.docx\"}◤END_MD_DOCX◤\n"
        "Middle\n"
        "◤PPTX◤{\"slides_path\":\"b.json\",\"filename\":\"B.pptx\"}◤END_PPTX◤\n"
    )
    markers = find_markers(text)
    assert len(markers) == 2
    assert [m.kind for m in markers] == ["MD_DOCX", "PPTX"]


def test_strip_markers_removes_them_from_visible_text():
    text = "Before ◤MD_DOCX◤{\"md_path\":\"a.md\",\"filename\":\"A.docx\"}◤END_MD_DOCX◤ After"
    stripped = strip_markers(text)
    assert "◤" not in stripped
    assert "Before" in stripped
    assert "After" in stripped


def test_marker_with_malformed_json_is_skipped():
    text = "◤MD_DOCX◤{not json}◤END_MD_DOCX◤"
    markers = find_markers(text)
    assert markers == []


def test_marker_with_no_close_tag_is_skipped():
    text = "◤MD_DOCX◤{\"md_path\":\"a.md\"} no close"
    markers = find_markers(text)
    assert markers == []
```

**Step 2: Run test to verify it fails**

```bash
cd /root/zhanlu/backend && pytest tests/test_artifact_markers.py -q
```
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.artifact_markers'`.

**Step 3: Implement the parser**

Create `backend/app/services/artifact_markers.py`:

```python
"""Marker contract parser for ◤FMT◤{...}◤END_FMT◤ blocks in assistant text.

Skills like `docx`, `pptx`, and `web-artifacts-builder` instruct the LLM to
emit a marker at the end of its reply describing the file it just wrote to
`outputs/`. This module extracts those markers so the backend can route
them into `create_artifact`.

The marker shape is:

    ◤MD_DOCX◤{"md_path": "outputs/report.md", "filename": "Report.docx"}◤END_MD_DOCX◤
    ◤HTML_DOCX◤{"html_path": "outputs/r.html", "filename": "R.docx"}◤END_HTML_DOCX◤
    ◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Deck.pptx"}◤END_PPTX◤

Supported kinds (extensible):
- MD_DOCX   — markdown file → DOCX (pandoc pipeline)
- HTML_DOCX — HTML file → DOCX (preserves styling)
- PPTX      — slide-spec JSON → PPTX (python-pptx pipeline)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Opening token, JSON payload, closing token. The closing token uses the
# same kind string as the opening one. We capture the kind so we can match
# the right closing tag.
MARKER_PATTERN = re.compile(
    r"◤([A-Z_]+)◤(\{[^◤]*?\})◤END_\1◤",
    re.DOTALL,
)

SUPPORTED_KINDS = frozenset({"MD_DOCX", "HTML_DOCX", "PPTX"})


@dataclass
class Marker:
    """A parsed marker occurrence."""

    kind: str                     # e.g. "MD_DOCX"
    payload: dict[str, Any]       # parsed JSON body
    start: int                    # char offset of ◤ opening
    end: int                      # char offset one past ◤END_...◤ closing
    raw: str                      # the full raw marker text (for stripping)

    @property
    def filename(self) -> str:
        return str(self.payload.get("filename", "") or "")


def find_markers(text: str) -> list[Marker]:
    """Return all supported markers in ``text``, in order of appearance.

    Malformed JSON, unknown kinds, and mismatched open/close tags are
    silently skipped (we never fail the host message on a bad marker).
    """
    if not text:
        return []
    out: list[Marker] = []
    for m in MARKER_PATTERN.finditer(text):
        kind = m.group(1)
        if kind not in SUPPORTED_KINDS:
            logger.debug("Skipping unsupported marker kind %r", kind)
            continue
        try:
            payload = json.loads(m.group(2))
        except json.JSONDecodeError:
            logger.debug("Skipping marker %r with malformed JSON", kind)
            continue
        if not isinstance(payload, dict):
            continue
        out.append(
            Marker(
                kind=kind,
                payload=payload,
                start=m.start(),
                end=m.end(),
                raw=m.group(0),
            )
        )
    return out


def strip_markers(text: str) -> str:
    """Remove all marker blocks from ``text`` and tidy up surrounding whitespace."""
    if not text:
        return text
    cleaned = MARKER_PATTERN.sub("", text)
    # Collapse 3+ newlines left behind by removals
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def iter_kind(markers: list[Marker], kind: str) -> Iterator[Marker]:
    """Yield markers of a specific kind (convenience)."""
    for m in markers:
        if m.kind == kind:
            yield m
```

**Step 4: Run test to verify it passes**

```bash
cd /root/zhanlu/backend && pytest tests/test_artifact_markers.py -q
```
Expected: PASS (7 passed).

**Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/artifact_markers.py backend/tests/test_artifact_markers.py
git commit -m "feat(artifacts): marker contract parser for ◤MD_DOCX◤ / ◤HTML_DOCX◤ / ◤PPTX◤"
```

---

### Task 2: Wire the marker parser into the v2 `add_message` flow

**Files:**
- Modify: `backend/app/routers/agents.py` (the v2 `add_message` endpoint, around line 1299)
- Create: `backend/tests/test_add_message_marker.py`

**Step 1: Write the failing test**

Create `backend/tests/test_add_message_marker.py`:

```python
"""Integration test: assistant text containing ◤MD_DOCX◤ triggers create_artifact."""
import io
import os
import tempfile
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app


def _seed_md_file(tmp_dir, content="# Hello\n\nWorld"):
    """Drop a minimal markdown file into a temp outputs dir, return its path."""
    out_dir = os.path.join(tmp_dir, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "report.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def test_marker_triggers_create_artifact(tmp_path):
    md_path = _seed_md_file(str(tmp_path))
    marker_payload = (
        f'◤MD_DOCX◤{{"md_path": "{md_path}", "filename": "Report.docx"}}◤END_MD_DOCX◤'
    )

    # Stub out the LLM call so the agent's reply is just the marker text
    fake_assistant_text = "Here is your report.\n\n" + marker_payload

    # Patch the LLM invocation inside the agent run
    with patch("app.routers.agents._run_llm_for_agent") as mock_llm:
        mock_llm.return_value = (fake_assistant_text, [])  # (text, tool_calls)
        client = TestClient(app)
        # Replace with the actual conversation-creation endpoint shape
        resp = client.post(
            "/api/agents/default/add_message",
            json={"content": "make me a report", "role": "user"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The visible message text should NOT contain the marker
        assert "◤" not in str(body)
        # There should be an artifact of type docx associated with the conversation
        artifacts = body.get("artifacts") or []
        assert any(a.get("artifact_type") == "docx" for a in artifacts)
```

**Step 2: Run test to verify it fails**

```bash
cd /root/zhanlu/backend && pytest tests/test_add_message_marker.py -q
```
Expected: FAIL — either 404 on the endpoint, no artifact created, or the marker still visible.

**Step 3: Locate the assistant-text persistence point**

In `backend/app/routers/agents.py`, find the v2 `add_message` endpoint (around line 1299, where `conv.to_dict()` is returned after persisting the assistant message). Locate the place where `assistant_content` is finalized before being saved to the DB.

**Step 4: Insert the marker-handling block**

Right before the assistant message is persisted, add:

```python
# ── Marker contract: ◤MD_DOCX◤ / ◤HTML_DOCX◤ / ◤PPTX◤ → create_artifact ──
from app.services.artifact_markers import find_markers, strip_markers

_markers = find_markers(assistant_content)
if _markers:
    from app.services.tool_handlers.artifact_tool import execute_create_artifact
    for _m in _markers:
        try:
            # Map marker kind to the payload shape create_artifact expects
            if _m.kind == "MD_DOCX":
                _payload = {"md_path": _m.payload["md_path"]}
                _type = "docx"
            elif _m.kind == "HTML_DOCX":
                _payload = {"html_path": _m.payload["html_path"]}
                _type = "docx"
            elif _m.kind == "PPTX":
                _payload = {"slides_path": _m.payload["slides_path"]}
                _type = "pptx"
            else:
                continue
            execute_create_artifact(
                db=db,
                artifact_type=_type,
                title=_m.filename or f"{_type}-artifact",
                payload=_payload,
                skill=_m.kind.lower(),
                conversation_id=conv.id,
                agent_app_id=agent_app.id if agent_app else None,
            )
        except Exception as _marker_err:
            logger.warning("Marker %s execution failed (non-fatal): %s", _m.kind, _marker_err)
    # Strip markers from the user-visible text
    assistant_content = strip_markers(assistant_content)
```

**Step 5: Run test to verify it passes**

```bash
cd /root/zhanlu/backend && pytest tests/test_add_message_marker.py -q
```
Expected: PASS.

**Step 6: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/agents.py backend/tests/test_add_message_marker.py
git commit -m "feat(chat): parse ◤MD_DOCX◤/◤HTML_DOCX◤/◤PPTX◤ markers in add_message → create_artifact"
```

---

### Task 3: Wire the same parser into the v3 SSE `add_message_stream` flow

**Files:**
- Modify: `backend/app/routers/agents.py` (the v3 `add_message_stream` endpoint, around line 3035)

**Step 1: Apply the same marker-handling block at the point where the final assistant text is assembled before being persisted.**

The SSE stream already accumulates the assistant's reply text for the final DB write. Add the same `find_markers`/`strip_markers` block right before the persist call. The SSE stream itself should also strip markers from the streamed chunks so the client never sees them.

**Step 2: Manually verify with a curl SSE stream**

```bash
curl -N -X POST http://localhost:5002/api/agents/default/add_message_stream \
  -H "Content-Type: application/json" \
  -d '{"content": "make me a report", "role": "user"}'
```
Expected: streamed chunks contain no `◤` characters; final message row has a linked artifact.

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/agents.py
git commit -m "feat(chat): marker parsing in v3 add_message_stream (SSE)"
```

---

## Phase 2 — PPTX inline preview

### Task 4: PPTX → sanitized HTML converter

**Files:**
- Modify: `backend/app/services/artifacts/preview_builder.py`
- Create: `backend/tests/test_pptx_to_html.py`

**Step 1: Write the failing test**

```python
"""PPTX → HTML conversion for inline preview."""
import io
from pptx import Presentation
from pptx.util import Inches
from app.services.artifacts.preview_builder import convert_pptx_to_html


def _make_pptx():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title-only
    slide.shapes.title.text = "Q3 Sales Review"
    tx = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(9), Inches(3))
    tx.text_frame.text = "Revenue up 12% QoQ."
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_convert_pptx_to_html_renders_title_and_body():
    html, messages = convert_pptx_to_html(_make_pptx())
    assert "<h1" in html or "<h2" in html
    assert "Q3 Sales Review" in html
    assert "Revenue up 12%" in html
    assert isinstance(messages, list)


def test_convert_pptx_to_html_escapes_script():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "<script>alert(1)</script>"
    buf = io.BytesIO()
    prs.save(buf)
    html, _ = convert_pptx_to_html(buf.getvalue())
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_convert_pptx_to_html_bad_bytes_returns_empty():
    html, messages = convert_pptx_to_html(b"not a pptx")
    assert html == ""
    assert any("error" in (m or "").lower() for m in messages)
```

**Step 2: Run test to verify it fails**

```bash
cd /root/zhanlu/backend && pytest tests/test_pptx_to_html.py -q
```
Expected: FAIL — `ImportError: cannot import name 'convert_pptx_to_html'`.

**Step 3: Implement the converter (python-pptx based; LibreOffice HTML export is unreliable)**

Append to `backend/app/services/artifacts/preview_builder.py`:

```python
# ── PPTX → HTML inline preview (Task 4) ─────────────────────────────
def convert_pptx_to_html(pptx_bytes: bytes) -> tuple[str, list[str]]:
    """Convert PPTX bytes to sanitized HTML.

    Strategy: extract text frame-by-frame via python-pptx, emit a simple
    HTML structure with one <section> per slide. Images embedded in the
    deck are inlined as base64 <img> tags (cap at 5 MB total). Raw HTML in
    slide text is escaped — we never let it through.

    Returns (html, messages). html is "" on failure.
    """
    import base64
    import html as _html_lib

    try:
        from pptx import Presentation
        from pptx.util import Emu
    except ImportError as exc:  # pragma: no cover
        return "", [f"python-pptx not available: {exc}"]

    try:
        prs = Presentation(io.BytesIO(pptx_bytes))
    except Exception as exc:
        logger.warning("convert_pptx_to_html failed to open: %s", exc)
        return "", [f"pptx error: {exc}"]

    parts: list[str] = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<style>",
        "body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#fafafa}",
        ".slide{background:#fff;border:1px solid #ddd;border-radius:6px;",
        "padding:24px;margin:0 auto 16px;max-width:900px;box-shadow:0 1px 3px rgba(0,0,0,.05)}",
        ".slide h1{margin-top:0;font-size:1.6em;color:#1a1a1a}",
        ".slide h2{font-size:1.2em;color:#444}",
        ".slide p{line-height:1.5;color:#333}",
        ".slide img{max-width:100%;height:auto;border-radius:4px}",
        ".slide-num{font-size:.75em;color:#999;text-align:right;margin-top:8px}",
        "</style></head><body>",
    ]
    messages: list[str] = []
    total_img_bytes = 0

    for i, slide in enumerate(prs.slides, start=1):
        parts.append(f"<section class='slide' data-slide='{i}'>")
        # Title (if any)
        title_text = ""
        if slide.shapes.title is not None:
            title_text = slide.shapes.title.text or ""
        if title_text:
            parts.append(f"<h1>{_html_lib.escape(title_text)}</h1>")
        # Walk all shapes in reading order (top-to-bottom, left-to-right)
        sorted_shapes = sorted(
            slide.shapes,
            key=lambda s: (s.top or 0, s.left or 0),
        )
        for shape in sorted_shapes:
            if shape == slide.shapes.title:
                continue  # already handled
            # Text frames
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(run.text for run in para.runs)
                    if not text.strip():
                        continue
                    # Treat first-level paragraphs as body text; larger fonts → h2
                    font_size = None
                    for run in para.runs:
                        if run.font.size is not None:
                            font_size = run.font.size.pt
                            break
                    tag = "h2" if (font_size and font_size >= 18) else "p"
                    parts.append(f"<{tag}>{_html_lib.escape(text)}</{tag}>")
            # Tables
            elif shape.has_table:
                parts.append("<table border='1' cellpadding='6' cellspacing='0' "
                             "style='border-collapse:collapse;margin:8px 0'>")
                for row in shape.table.rows:
                    parts.append("<tr>")
                    for cell in row.cells:
                        cell_text = cell.text or ""
                        parts.append(f"<td>{_html_lib.escape(cell_text)}</td>")
                    parts.append("</tr>")
                parts.append("</table>")
            # Images
            elif shape.shape_type == 13:  # PICTURE
                try:
                    img = shape.image
                    img_bytes = img.blob
                    if total_img_bytes + len(img_bytes) > 5 * 1024 * 1024:
                        messages.append(f"slide {i}: skipped image (5 MB cap)")
                        continue
                    total_img_bytes += len(img_bytes)
                    b64 = base64.b64encode(img_bytes).decode("ascii")
                    parts.append(
                        f"<img src='data:{img.content_type};base64,{b64}' alt=''>"
                    )
                except Exception as img_exc:
                    messages.append(f"slide {i}: image skipped: {img_exc}")
        parts.append(f"<div class='slide-num'>Slide {i}</div>")
        parts.append("</section>")

    parts.append("</body></html>")
    return "".join(parts), messages
```

**Step 4: Run test to verify it passes**

```bash
cd /root/zhanlu/backend && pytest tests/test_pptx_to_html.py -q
```
Expected: PASS (3 passed).

**Step 5: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/artifacts/preview_builder.py backend/tests/test_pptx_to_html.py
git commit -m "feat(artifacts): PPTX → sanitized HTML converter for inline preview"
```

---

### Task 5: Advertise `preview_modes` + outline on the artifact payload for PPTX

**Files:**
- Modify: `backend/app/routers/artifacts.py`

**Step 1: Extend the DOCX-only branch to also cover PPTX**

In `backend/app/routers/artifacts.py`, change the `if artifact.artifact_type == "docx":` branch to also match `"pptx"`. For PPTX, the outline extraction uses slide titles instead of Word headings. Add a small `extract_pptx_outline` helper in `preview_builder.py`:

```python
def extract_pptx_outline(pptx_bytes: bytes) -> list[dict]:
    """One outline entry per slide, using the slide's title (or "Slide N")."""
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(pptx_bytes))
    except Exception as exc:
        logger.warning("extract_pptx_outline failed: %s", exc)
        return []
    outline = []
    for i, slide in enumerate(prs.slides, start=1):
        title = ""
        if slide.shapes.title is not None:
            title = (slide.shapes.title.text or "").strip()
        outline.append({"level": 1, "text": title or f"Slide {i}", "id": f"slide-{i}"})
    return outline
```

Then update the endpoint branch to use the right outline extractor per type.

**Step 2: Test**

```bash
cd /root/zhanlu/backend && pytest tests/test_artifact_payload_docx.py -q
# Extend the test with a PPTX case
```

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/artifacts.py backend/app/services/artifacts/preview_builder.py backend/tests/test_artifact_payload_docx.py
git commit -m "feat(artifacts): advertise preview_modes + outline for PPTX"
```

---

### Task 6: Serve PPTX HTML preview via `?format=html`

**Files:**
- Modify: `backend/app/routers/artifacts.py` (the `get_preview` endpoint)

**Step 1: Extend the `if fmt == "html":` branch**

Change the type check from `!= "docx"` to `not in ("docx", "pptx")`. Call `convert_pptx_to_html` for PPTX, `convert_docx_to_html` for DOCX.

**Step 2: Test**

```bash
cd /root/zhanlu/backend && pytest tests/test_docx_preview_html_endpoint.py -q
# Add a PPTX case in a new test file test_pptx_preview_html_endpoint.py
```

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/routers/artifacts.py backend/tests/test_pptx_preview_html_endpoint.py
git commit -m "feat(artifacts): serve PPTX → HTML via /preview?format=html"
```

---

### Task 7: React reader for PPTX

**Files:**
- Create: `frontend/src/components/chat/PptxArtifactPreview.jsx`
- Create: `frontend/src/components/chat/PptxArtifactPreview.test.jsx`
- Modify: `frontend/src/components/chat/ArtifactPreviewCard.jsx`
- Modify: `frontend/src/components/chat/ArtifactPreviewSheet.jsx`

**Step 1: Model the component on `DocxArtifactPreview.jsx` but with slide-aware styling**

The outline sidebar becomes a slide-number sidebar. Clicking a slide scrolls to it. Use the same fetch pattern (`/api/artifacts/{id}/preview?format=html`).

**Step 2: Wire it into the card and the sheet, mirroring the DOCX branches**

**Step 3: Test**

```bash
cd /root/zhanlu/frontend && npx vitest run src/components/chat/PptxArtifactPreview.test.jsx
```

**Step 4: Commit**

```bash
cd /root/zhanlu && git add frontend/src/components/chat/PptxArtifactPreview.jsx frontend/src/components/chat/PptxArtifactPreview.test.jsx frontend/src/components/chat/ArtifactPreviewCard.jsx frontend/src/components/chat/ArtifactPreviewSheet.jsx
git commit -m "feat(chat): PptxArtifactPreview inline reader"
```

---

## Phase 3 — Prompt block + skill upgrades

### Task 8: Tighten the default-skills prompt block to mention `run_sandbox_skill`

**Files:**
- Modify: `backend/app/services/agent_prompts.py` (the `_build_default_skills_block` function, line 52-77)

**Step 1: Update the block to explicitly tell the agent how to execute file-generation skills**

Replace the `lines = [...]` block with:

```python
        lines = [
            "\n\n---\n\n",
            "# DEFAULT SKILLS [Built-in, Always Available]\n\n",
            "The following skills are always available to you. When the user "
            "asks for a deliverable (report, deck, PDF, dashboard, web page, "
            "documentation), follow this exact recipe:\n\n",
            "1. Call `skill_view(name)` to load the skill's methodology.\n",
            "2. Follow the methodology to produce the file content.\n",
            "3. If the skill body instructs you to emit a marker "
            "(◤MD_DOCX◤ / ◤HTML_DOCX◤ / ◤PPTX◤), emit it at the END of "
            "your reply — the platform will detect it and create the "
            "artifact automatically.\n",
            "4. For long-running or tool-heavy generation (LibreOffice, "
            "pandoc, custom scripts), call `run_sandbox_skill(format=..., "
            "data=..., title=..., instructions=...)` instead of inline "
            "execution — it runs in an isolated Docker sandbox.\n\n",
        ]
```

**Step 2: Quick smoke — start the backend and inspect one agent prompt**

```bash
cd /root/zhanlu/backend && python -c "
from app.services.agent_prompts import _DEFAULT_SKILLS_BLOCK
assert 'run_sandbox_skill' in _DEFAULT_SKILLS_BLOCK
assert '◤MD_DOCX◤' in _DEFAULT_SKILLS_BLOCK
print('OK')
"
```

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/app/services/agent_prompts.py
git commit -m "feat(prompts): default-skills block mentions run_sandbox_skill + marker contract"
```

---

### Task 9: Add slide-type conventions to `pptx/SKILL.md` (MiniMax idea)

**Files:**
- Modify: `backend/skills/pptx/SKILL.md`

**Step 1: Append a "Slide-type conventions" section after the "Design Ideas" section**

```markdown
## Slide-type Conventions

Every deck uses a small set of repeatable slide types. Pick the right type per slide — don't improvise a new layout for each one.

| Type | When to use | Layout |
|---|---|---|
| **Cover** | Slide 1 only | Dark bg, large title (44-56pt), subtitle (18-24pt), optional logo/image. No body text. |
| **Section divider** | Between major sections | Dark or accent bg, section number + name, optional one-line takeaway. |
| **TOC** | After cover, if deck >8 slides | Numbered list of sections, right-aligned page refs optional. |
| **Content** | The bulk of the deck | Title + body, following the layout options in *Design Ideas* (two-column, icon rows, 2x2 grid, half-bleed). |
| **Data callout** | Big number / KPI slide | One giant stat (60-72pt) + small label, plus a supporting sentence or chart. |
| **Comparison** | Before/after, pros/cons, options A/B/C | Two or three columns of equal width, headers aligned, parallel structure. |
| **Quote** | Customer or exec quote | Large italic quote, attribution below, optional photo. |
| **Summary / Next steps** | Final content slide | 3-5 bullets, action-oriented, owner + date if applicable. |
| **Thank you / Contact** | Last slide | Mirrors cover style, simple. |

Rules:
- A deck usually has 1 cover, 0-1 TOC, 1+ section dividers, N content slides, 1 summary, 1 thank-you.
- Don't skip the summary slide — decks without one feel unfinished.
- Don't use a "Content" layout for the cover or summary.
```

**Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/skills/pptx/SKILL.md
git commit -m "feat(skills): add slide-type conventions to pptx skill (MiniMax-inspired)"
```

---

### Task 10: Wire XSD validation into `docx` skill's `pack.py` invocation

**Files:**
- Modify: `backend/skills/docx/SKILL.md`
- Verify: `backend/skills/docx/scripts/office/pack.py` already invokes `validate.py`

**Step 1: Check current `pack.py` behavior**

```bash
cd /root/zhanlu && grep -n "validate\|xsd" backend/skills/docx/scripts/office/pack.py | head -20
```

If `pack.py` doesn't already call the validator, update it to do so. If it does, just document the behavior in `SKILL.md`.

**Step 2: Update `SKILL.md` to make XSD validation explicit**

In the "Step 3: Pack" section, change the `--validate false` line to make it clear that validation is on-by-default and uses the bundled 39 XSD schemas.

**Step 3: Commit**

```bash
cd /root/zhanlu && git add backend/skills/docx/SKILL.md backend/skills/docx/scripts/office/pack.py
git commit -m "feat(skills): wire XSD validation into docx pack pipeline (MiniMax-inspired)"
```

---

### Task 11: Add `◤PPTX◤` marker contract to `pptx/SKILL.md`

**Files:**
- Modify: `backend/skills/pptx/SKILL.md`

**Step 1: Add a marker section after "Creating from Scratch"**

```markdown
### Emitting the PPTX marker (headless / managed-agent flow)

When running in a headless environment (no live Office), after producing the slide-spec JSON file, emit at the END of your reply:

\`\`\`
◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Deck.pptx"}◤END_PPTX◤
\`\`\`

The platform will detect the marker and call `create_artifact` with `type="pptx"`, rendering the file via the sandbox pipeline and surfacing an inline preview card. The marker is stripped from the visible reply.

Rules:
- `slides_path` is relative to your workspace root and points to the slide-spec JSON (see "Creating from Scratch").
- `filename` must end in `.pptx`.
- Emit the marker exactly once per deck, at the very end of your reply.
```

**Step 2: Commit**

```bash
cd /root/zhanlu && git add backend/skills/pptx/SKILL.md
git commit -m "feat(skills): add ◤PPTX◤ marker contract to pptx skill"
```

---

## Phase 4 — Final regression

### Task 12: Full test suite run

```bash
cd /root/zhanlu/backend && pytest -q
```
Expected: all previous tests pass + new ones (target ≥ 55 backend tests).

```bash
cd /root/zhanlu/frontend && npx vitest run
```
Expected: all previous tests pass + new ones (target ≥ 92 frontend tests).

### Task 13: Update the artifact preview spec

**Files:**
- Modify: `docs/04_sandbox_artifacts/Zhanlu_Artifact_Preview_Implementation_Spec.md`

Append a new section "Marker Contract + PPTX Inline Preview (2026-07-21)" documenting:
- The three marker kinds and their JSON payload shapes
- The flow: LLM emits marker → `artifact_markers.find_markers` → `create_artifact` → blob stored → preview rendered → card displayed
- The PPTX→HTML converter strategy (python-pptx, 5 MB image cap, slide-per-section HTML)
- The updated default-skills prompt block

### Task 14: Commit the spec update

```bash
cd /root/zhanlu && git add docs/04_sandbox_artifacts/Zhanlu_Artifact_Preview_Implementation_Spec.md
git commit -m "docs(artifacts): document marker contract + PPTX inline preview"
```

---

## Acceptance criteria

- [ ] `find_markers` parses all 3 marker kinds, skips malformed JSON, strips cleanly
- [ ] Emitting `◤MD_DOCX◤` in v2 `add_message` produces a DOCX artifact and strips the marker from the visible reply
- [ ] Same for v3 SSE `add_message_stream`
- [ ] `convert_pptx_to_html` renders title, body, tables, images (≤5 MB), escapes `<script>`
- [ ] `GET /api/artifacts/{id}` for PPTX advertises `preview_modes` + slide outline
- [ ] `GET /api/artifacts/{id}/preview?format=html` returns sanitized HTML for both DOCX and PPTX
- [ ] `PptxArtifactPreview` React component renders in card + sheet
- [ ] `_DEFAULT_SKILLS_BLOCK` mentions `run_sandbox_skill` and the marker contract
- [ ] `pptx/SKILL.md` has slide-type conventions + `◤PPTX◤` marker contract
- [ ] `docx/SKILL.md` documents XSD validation invocation
- [ ] All backend tests pass (≥ 55)
- [ ] All frontend tests pass (≥ 92)
- [ ] Spec doc updated

## Out of scope (deferred)

- Importing MiniMax/Anthropic skills wholesale (current ones are stronger; we adopted only the 2 specific ideas)
- Real-time collaborative PPTX editing
- Custom React PPTX renderer (using HTML preview, not a `pptx-preview` JS lib)
- XSD validation on the **input** side (only on pack output)
- MS Word Online tier for PPTX (DOCX has it; PPTX can reuse the same signed-URL pattern later)
