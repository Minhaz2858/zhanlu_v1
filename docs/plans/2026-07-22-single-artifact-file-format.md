# Single-Artifact Per File-Format Intent — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When the user asks for a specific file format (docx/pptx/xlsx/pdf), the chat shows exactly **one** artifact card for that file, its inline preview uses the same rich HTML the user already loves, and the downloaded file itself looks styled (not the plain python-docx default).

**Architecture:**
- **Layer 1 (de-dup):** In `_collect_artifact_results` and the report-finalize path, detect when a file format is requested and drop the in-chat HTML ReportCard and any sibling HTML preview artifact. The file-format artifact is the only card shown.
- **Layer 2 (rich preview):** The file-format artifact (docx/pptx/xlsx) gets a sibling HTML "sidecar" produced by the same `run_sandbox_skill` call. The artifact's `preview_url` and `preview_artifact_id` point at the sidecar. The frontend's `DocxArtifactPreview` / `PptxArtifactPreview` render the sidecar's HTML when present, falling back to mammoth/pptx2html otherwise.
- **Layer 3 (enrich the file):** Replace the barebones `python-docx` generator with a styled version that uses theme colors, header/footer, KPI table, data table, and embedded bar chart placeholders. Same approach for pptx if time permits; otherwise leave the default.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), React + Vite (frontend), python-docx 1.2 (sandbox), mammoth (preview fallback), `zhanlu-sandbox-python:latest` image.

---

## Task 1: Add a sibling-HTML preview to `run_sandbox_skill` for docx/pptx/xlsx

**Files:**
- Modify: `backend/app/services/tool_handlers/sandbox_tool.py:160-250` (artifact + version creation)
- Modify: `backend/app/services/sandbox/sandbox_runner.py:550-560` (add `generate_html_sidecar`)
- Test: `backend/tests/test_sandbox_sibling_preview.py` (new)

**Why:** The current pipeline runs a single Docker job that produces one file. We need the same job to also produce a small HTML "sidecar" so the file format artifact has a rich preview without an extra round-trip. The sidecar is built from the same `rows` + `config` so the preview matches the file.

**Step 1: Write the failing test**

```python
# backend/tests/test_sandbox_sibling_preview.py
import pytest
from app.services.tool_handlers.sandbox_tool import run_sandbox_skill_sync
from app.services.artifacts.artifact_service import ArtifactService
from app.database import SessionLocal


def test_docx_job_creates_sibling_html_preview_blob():
    db = SessionLocal()
    svc = ArtifactService(db)
    rows = [
        {"region": "EMEA", "count": 24},
        {"region": "AMER", "count": 18},
    ]
    result = run_sandbox_skill_sync(
        args={"format": "docx", "data": rows, "title": "Sales"},
        db=db, user_id="test", context={"conversation_id": "x"},
    )
    assert result["success"]
    art_id = result["artifact_id"]
    # Sidecar HTML should be linked via metadata
    art = svc.get_artifact(art_id)
    assert art.metadata_json.get("preview_artifact_id")
    sidecar_id = art.metadata_json["preview_artifact_id"]
    sidecar = svc.get_artifact(sidecar_id)
    # Sidecar is an HTML artifact
    assert sidecar.artifact_type == "html"
    # The sidecar has a blob with the report HTML
    blobs = svc.get_version_blobs(sidecar.current_version_id)
    assert any(b.blob_type == "preview" or b.mime_type == "text/html" for b in blobs)
    db.close()
```

**Step 2: Run test, see it fail**

Run: `cd backend && pytest tests/test_sandbox_sibling_preview.py -v`
Expected: FAIL — `metadata_json["preview_artifact_id"]` is missing.

**Step 3: Implement**

In `sandbox_tool.py`, after the version is created and the sandbox job runs, add a sibling HTML artifact:

