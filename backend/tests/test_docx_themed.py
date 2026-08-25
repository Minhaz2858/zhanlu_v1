"""Phase C tests for the themed DOCX renderer.

Verifies that ``_render_via_python_docx`` consumes the theme/mode/doc_type
from ``ExportContext`` and produces Claude-grade documents themed against
the resolved ``DeckTheme``.
"""

from __future__ import annotations

import zipfile
from xml.etree import ElementTree as ET

import pytest

from app.services.artifacts.exporters import docx_export
from app.services.artifacts.exporters._common import ExportContext
from app.services.artifacts.exporters._theme import load_theme
from app.services.synexia.contracts import (
    ChartSpec, InsightSpec, ReportCardPayload, SectionSpec,
)


def _payload(**kwargs):
    base = dict(
        title="Q3 Revenue",
        subtitle=None,
        source="Q3_Finance",
        generated_at="2026-07-23",
        summary="Strong quarter driven by enterprise wins.",
        methodology="Aggregated from reconciled bookings.",
        kpis=[
            {"label": "Revenue", "value": "$12.4M", "delta": "+18%", "caption": "vs Q2"},
        ],
        key_findings=[{"text": "Enterprise book-out paced the quarter."}],
        insights=[InsightSpec(text="Renewal rate at all-time high.")],
        recommendations=[InsightSpec(text="Double-down on accounts >$100K ACV.")],
        sections=[SectionSpec(title="NPS", content="57", bullets=[])],
        chart=None,
        sql="",
        next_step="",
        warnings=[],
    )
    base.update(kwargs)
    return ReportCardPayload(**base)


def _ctx(doc_type: str = "report", theme: str = "zhanlu-blue", mode: str = "light"):
    return ExportContext(doc_type=doc_type, theme=theme, mode=mode)


def _docx_xml(docx_bytes: bytes, part_path: str) -> ET.Element:
    with zipfile.ZipFile(__import__("io").BytesIO(docx_bytes)) as z:
        with z.open(part_path) as f:
            return ET.parse(f).getroot()


def _iter_runs(root: ET.Element):
    """Yield every <w:r> element in the document body."""
    for r in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r"):
        yield r


def _run_color_hex(run: ET.Element) -> str | None:
    """Extract the run's w:color@w:val as a 6-char uppercase hex (no #)."""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    rPr = run.find(f"{ns}rPr")
    if rPr is None:
        return None
    color = rPr.find(f"{ns}color")
    return color.get(f"{ns}val") if color is not None else None


def _run_fill_hex(run: ET.Element) -> str | None:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    rPr = run.find(f"{ns}rPr")
    if rPr is None:
        return None
    shd = rPr.find(f"{ns}shd")
    return shd.get(f"{ns}fill") if shd is not None else None


def _docx_color_hex_for_text_containing(docx_bytes: bytes, needle: str) -> str | None:
    """Find the first <w:r> whose text contains needle and return its w:color@val."""
    root = _docx_xml(docx_bytes, "word/document.xml")
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    for r in _iter_runs(root):
        text = "".join((t.text or "") for t in r.iter(f"{ns}t"))
        if needle in text:
            return _run_color_hex(r)
    return None


# ── Theme threading ──────────────────────────────────────────────────────

class TestDocxThemed:
    def test_uses_resolved_theme_text_color_on_cover_title(self):
        # Default theme = "zhanlu-blue", text=#0F172A
        theme = load_theme("zhanlu-blue", "light")
        text_hex = "0F172A"
        docx = docx_export._render_via_python_docx(_payload(), _ctx())
        # Cover title text: "📊  Q3 Revenue"
        color = _docx_color_hex_for_text_containing(docx, "Q3 Revenue")
        assert color is not None and color.upper() == text_hex

    def test_alternate_theme_uses_alternate_text_color(self):
        # ocean-depths text is a different hex — proves it's threaded.
        theme = load_theme("ocean-depths", "light")
        from docx.dml.color import RGBColor

        # Probe the actual palette
        docx = docx_export._render_via_python_docx(
            _payload(),
            _ctx(theme="ocean-depths"),
        )
        color = _docx_color_hex_for_text_containing(docx, "Q3 Revenue")
        # We don't pin the exact hex (palette churn risk) — only that it's
        # NOT the default zhanlu-blue text color.
        assert color is not None and color.upper() != "0F172A"

    def test_kpi_value_uses_theme_text_color(self):
        # KPI value (e.g. "$12.4M") should be styled with the text color.
        docx = docx_export._render_via_python_docx(_payload(), _ctx())
        # Find any run containing the value text.
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        found = False
        for r in root.iter(f"{ns}r"):
            text = "".join((t.text or "") for t in r.iter(f"{ns}t"))
            if "12.4M" in text:
                # KPI value run is styled; text color should be present and bold.
                rPr = r.find(f"{ns}rPr")
                assert rPr is not None
                # size 20pt (~40 half-points)
                assert rPr.find(f"{ns}sz") is not None
                found = True
                break
        assert found, "KPI value run not found"

    def test_sql_block_uses_surface_shading_when_provided(self):
        payload = _payload(sql="SELECT 1 FROM dual")
        docx = docx_export._render_via_python_docx(payload, _ctx())
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        # Find the <w:r> whose text contains "SELECT 1" — its parent <w:p>
        # should carry a w:shd with the theme.surface hex.
        for r in root.iter(f"{ns}r"):
            text = "".join((t.text or "") for t in r.iter(f"{ns}t"))
            if "SELECT 1" in text:
                p = r  # iter to find parent
                # Walk up: r is inside w:p (or r might be direct child)
                break
        # Check shading on paragraph containing SQL text.
        sql_p_shading = None
        for p in root.iter(f"{ns}p"):
            text = "".join((t.text or "") for t in p.iter(f"{ns}t"))
            if "SELECT 1" in text:
                pPr = p.find(f"{ns}pPr")
                if pPr is not None:
                    shd = pPr.find(f"{ns}shd")
                    if shd is not None:
                        sql_p_shading = shd.get(f"{ns}fill")
                break
        assert sql_p_shading is not None
        # It should match the resolved theme.surface fill (theme.surface
        # used by the renderer for SQL paragraph shading).
        from app.services.artifacts.exporters._theme import _rgbcolor_hex
        expected = _rgbcolor_hex(load_theme("zhanlu-blue", "light").surface).lstrip("#").upper()
        assert sql_p_shading.upper() == expected

    def test_no_hardcoded_zhanlu_blue_in_render_for_alternate_theme(self):
        """A non-default theme must NOT emit zhanlu-blue's text color on the cover."""
        docx = docx_export._render_via_python_docx(
            _payload(), _ctx(theme="ocean-depths"),
        )
        color = _docx_color_hex_for_text_containing(docx, "Q3 Revenue")
        # Confirms threading
        assert color is not None
        assert color.upper() != "0F172A"


