"""Tests for the Layer-1 file-format vs HTML deduplication in
``_collect_artifact_results``.

Covers the "one card per file format" behavior: when a chat turn
produces both a file-format artifact (docx/pptx/xlsx/pdf/md) and an
HTML artifact with the same title (or an explicit sidecar linkage),
the HTML is dropped from the chat payload and the file-format artifact
inherits the HTML's preview_url + preview_artifact_id.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.routers.agents import _collect_artifact_results


def _tool_call(name: str, **kwargs) -> dict:
    return {
        "name": name,
        "results": {"success": True, **kwargs},
    }


def _collect(tool_calls):
    """Run the collector with a mocked DB (skip the link-to-message
    branch by faking db methods)."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    return _collect_artifact_results(
        tool_calls_for_frontend=tool_calls,
        db=db,
        message_id=None,
        conversation_id=None,
    )


def test_dedup_explicit_sidecar_linkage():
    """Sidecar linkage via preview_artifact_id wins over title."""
    html = _tool_call(
        "create_artifact",
        artifact_id="html-1",
        title="Q1 Sales Report",
        type="html",
        preview_url="/api/artifacts/html-1/preview",
    )
    docx = _tool_call(
        "run_sandbox_skill",
        artifact_id="docx-1",
        title="Q1 Sales Report",
        type="docx",
        preview_url="/api/artifacts/docx-1/preview",
        preview_artifact_id="html-1",
    )
    result = _collect([html, docx])
    assert len(result) == 1
    assert result[0]["type"] == "docx"
    assert result[0]["artifact_id"] == "docx-1"
    # Sidecar linkage is forwarded
    assert result[0]["preview_artifact_id"] == "html-1"


def test_dedup_title_based():
    """Title-based dedup catches the html_report + file-format case."""
    html_report = _tool_call(
        "create_artifact",
        artifact_id="html-99",
        title="Q1 Sales Report",
        type="html_report",
        preview_url="/api/artifacts/html-99/preview",
    )
    docx = _tool_call(
        "run_sandbox_skill",
        artifact_id="docx-99",
        title="Q1 Sales Report",
        type="docx",
        preview_url="/api/artifacts/docx-99/preview",
    )
    result = _collect([html_report, docx])
    assert len(result) == 1
    assert result[0]["type"] == "docx"
    assert result[0]["artifact_id"] == "docx-99"
    assert result[0]["preview_artifact_id"] == "html-99"


def test_no_dedup_when_only_html():
    """Single HTML artifact: kept untouched."""
    html = _tool_call(
        "create_artifact",
        artifact_id="html-only",
        title="Just a report",
        type="html",
        preview_url="/api/artifacts/html-only/preview",
    )
    result = _collect([html])
    assert len(result) == 1
    assert result[0]["type"] == "html"


def test_no_dedup_when_only_docx():
    """Single file-format artifact: kept untouched."""
    docx = _tool_call(
        "run_sandbox_skill",
        artifact_id="docx-only",
        title="Just a docx",
        type="docx",
        preview_url="/api/artifacts/docx-only/preview",
    )
    result = _collect([docx])
    assert len(result) == 1
    assert result[0]["type"] == "docx"


def test_no_dedup_when_titles_differ():
    """Different titles: both cards stay (no false-positive dedup)."""
    html = _tool_call(
        "create_artifact",
        artifact_id="html-x",
        title="Q1 Sales",
        type="html",
        preview_url="/api/artifacts/html-x/preview",
    )
    docx = _tool_call(
        "run_sandbox_skill",
        artifact_id="docx-y",
        title="Q2 Marketing Plan",
        type="docx",
        preview_url="/api/artifacts/docx-y/preview",
    )
    result = _collect([html, docx])
    assert len(result) == 2