```python
# After sandbox_result.get("success") and the artifact fields are set:
from app.services.artifacts.html_report_renderer import render_report_html
if fmt in ("docx", "pptx", "xlsx") and sandbox_result.get("success"):
    try:
        html_content = render_report_html(
            title=title, rows=data, instructions=instructions, source=context.get("source_name", "")
        )
        sidecar = artifact_service.create_artifact(
            artifact_type="html", title=f"{title} (preview)",
            conversation_id=conversation_id, created_by_agent_id=agent_app_id,
            description="Rich HTML preview of the file-format artifact",
        )
        sidecar_version = artifact_service.create_version(
            artifact_id=sidecar.id,
            changelog="Sidecar preview",
            produced_by_skill="sandbox_runner_sidecar",
        )
        artifact_service.store_blob(
            sidecar_version.id, blob_type="preview",
            file_name=f"{title}-preview.html",
            mime_type="text/html", data=html_content.encode("utf-8"),
        )
        artifact.metadata_json = dict(artifact.metadata_json or {})
        artifact.metadata_json["preview_artifact_id"] = sidecar.id
        db.add(artifact); db.flush()
    except Exception as e:
        logger.warning("Sidecar preview generation failed: %s", e)
```

In `sandbox_runner.py`, expose a function to render the HTML sidecar (we can reuse `generate_html` from the sandbox but calling it via an in-process helper is faster than spinning another Docker job):

```python
def generate_html_sidecar(rows, config, instructions):
    """Return the HTML body that should be served as the rich preview.

    Reuses the same Plotly-based dashboard generator as ``generate_html``
    but strips the <html>/<head> envelope so we can serve just the body
    in the preview iframe.
    """
    full = generate_html(rows, config, instructions)
    # Extract <body>...</body>
    start = full.index("<body>") + len("<body>")
    end = full.rindex("</body>")
    return full[start:end]
```

Actually a simpler approach: just store the existing `generate_html` output as a `text/html` blob. The docx file remains docx, the preview blob is HTML. Two blobs on the same version (one `original` docx, one `preview` html).

**Step 4: Run test, see it pass**

Run: `cd backend && pytest tests/test_sandbox_sibling_preview.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/app/services/tool_handlers/sandbox_tool.py \
        backend/app/services/sandbox/sandbox_runner.py \
        backend/tests/test_sandbox_sibling_preview.py
git commit -m "feat: generate rich HTML sidecar preview for docx/pptx/xlsx artifacts"
```

---

## Task 2: Suppress duplicate artifact cards when a file format is requested

**Files:**
- Modify: `backend/app/routers/agents.py:962-1005` (`_collect_artifact_results`)
- Test: `backend/tests/test_collect_artifact_results_dedup.py` (new)

**Why:** When the agent calls `run_sandbox_skill` twice (once for `html` preview, once for `docx` file), both land in `message.artifacts`. The user sees two cards. The fix: when a file-format artifact exists in the tool_calls and an HTML preview exists for the same data, drop the HTML from the result list and link the file-format artifact's `preview_url` to the HTML.

**Step 1: Write the failing test**

```python
def test_collect_drops_html_sibling_when_docx_present():
    tcs = [
        {"name": "run_sandbox_skill", "results": {
            "success": True, "artifact_id": "html-1", "type": "html",
            "title": "Sales", "preview_url": "/preview/html-1",
        }},
        {"name": "run_sandbox_skill", "results": {
            "success": True, "artifact_id": "docx-1", "type": "docx",
            "title": "Sales", "preview_url": None,
            "metadata": {"preview_artifact_id": "html-1"},
        }},
    ]
    out = _collect_artifact_results(tcs, db=None, message_id=None, conversation_id=None)
    # Only the docx should be in the result
    ids = [a["artifact_id"] for a in out]
    assert ids == ["docx-1"]
    # And the docx's preview_url should be lifted from the HTML
    assert out[0]["preview_url"] == "/preview/html-1"
    assert out[0]["preview_artifact_id"] == "html-1"
```

**Step 2: Run test, see it fail**

