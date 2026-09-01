"""Tests for DOCX intent detection and exporter integration."""
from __future__ import annotations

import pytest

from app.services.synexia.intent_router import detect_file_intent


class TestDocxIntentDetection:
    """detect_file_intent should return 'docx' for Word/document requests."""

    def test_docx_keyword(self):
        assert detect_file_intent("make me a DOCX sales report") == "docx"

    def test_word_document(self):
        assert detect_file_intent("create a Word document") == "docx"

    def test_dot_doc(self):
        assert detect_file_intent("save as .doc file") == "docx"

    def test_chinese_gongwen(self):
        assert detect_file_intent("生成一份公文") == "docx"

    def test_chinese_report_doc(self):
        assert detect_file_intent("帮我写一个报告文档") == "docx"

    def test_pptx_not_docx(self):
        assert detect_file_intent("make a PowerPoint presentation") == "pptx"

    def test_xlsx_not_docx(self):
        assert detect_file_intent("export as spreadsheet") == "xlsx"

    def test_no_file_intent(self):
        assert detect_file_intent("hello, how are you?") is None

    def test_empty_string(self):
        assert detect_file_intent("") is None

    def test_none(self):
        assert detect_file_intent(None) is None


class TestDocxExporterRegistration:
    """The docx format must be in SUPPORTED_FORMATS and the render function should work."""

    def test_docx_in_supported_formats(self):
        from app.services.artifacts.exporters import SUPPORTED_FORMATS, render
        from app.services.artifacts.exporters._common import ExportContext
        from app.services.synexia.contracts import ReportCardPayload

        assert "docx" in SUPPORTED_FORMATS

    def test_docx_render_produces_bytes(self):
        """A minimal payload should produce a non-empty .docx."""
        from app.services.artifacts.exporters import render, ExportContext
        from app.services.synexia.contracts import ReportCardPayload, InsightSpec

        payload = ReportCardPayload(
            title="Test Report",
            summary="This is a test report for DOCX export.",
            kpis=[
                {"label": "Revenue", "value": "¥1,200,000"},
                {"label": "Orders", "value": "847"},
            ],
            insights=[
                InsightSpec(text="Sales are trending upward."),
                InsightSpec(text="Top product is C5 Olefin."),
            ],
        )
        ctx = ExportContext(source="test", conversation_id="conv-1")

        data, mime, ext = render("docx", payload, ctx)

        assert isinstance(data, bytes)
        assert len(data) > 0
        assert mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert ext == ".docx"
