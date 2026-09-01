"""Phase C tests for ExportService doc_type + ExportContext plumbing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.artifacts.exporters.service import ExportService
from app.services.artifacts.exporters._common import ExportContext


def _artifact(id_: str = "a-1"):
    """A minimal stand-in for the ORM Artifact that satisfies _payload_from_artifact."""
    return SimpleNamespace(
        title="t",
        conversation_id="c",
        canonical_format="json",
        id=id_,
        metadata_json=None,
        description="",
    )


class TestDocTypeValidation:
    def test_get_or_render_normalizes_unknown_doc_type(self):
        with patch.object(
            ExportService, "_find_cached_format_blob", return_value=None,
        ), patch.object(
            ExportService, "_render_and_store", return_value=(b"x", "x", "f"),
        ) as spy:
            ExportService(MagicMock()).get_or_render(
                _artifact(), "docx",
                user_message="m", sql=None, source=None,
                doc_type="legal-brief",
            )
        assert spy.call_args.kwargs["doc_type"] == "report"

    def test_get_or_render_preserves_valid_doc_types(self):
        with patch.object(
            ExportService, "_find_cached_format_blob", return_value=None,
        ), patch.object(
            ExportService, "_render_and_store", return_value=(b"x", "x", "f"),
        ) as spy:
            for dt in ("report", "brief", "memo"):
                ExportService(MagicMock()).get_or_render(
                    _artifact(), "docx",
                    user_message="m", sql=None, source=None,
                    doc_type=dt,
                )
        last_doc_type = spy.call_args.kwargs["doc_type"]
        assert last_doc_type == "memo"

    def test_get_or_render_defaults_doc_type_report(self):
        with patch.object(
            ExportService, "_find_cached_format_blob", return_value=None,
        ), patch.object(
            ExportService, "_render_and_store", return_value=(b"x", "x", "f"),
        ) as spy:
            ExportService(MagicMock()).get_or_render(
                _artifact(), "docx",
                user_message="m", sql=None, source=None,
            )
        assert spy.call_args.kwargs["doc_type"] == "report"


class TestDocTypeCaching:
    def test_report_doc_type_consults_cache(self):
        with patch.object(ExportService, "_find_cached_format_blob", return_value=SimpleNamespace(
            mime_type="x", file_name="f", content_hash="h",
        )) as fc, \
             patch.object(ExportService, "_blob_data", return_value=b"\xff"), \
             patch.object(ExportService, "_render_and_store") as rs:
            ExportService(MagicMock()).get_or_render(
                _artifact(), "docx", doc_type="report",
            )
        assert fc.called, "cache lookup required when doc_type=report + no theme"
        assert not rs.called

    def test_brief_doc_type_skips_cache(self):
        with patch.object(ExportService, "_find_cached_format_blob") as fc, \
             patch.object(ExportService, "_render_and_store", return_value=(b"x", "x", "f")):
            ExportService(MagicMock()).get_or_render(
                _artifact(), "docx", doc_type="brief",
            )
        assert not fc.called, "doc_type != report must skip the cache"

    def test_memo_doc_type_skips_cache(self):
        with patch.object(ExportService, "_find_cached_format_blob") as fc, \
             patch.object(ExportService, "_render_and_store", return_value=(b"x", "x", "f")):
            ExportService(MagicMock()).get_or_render(
                _artifact(), "docx", doc_type="memo",
            )
        assert not fc.called


class TestDocTypeRenderAndStore:
    def test_threads_doc_type_into_export_context(self):
        captured = {}

        def fake_render(format, payload, ctx):
            captured["ctx"] = ctx
            return b"\x78", "x", "f"

        with patch(
            "app.services.artifacts.exporters.service.render",
            new=fake_render,
        ):
            ExportService(MagicMock())._render_and_store(
                _artifact("a-6"), "docx",
                user_message="m", sql=None, source=None,
                doc_type="memo",
                persist=False,
            )
        assert captured["ctx"].doc_type == "memo"

    def test_default_doc_type_on_render_and_store_is_report(self):
        captured = {}

        def fake_render(format, payload, ctx):
            captured["ctx"] = ctx
            return b"\x78", "x", "f"

        with patch(
            "app.services.artifacts.exporters.service.render",
            new=fake_render,
        ), patch.object(ExportService, "_current_version", return_value=None), \
             patch.object(ExportService, "_attach_format_blob", return_value=SimpleNamespace(
                 mime_type="x", file_name="f",
             )):
            ExportService(MagicMock())._render_and_store(
                _artifact("a-7"), "docx",
                user_message="m", sql=None, source=None,
            )
        assert captured["ctx"].doc_type == "report"


class TestExportContextDocType:
    """Backward-compat: ExportContext.doc_type defaults to 'report'."""

    def test_default_value(self):
        ctx = ExportContext()
        assert ctx.doc_type == "report"

    def test_explicit_value(self):
        ctx = ExportContext(doc_type="brief")
        assert ctx.doc_type == "brief"