Run: `cd backend && pytest tests/test_collect_artifact_results_dedup.py -v`
Expected: FAIL — both artifacts are returned, preview_url is None on the docx.

**Step 3: Implement**

In `_collect_artifact_results` after building the `artifacts` list:

```python
# If we have a file-format artifact (docx/pptx/xlsx/pdf) and an HTML
# sibling in the same batch, drop the HTML from the chat payload and
# lift the HTML's preview_url + preview_artifact_id onto the file-
# format artifact.  This is the "one card per file format" rule the
# user asked for.
_FILE_FORMATS = {"docx", "pptx", "xlsx", "pdf", "md"}
html_by_signature: dict[tuple, dict] = {}
for a in artifacts:
    if a.get("type") == "html":
        sig = (a.get("title", "").lower(), tuple(sorted(a.get("file_name", "").lower().split("-"))))
        html_by_signature.setdefault(sig, []).append(a)

kept = []
seen_sigs_consumed = set()
for a in artifacts:
    if a.get("type") in _FILE_FORMATS:
        # Find matching HTML sibling by title
        title_key = a.get("title", "").lower()
        for sig, siblings in html_by_signature.items():
            if sig[0] in title_key or title_key in sig[0]:
                if sig in seen_sigs_consumed:
                    continue
                a["preview_url"] = siblings[0].get("preview_url") or a.get("preview_url")
                a["preview_artifact_id"] = siblings[0].get("artifact_id")
                seen_sigs_consumed.add(sig)
                break
        kept.append(a)
    elif a.get("type") == "html":
        # Already consumed as a sibling, skip
        continue
    else:
        kept.append(a)
artifacts = kept
```

**Step 4: Run test, see it pass**

Run: `cd backend && pytest tests/test_collect_artifact_results_dedup.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/app/routers/agents.py \
        backend/tests/test_collect_artifact_results_dedup.py
git commit -m "feat: de-duplicate HTML preview when file-format artifact is present"
```

---

## Task 3: Make frontend `DocxArtifactPreview` prefer the rich HTML sidecar

**Files:**
- Modify: `frontend/src/components/chat/ArtifactPreviewPane.jsx:78-118` (preview branch for `docx`)
- Modify: `frontend/src/components/chat/DocxArtifactPreview.jsx` (use preview_artifact_id)

**Why:** Today `DocxArtifactPreview` always fetches `/api/artifacts/{id}/preview?format=html` and renders mammoth HTML. With the sidecar, we have a richer HTML artifact (the Plotly one). Prefer the sidecar if `artifact.preview_artifact_id` is set.

**Step 1: Update ArtifactPreviewPane for docx/pptx branch**

Replace:
```jsx
{artifact.type === 'docx' ? (
  <DocxArtifactPreview artifactId={artifactId} ... />
) : ...}
```
With:
```jsx
{artifact.type === 'docx' ? (
  artifact.preview_artifact_id ? (
    <iframe
      key={`preview-${artifactId}`}
      src={`/api/artifacts/${artifact.preview_artifact_id}/preview?format=html`}
      title={title}
      className="h-full w-full border-0"
    />
  ) : (
    <DocxArtifactPreview artifactId={artifactId} ... />
  )
) : ...}
```

Same for pptx branch.

**Step 2: Test in browser**

Click the docx card in the existing "make sales report" chat. The right pane should render the rich HTML (KPIs, Plotly chart) instead of the plain mammoth output.

**Step 3: Commit**

```bash
git add frontend/src/components/chat/ArtifactPreviewPane.jsx
git commit -m "feat: prefer rich HTML sidecar over mammoth preview for docx/pptx"
```

---

## Task 4: Enrich the docx generator in the sandbox

**Files:**
- Modify: `backend/app/services/sandbox/sandbox_runner.py:460-505` (`generate_docx`)
- Test: `backend/tests/test_docx_generator.py` (new)