def test_dedup_with_explicit_link_does_not_match_other_html():
    """When docx has preview_artifact_id, only that specific HTML is
    consumed — other HTML siblings stay."""
    html_sidecar = _tool_call(
        "create_artifact",
        artifact_id="html-sidecar",
        title="Q1 Sales Report",
        type="html",
        preview_url="/api/artifacts/html-sidecar/preview",
    )
    html_other = _tool_call(
        "create_artifact",
        artifact_id="html-other",
        title="Q1 Sales Report",
        type="html",
        preview_url="/api/artifacts/html-other/preview",
    )
    docx = _tool_call(
        "run_sandbox_skill",
        artifact_id="docx-1",
        title="Q1 Sales Report",
        type="docx",
        preview_url="/api/artifacts/docx-1/preview",
        preview_artifact_id="html-sidecar",
    )
    result = _collect([html_sidecar, html_other, docx])
    # Only the sidecar is consumed; html-other stays
    assert len(result) == 2
    by_id = {a["artifact_id"]: a for a in result}
    assert "html-sidecar" not in by_id  # consumed
    assert "html-other" in by_id         # untouched
    assert "docx-1" in by_id
    # The docx points at its sidecar, not the unrelated html-other
    assert by_id["docx-1"]["preview_artifact_id"] == "html-sidecar"


# ---------------------------------------------------------------------------
# Fuzzy title matching — handles file-name titles with extension/underscores
# ---------------------------------------------------------------------------


def test_dedup_with_filename_title_underscored_and_extension():
    """The real-world failure mode: docx title is the auto-generated
    file name (with underscores + extension) while the HTML title is
    the human-readable form.  Both must dedup into one card."""
    html = _tool_call(
        "create_artifact",
        artifact_id="html-real",
        title="Address Distribution Report by Region",
        type="html_report",
        preview_url="/api/artifacts/html-real/preview",
    )
    docx = _tool_call(
        "create_artifact",
        artifact_id="docx-real",
        title="Address_Distribution_Report.docx",
        type="docx",
        preview_url="/api/artifacts/docx-real/preview",
    )
    result = _collect([html, docx])
    # Single card after dedup
    assert len(result) == 1
    assert result[0]["type"] == "docx"
    assert result[0]["artifact_id"] == "docx-real"
    # The HTML is now the sidecar preview for the docx
    assert result[0]["preview_artifact_id"] == "html-real"
    assert result[0]["preview_url"] == "/api/artifacts/html-real/preview"


def test_dedup_strips_various_extensions():
    """Any of (.docx, .pptx, .xlsx, .pdf, .html) should be stripped."""
    base = "Quarterly Report"
    cases = [
        (f"{base}.docx", f"{base}", "docx"),
        (f"{base}_backup.pptx", f"{base} backup", "pptx"),
        (f"{base}.html", f"{base} Region", "html"),  # HTML sibling
    ]
    # html needs a matching html_report sibling; build it
    html_title, docx_title, fmt = cases[0]
    html = _tool_call(
        "create_artifact",
        artifact_id="h",
        title=html_title,
        type="html_report",
        preview_url="/api/h/preview",
    )
    docx = _tool_call(
        "create_artifact",
        artifact_id="d",
        title=docx_title,
        type=fmt,
        preview_url="/api/d/preview",
    )
    result = _collect([html, docx])
    assert len(result) == 1
    assert result[0]["artifact_id"] == "d"


def test_dedup_strips_preview_marker():
    """Sidecar titles often end in ' (preview)'; that must not break the match."""
    html = _tool_call(
        "create_artifact",
        artifact_id="h2",
        title="Sales Report (preview)",
        type="html",
        preview_url="/api/h2/preview",
    )
    docx = _tool_call(
        "create_artifact",
        artifact_id="d2",
        title="Sales Report",
        type="docx",
        preview_url="/api/d2/preview",
    )
    result = _collect([html, docx])
    assert len(result) == 1
    assert result[0]["artifact_id"] == "d2"


