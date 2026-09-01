"""Tests for the 2026-08-24 Table-of-Contents removal in the DOCX renderer.

The auto-inserted Word TOC field was reported as useless (it shows a
"Right-click to update" placeholder until manually updated). Per user request
all doc formats now skip the TOC — documents go straight from cover page to
body content.
"""

from __future__ import annotations

import zipfile
from xml.etree import ElementTree as ET

import pytest

from app.services.artifacts.exporters import docx_export
from app.services.artifacts.exporters._common import ExportContext
from app.services.synexia.contracts import (
    InsightSpec, ReportCardPayload, SectionSpec,
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


def _doc_xml(docx_bytes: bytes) -> str:
    with zipfile.ZipFile(__import__("io").BytesIO(docx_bytes)) as z:
        return z.read("word/document.xml").decode("utf-8")


def _doc_root(docx_bytes: bytes) -> ET.Element:
    with zipfile.ZipFile(__import__("io").BytesIO(docx_bytes)) as z:
        with z.open("word/document.xml") as f:
            return ET.parse(f).getroot()


def _joined_texts(root: ET.Element) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    texts = [
        "".join((t.text or "") for t in p.iter(f"{ns}t"))
        for p in root.iter(f"{ns}p")
    ]
    return "\n".join(texts)


class TestDocxNoToc:
    """A rendered report must NOT contain a TOC field or placeholder."""

    def test_report_has_no_toc_field(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("report"))
        doc_xml = _doc_xml(docx)
        assert 'TOC \\o "1-2"' not in doc_xml
        assert "instrText" not in doc_xml

    def test_report_has_no_fldchar_elements(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("report"))
        root = _doc_root(docx)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        assert list(root.iter(f"{ns}fldChar")) == []

    def test_report_has_no_toc_heading_text(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("report"))
        root = _doc_root(docx)
        assert "Table of Contents" not in _joined_texts(root)

    def test_cover_transitions_directly_to_body(self):
        """Body heading (Executive Summary) must follow the cover without a
        TOC page break — i.e. no 'Table of Contents' paragraph between the
        cover title and 'Executive Summary'."""
        docx = docx_export._render_via_python_docx(_payload(), _ctx("report"))
        root = _doc_root(docx)
        texts = _joined_texts(root).split("\n")
        assert "Table of Contents" not in texts
        # Cover title still present
        assert any("Q3 Revenue" in t for t in texts)
        # Executive Summary present and positioned after the cover
        assert any("Executive Summary" in t for t in texts)

    def test_unknown_doc_type_still_renders_without_toc(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx(doc_type="weird"))
        root = _doc_root(docx)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        assert list(root.iter(f"{ns}fldChar")) == []
        assert "Table of Contents" not in _joined_texts(root)


class TestDocxOtherDocTypesUnaffected:
    """memo/brief never had a TOC; they must still render fine."""

    def test_memo_renders_without_toc(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("memo"))
        root = _doc_root(docx)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        assert list(root.iter(f"{ns}fldChar")) == []

    def test_brief_renders_without_toc(self):
        docx = docx_export._render_via_python_docx(_payload(), _ctx("brief"))
        root = _doc_root(docx)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        assert list(root.iter(f"{ns}fldChar")) == []
        assert "Executive Summary" in _joined_texts(root)