**Why:** The current docx is plain — just a title, an instructions paragraph, and a data table. The user wants a styled output (similar to the rich HTML). We can do a lot with python-docx:
- Title with custom font/color/size
- Subtitle/date metadata
- KPI cards as a styled table (4-column with bold large value cells)
- Data table with colored header row
- Insights as bullet list with colored marker

**Step 1: Write the failing test**

```python
def test_docx_has_styled_title_and_kpi_table():
    from app.services.sandbox.sandbox_runner import generate_docx
    rows = [
        {"region": "EMEA", "count": 24},
        {"region": "AMER", "count": 18},
    ]
    config = {"title": "Sales", "kpis": [{"label": "Total", "value": "42", "caption": "x"}]}
    instructions = "Make a report"
    # Run in temp dir
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        generate_docx(rows, config, instructions)
        out = os.path.join(d, "report.docx")
        assert os.path.exists(out)
        from docx import Document
        d = Document(out)
        # The first paragraph should be the title with a colored run
        title_para = d.paragraphs[0]
        assert title_para.runs
        # Color should be set (orange-ish)
        from docx.shared import RGBColor
        run = title_para.runs[0]
        assert run.font.color and run.font.color.rgb is not None
```

**Step 2: Run test, see it fail**

Run: `cd backend && pytest tests/test_docx_generator.py -v`
Expected: FAIL — no color is set on the title run.

**Step 3: Rewrite `generate_docx` with styling**

```python
def generate_docx(rows, config, instructions):
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()
    # Theme
    ORANGE = RGBColor(0xE6, 0x7E, 0x22)
    DARK = RGBColor(0x1F, 0x2A, 0x44)
    MUTED = RGBColor(0x6B, 0x72, 0x80)

    # --- Title ---
    title_text = config.get("title", "Report")
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.LEFT
    title_run = title_para.add_run(f"📊 {title_text}")
    title_run.font.size = Pt(26)
    title_run.font.bold = True
    title_run.font.color.rgb = DARK

    # Subtitle / metadata
    sub_para = doc.add_paragraph()
    sub_para.paragraph_format.space_after = Pt(12)
    sub_run = sub_para.add_run(
        f"Generated on {datetime.utcnow().strftime('%Y-%m-%d')} · {len(rows)} records"
    )
    sub_run.font.size = Pt(10)
    sub_run.font.color.rgb = MUTED
    sub_run.italic = True

    # --- Instructions ---
    if instructions:
        h = doc.add_paragraph()
        h_run = h.add_run("Instructions")
        h_run.font.size = Pt(14); h_run.font.bold = True; h_run.font.color.rgb = ORANGE
        doc.add_paragraph(instructions)

    # --- KPI table (if any) ---
    kpis = config.get("kpis") or []
    if kpis:
        h = doc.add_paragraph()
        h_run = h.add_run("Key Metrics")
        h_run.font.size = Pt(14); h_run.font.bold = True; h_run.font.color.rgb = ORANGE
        kpi_table = doc.add_table(rows=1, cols=len(kpis))
        kpi_table.style = "Light Grid Accent 1"
        for i, kpi in enumerate(kpis):
            cell = kpi_table.rows[0].cells[i]
            cell.text = ""
            label_p = cell.paragraphs[0]
            label_run = label_p.add_run(kpi.get("label", ""))
            label_run.font.size = Pt(9); label_run.font.color.rgb = MUTED
            value_p = cell.add_paragraph()
            value_run = value_p.add_run(str(kpi.get("value", "")))
            value_run.font.size = Pt(20); value_run.font.bold = True; value_run.font.color.rgb = DARK
            if kpi.get("caption"):
                cap_p = cell.add_paragraph()
                cap_run = cap_p.add_run(kpi["caption"])
                cap_run.font.size = Pt(8); cap_run.font.color.rgb = MUTED
        doc.add_paragraph()  # spacing

    # --- Data table ---
    h = doc.add_paragraph()
    h_run = h.add_run("Data")
    h_run.font.size = Pt(14); h_run.font.bold = True; h_run.font.color.rgb = ORANGE
    columns = derive_columns(rows)
    table = doc.add_table(rows=1, cols=len(columns))
    table.style = "Light Grid Accent 1"
    header_cells = table.rows[0].cells
    for i, col in enumerate(columns):
        header_cells[i].text = ""
        para = header_cells[i].paragraphs[0]
        run = para.add_run(col)
        run.font.bold = True
        run.font.color.rgb = DARK
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        row_cells = table.add_row().cells
        for i, col in enumerate(columns):
            row_cells[i].text = str(row.get(col, ""))

    # --- Insights ---
    insights = config.get("insights") or []
    if insights:
        h = doc.add_paragraph()
        h_run = h.add_run("Key Insights")
        h_run.font.size = Pt(14); h_run.font.bold = True; h_run.font.color.rgb = ORANGE
        for ins in insights:
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(ins if isinstance(ins, str) else ins.get("text", ""))
            run.font.size = Pt(10)

    output_path = OUTPUT_DIR / "report.docx"
    doc.save(str(output_path))
    print(f"WROTE {output_path}")
    return "report.docx"
```

