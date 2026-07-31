# Artifact System Redesign + Contribution Report

**Goal:** Storage abstraction (Postgres/MinIO), canonical source-of-truth, HTML→DOCX/PDF renderers, contribution report gen via artifact pipeline.

**Architecture:** BlobStorage interface → PostgresBlobStorage/MinioBlobStorage → ArtifactService delegates storage. canonical_format on Artifact. HTML-to-format renderers. Report: git log → doc model → Jinja2 HTML → python-docx DOCX → artifact system → stub conversation + MessageArtifact.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, MinIO, Python-docx, Pandoc, WeasyPrint, Jinja2

---

### Task 1: DB Migration + Model Changes

**Files:**
- Create: `backend/alembic/versions/017_artifact_storage_and_canonical.py`
- Modify: `backend/app/models/artifact.py`

Add `storage_uri` (String 500, nullable) to ArtifactBlob, make `data` nullable. Add `canonical_format` (String 10, nullable) to Artifact. Backfill storage_uri for existing blobs.

**Steps:**
1. Write migration with alter_column + add_column + backfill UPDATE
2. Run `alembic upgrade head` → success
3. Update SQLAlchemy model columns
4. Commit: `feat: add storage_uri and canonical_format columns`

---

### Task 2: BlobStorage Interface + Postgres/MinIO Implementations

**Files:**
- Create: `backend/app/services/artifacts/storage.py`
- Modify: `backend/app/services/artifacts/artifact_service.py`

Write ABC with `put/get/delete/exists`. PostgresBlobStorage writes to `data` column. MinioBlobStorage uses minio-py client. Factory function in config resolves backend from settings.ARTIFACT_STORAGE_BACKEND. Update `store_blob()` and `get_blob()` to delegate through storage backend.

**Steps:**
1. Write `storage.py` with interface + both implementations
2. Add `get_blob_storage()` factory to config
3. Modify `store_blob()` → creates row, calls `storage.put()`, updates storage_uri + checksum
4. Modify `get_blob()` → calls `storage.get(blob.storage_uri)`
5. Commit: `feat: BlobStorage abstraction with Postgres and MinIO backends`

---

### Task 3: Generic HTML → DOCX + PDF Renderers

**Files:**
- Create: `backend/app/services/artifacts/exporters/html_docx.py`
- Create: `backend/app/services/artifacts/exporters/html_pdf.py`
- Modify: `backend/app/services/artifacts/exporters/__init__.py`
- Modify: `backend/app/services/artifacts/exporters/service.py`

`html_docx.py`: `render_html_to_docx(html_bytes) → bytes` — pandoc first, python-docx+BeautifulSoup fallback. `html_pdf.py`: `render_html_to_pdf(html_bytes) → bytes` — weasyprint first, LibreOffice fallback. Wire into ExportService: when `canonical_format=="html"`, read original HTML blob and call html→format renderer.

**Steps:**
1. Write `html_docx.py` with pandoc + python-docx fallback
2. Write `html_pdf.py` with weasyprint + LibreOffice fallback
3. Add "html" to SUPPORTED_FORMATS, add routing in __init__.py
4. Update ExportService.get_or_render() for canonical_format awareness
5. Commit: `feat: generic HTML→DOCX and HTML→PDF renderers`

---

### Task 4: Contribution Report Generator

**Files:**
- Create: `backend/scripts/generate_contribution_report.py`
- Create: `backend/scripts/report_templates/contribution_report.html` (Jinja2)

Script flow: analyze git log → structured doc model → Jinja2 HTML → python-docx DOCX → Artifact + Version + 4 blobs (HTML original, DOCX export, PDF preview, PNG thumbnail) → stub Conversation + Message + MessageArtifact.

Doc model includes: executive summary, metrics table, area-by-area breakdown (tools/skills, core chat, artifact system, synexia FSM, enterprise layer, sandbox, agents), key achievements, forward roadmap, tech stack summary.

Template style: professional, print-friendly, matching existing `zhanlu_improvement_report.docx` tone.

**Steps:**
1. Write git log analysis functions
2. Write Jinja2 template
3. Write main generator function
4. Create stub Conversation + Message models
5. Commit: `feat: contribution report generator with artifact pipeline`

---

### Task 5: UI Updates — HtmlReportArtifactPreview

**Files:**
- Create: `frontend/src/components/chat/HtmlReportArtifactPreview.jsx`
- Modify: `frontend/src/components/chat/ArtifactCardList.jsx`
- Modify: `frontend/src/components/chat/ArtifactPreviewCard.jsx`

Add `html_report` type meta (purple, FileCode icon) to TYPE_META in ArtifactPreviewCard. Add export format chips (DOCX, PDF, HTML) when cached formats exist. New HtmlReportArtifactPreview component with outline sidebar navigation.

**Steps:**
1. Add `html_report` to TYPE_META
2. Create HtmlReportArtifactPreview component
3. Wire format chips from `list_available_formats` API
4. Commit: `feat: HtmlReportArtifactPreview UI component`

---

### Task 6: Migrate On-Disk File Writes

**Files:**
- Modify: `backend/app/services/artifacts/preview_builder.py` (remove disk writes)
- Modify: `backend/app/services/artifacts/exporters/docx_export.py` (remove disk writes)

Audit every `open(..., "wb")` and `tempfile.NamedTemporaryFile` in the artifact pipeline. Replace with in-memory BytesIO where possible. For LibreOffice conversions that require a temp file, ensure cleanup in finally block. Add audit comment documenting remaining cases.

**Steps:**
1. Review preview_builder.py → already uses TemporaryDirectory correctly
2. Review docx_export.py → pandoc path uses tempfile + finally cleanup (OK)
3. Document remaining disk writes (sandbox temp, LibreOffice) as "transient, auto-cleaned"
4. Commit: `chore: audit and document on-disk file writes in artifact pipeline`

---

### Task 7: Tests + Documentation

**Files:**
- Create: `backend/tests/test_artifact_storage_backends.py`
- Create: `backend/tests/test_artifact_canonical.py`
- Create: `backend/tests/test_html_renderers.py`
- Modify: `docs/architecture/artifact-system.md`

Tests: Postgres round-trip, canonical format enforcement, HTML→DOCX produces valid ZIP, HTML→PDF produces valid %PDF. Run full test suite, fix regressions. Update architecture doc with new storage flow diagram.

**Steps:**
1. Write storage backend tests
2. Write canonical format tests
3. Write HTML renderer byte-validity tests
4. Run `pytest tests/ -x -v` → all pass
5. Update `docs/architecture/artifact-system.md`
6. Commit: `test: storage, canonical, and HTML renderer tests`

---

### Task 8: Generate + Verify Report

Run `python scripts/generate_contribution_report.py`, verify artifact appears in API (`GET /api/artifacts`), download DOCX (`GET /api/artifacts/{id}/download?format=docx`), verify HTML preview (`GET /api/artifacts/{id}/preview?format=html`), verify PDF preview (`GET /api/artifacts/{id}/preview?format=pdf`).

**Steps:**
1. Run generator script
2. Verify all API endpoints
3. Open preview in browser if available
4. Commit: final polish commit