def test_no_false_positive_dedup_for_short_overlap():
    """A 1-word common prefix (e.g. 'Q1') is too weak to dedup."""
    html = _tool_call(
        "create_artifact",
        artifact_id="h3",
        title="Q1 Revenue Analysis",
        type="html_report",
        preview_url="/api/h3/preview",
    )
    docx = _tool_call(
        "create_artifact",
        artifact_id="d3",
        title="Q1 Marketing Plan",
        type="docx",
        preview_url="/api/d3/preview",
    )
    result = _collect([html, docx])
    # Different second word → must NOT dedup
    assert len(result) == 2


def test_dedup_with_diacritics_and_underscores():
    """Accented chars and underscores in title should normalize cleanly."""
    html = _tool_call(
        "create_artifact",
        artifact_id="h4",
        title="Café Report — Q3 Numbers",
        type="html",
        preview_url="/api/h4/preview",
    )
    docx = _tool_call(
        "create_artifact",
        artifact_id="d4",
        title="Cafe_Report_Q3.docx",
        type="docx",
        preview_url="/api/d4/preview",
    )
    result = _collect([html, docx])
    # 'cafe report q3' (normalized) vs 'cafe report q3 numbers' — 3-word
    # common prefix → dedup.
    assert len(result) == 1
    assert result[0]["artifact_id"] == "d4"


def test_rich_html_report_preferred_over_sparse_sandbox_sidecar():
    """The user's exact case: agent called ``finalize_into_artifact``
    (rich ``html_report``) and then ``run_sandbox_skill`` (which
    creates its own sparse ``html`` sidecar).  The docx preview
    must use the RICH ``html_report``, not the sparse sandbox
    sidecar."""
    tcs = [
        # The rich report card from finalize_into_artifact
        _tool_call(
            "create_artifact",
            artifact_id="html-rich", version_id="v1",
            title="Sales Report - Address Distribution by Region",
            type="html_report",
            preview_url="/p/html-rich",
        ),
        # The sandbox sidecar (sparse, generated by sandbox_tool)
        _tool_call(
            "run_sandbox_skill",
            artifact_id="html-sparse-sidecar", version_id="v2",
            title="Sales Report - Address Distribution by Region (preview)",
            type="html",
            preview_url="/p/html-sparse",
        ),
        # The docx file (from sandbox_runner) — its explicit sidecar
        # points to the sparse sidecar, but the dedup should upgrade
        # it to the rich html_report.
        _tool_call(
            "run_sandbox_skill",
            artifact_id="docx-file", version_id="v3",
            title="Sales_Report_Address_Distribution_by_Region.docx",
            type="docx",
            preview_url="/p/docx",
            preview_artifact_id="html-sparse-sidecar",
        ),
    ]
    out = _collect(tcs)
    # Only the docx card should remain.
    assert len(out) == 1
    docx = out[0]
    assert docx["type"] == "docx"
    assert docx["artifact_id"] == "docx-file"
    # The preview is the RICH html_report, not the sparse sidecar.
    assert docx["preview_artifact_id"] == "html-rich"
    assert docx["preview_url"] == "/p/html-rich"


def test_explicit_sidecar_used_when_no_rich_html_report_exists():
    """When there's no rich ``html_report`` in the same turn, the
    explicit ``preview_artifact_id`` on the file-format artifact
    still wins (e.g. the agent only called ``run_sandbox_skill``,
    which produced both the docx and its own sidecar)."""
    tcs = [
        _tool_call(
            "run_sandbox_skill",
            artifact_id="html-sidecar", version_id="v2",
            title="Quarterly Revenue Report (preview)",
            type="html",
            preview_url="/p/sidecar",
        ),
        _tool_call(
            "run_sandbox_skill",
            artifact_id="docx-file", version_id="v3",
            title="Quarterly Revenue Report",
            type="docx",
            preview_url="/p/docx",
            preview_artifact_id="html-sidecar",
        ),
    ]
    out = _collect(tcs)
    assert len(out) == 1
    assert out[0]["type"] == "docx"
    assert out[0]["preview_artifact_id"] == "html-sidecar"
    assert out[0]["preview_url"] == "/p/sidecar"