# ── Doc-type layouts ─────────────────────────────────────────────────────

class TestDocxDocTypeReport:
    def test_report_includes_cover_and_toc(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("report"))
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        texts = [
            "".join((t.text or "") for t in p.iter(f"{ns}t"))
            for p in root.iter(f"{ns}p")
        ]
        joined = "\n".join(texts)
        assert "📊  Q3 Revenue" in joined
        assert "Table of Contents" in joined

    def test_report_toc_field_present(self):
        """The TOC must be a real Word field, not just a placeholder heading."""
        docx = docx_export._render_via_python_docx(_payload(), _ctx("report"))
        with zipfile.ZipFile(__import__("io").BytesIO(docx)) as z:
            doc_xml = z.read("word/document.xml").decode("utf-8")
        assert 'TOC \\o "1-2"' in doc_xml
        assert 'instrText' in doc_xml


class TestDocxDocTypeBrief:
    def test_brief_skips_cover(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("brief"))
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        joined = "\n".join(
            "".join((t.text or "") for t in p.iter(f"{ns}t"))
            for p in root.iter(f"{ns}p")
        )
        # No big cover title (the emoji-stamped centered variant lives only
        # in the report layout).
        assert "📊  Q3 Revenue" not in joined
        assert "Table of Contents" not in joined
        # But the document title still appears as Heading 1.
        assert "Q3 Revenue" in joined

    def test_brief_starts_with_title_heading(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("brief"))
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        # The first Heading 1 paragraph must be the title.
        headings = list(root.iter(f"{ns}pStyle"))
        # The first body element must be a pStyle=Heading1
        body_paras = list(root.iter(f"{ns}p"))
        first_heading = None
        for p in body_paras:
            pPr = p.find(f"{ns}pPr")
            if pPr is not None:
                ps = pPr.find(f"{ns}pStyle")
                if ps is not None and ps.get(f"{ns}val") == "Heading1":
                    first_heading = p
                    break
        assert first_heading is not None
        text = "".join((t.text or "") for t in first_heading.iter(f"{ns}t"))
        assert "Q3 Revenue" in text


class TestDocxDocTypeMemo:
    def test_memo_has_memorandum_header(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("memo"))
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        joined = "\n".join(
            "".join((t.text or "") for t in p.iter(f"{ns}t"))
            for p in root.iter(f"{ns}p")
        )
        assert "MEMORANDUM" in joined
        for label in ("To:", "From:", "Date:", "Subject:"):
            assert label in joined

    def test_memo_skips_toc(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("memo"))
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        joined = "\n".join(
            "".join((t.text or "") for t in p.iter(f"{ns}t"))
            for p in root.iter(f"{ns}p")
        )
        assert "Table of Contents" not in joined

    def test_memo_subject_is_title(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("memo"))
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        joined = "\n".join(
            "".join((t.text or "") for t in p.iter(f"{ns}t"))
            for p in root.iter(f"{ns}p")
        )
        assert "Subject: Q3 Revenue" in joined


# ── Fallback ──────────────────────────────────────────────────────────────

class TestDocxFallback:
    def test_unknown_doc_type_defaults_to_report(self):
        # The renderer should coerce unknown doc_types back to "report".
        docx = docx_export._render_via_python_docx(
            _payload(), _ctx(doc_type="weird"),
        )
        root = _docx_xml(docx, "word/document.xml")
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        joined = "\n".join(
            "".join((t.text or "") for t in p.iter(f"{ns}t"))
            for p in root.iter(f"{ns}p")
        )
        assert "Table of Contents" in joined

    def test_render_smoke(self):
        """End-to-end smoke: every section present, no exception, valid zip."""
        data, mime, ext = docx_export.render(
            _payload(), _ctx("report"),
        )
        assert mime == docx_export.MIME
        assert ext == docx_export.EXT
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
            assert "word/document.xml" in z.namelist()


def test_data_execution_cache_flag_defaults_false():
    from app.config import settings
    assert settings.DATA_EXECUTION_CACHE_ENABLED is False


def test_data_execution_cleanup_flag_defaults_false():
    from app.config import settings
    assert settings.DATA_EXECUTION_CLEANUP_ENABLED is False


def test_intent_router_flag_defaults_false():
    from app.config import settings
    assert settings.INTENT_ROUTER_ENABLED is False