**Step 4: Run test, see it pass**

Run: `cd backend && pytest tests/test_docx_generator.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/app/services/sandbox/sandbox_runner.py \
        backend/tests/test_docx_generator.py
git commit -m "feat: enrich docx generator with styled title, KPIs, insights"
```

---

## Task 5: Pass KPIs/insights from `finalize_into_artifact` to the docx generator

**Files:**
- Modify: `backend/app/services/synexia/finalize.py:316-330` (the `run_sandbox_skill_sync` call's `args`)

**Why:** Task 4's enriched docx wants `kpis` and `insights` from the report payload. The current call only passes `format`, `data`, `title`, `instructions`.

**Step 1: Update the args dict**

```python
sandbox_result = run_sandbox_skill_sync(
    args={
        "format": requested_fmt,
        "data": chart_rows,
        "title": payload.title,
        "instructions": instructions,
        "kpis": [{"label": k.label, "value": k.value, "caption": k.caption}
                 for k in (payload.kpis or []) if k.value],
        "insights": [i.text for i in (payload.insights or [])],
        "source": source,
    },
    db=db, user_id=agent_name, context={...},
)
```

**Step 2: Test the new args flow through to the docx**

Run: `cd backend && pytest tests/test_synexia_finalize_docx.py -v` (existing test, should still pass with extra fields)
Expected: PASS

**Step 3: Commit**

```bash
git add backend/app/services/synexia/finalize.py
git commit -m "feat: pass kpis and insights from synthesis to docx generator"
```

---

## Task 6: End-to-end verification in the browser

**Step 1: Restart the backend**

```bash
docker restart zhanlu-backend
sleep 5
```

**Step 2: Rebuild the frontend**

```bash
cd /root/zhanlu/frontend && npm run build
```

**Step 3: Open the "make sales report fro me" chat and verify**

- Only one artifact card (the DOCX) should be visible in the chat
- Clicking Preview should render the rich HTML (Plotly chart, KPI cards, table) in the right pane
- Clicking Download should give a styled docx (orange title, KPI table, header row in dark)

**Step 4: Run all tests**

```bash
cd backend && pytest tests/ -x
cd frontend && npx vitest run
```

Expected: All pass.

**Step 5: Commit any final touches**

```bash
git add -A && git commit -m "chore: e2e verify single-artifact per file format"
```

---

## Out of Scope

- Replacing the in-chat ReportCard (chart card) with the file-format artifact. The chart card still shows in the chat message as a separate visualization. If the user wants the chart gone, that's a follow-up.
- PPTX styling (only docx in this pass).
- Changing the LLM prompts to discourage the agent from creating extra artifacts. (We handle it at the post-processing layer instead — the agent can still call `run_sandbox_skill` twice; we just drop the HTML from the chat and link it as the docx's sidecar.)
