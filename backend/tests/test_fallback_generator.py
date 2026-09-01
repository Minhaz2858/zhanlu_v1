"""Unit tests for the deterministic fallback generators.

These verify that ``fallback_generator.generate_*_fallback`` produces a
valid file for every supported format given a minimal config + data.
The tests don't need Docker — they call the generators directly in the
test process.

The fallback is the safety net for the C-Heavy skill-driven runner; if
these tests fail, the user will get NOTHING when the LLM path is down.
"""
from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.sandbox.fallback_generator import (
    _meta_from_config,
    _rows_from_data,
    generate_docx_fallback,
    generate_html_utility,
    generate_md_utility,
    generate_pdf_fallback,
    generate_pptx_fallback,
    generate_xlsx_fallback,
)


# ── Shared test fixtures ─────────────────────────────────────────────────

SAMPLE_CONFIG = {
    "title": "Q3 Sales Report",
    "source": "SalesForce + ERP",
    "summary": "Q3 delivered 12.3% YoY growth across all regions with APAC outperforming.",
    "methodology": "Data sourced from SalesForce CRM and ERP Fusion. Period: Jul 1 - Sep 30.",
    "kpis": [
        {"label": "Revenue", "value": "$847M", "delta": "+12.3% YoY"},
        {"label": "Margin", "value": "58.2%", "delta": "+210 bps"},
    ],
    "key_findings": [
        "APAC growth fueled by three semiconductor mega-deals.",
        "NA enterprise saw 15% increase in multi-year contract renewals.",
    ],
    "recommendations": [
        "Invest $2M in EMEA sales enablement.",
        "Launch APAC semiconductor playbook for other regional teams.",
    ],
    "next_step": "Schedule Q4 OKR review by Oct 15.",
    "sql": "SELECT region, SUM(revenue) FROM sales GROUP BY region",
}

SAMPLE_DATA = [
    {"region": "NA", "revenue": 398},
    {"region": "APAC", "revenue": 221},
    {"region": "EMEA", "revenue": 178},
    {"region": "LATAM", "revenue": 50},
]


# ── DOCX fallback ────────────────────────────────────────────────────────


def test_docx_fallback_produces_valid_file(tmp_path):
    """The DOCX output should be a real OOXML zip containing document.xml."""
    out = tmp_path / "report.docx"
    generate_docx_fallback(output_path=out, config=SAMPLE_CONFIG, data=SAMPLE_DATA)
    assert out.exists()
    assert out.stat().st_size > 1000, "docx unexpectedly small"
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "word/document.xml" in names
        # Verify the title made it into the document body
        body = zf.read("word/document.xml").decode("utf-8", errors="ignore")
        assert "Q3 Sales Report" in body


def test_docx_fallback_handles_empty_data(tmp_path):
    """An empty data list should not crash; the file should still be valid."""
    out = tmp_path / "report.docx"
    generate_docx_fallback(output_path=out, config=SAMPLE_CONFIG, data=[])
    assert out.exists()
    assert out.stat().st_size > 500


def test_docx_fallback_handles_missing_summary(tmp_path):
    """Optional fields like summary/methodology can be missing."""
    minimal = {"title": "Quick Note"}
    out = tmp_path / "report.docx"
    generate_docx_fallback(output_path=out, config=minimal, data=SAMPLE_DATA)
    assert out.exists()


# ── PPTX fallback ────────────────────────────────────────────────────────


def test_pptx_fallback_produces_valid_file(tmp_path):
    """The PPTX output should be a real OOXML zip containing slide XML."""
    out = tmp_path / "report.pptx"
    generate_pptx_fallback(output_path=out, config=SAMPLE_CONFIG, data=SAMPLE_DATA)
    assert out.exists()
    assert out.stat().st_size > 1000
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert any(n.startswith("ppt/slides/slide") for n in names)
        assert any(n.startswith("ppt/presentation.xml") for n in names)


def test_pptx_fallback_handles_minimal_config(tmp_path):
    """Only title required."""
    out = tmp_path / "report.pptx"
    generate_pptx_fallback(output_path=out, config={"title": "Hello"}, data=[])
    assert out.exists()


# ── XLSX fallback ────────────────────────────────────────────────────────


def test_xlsx_fallback_produces_valid_file(tmp_path):
    """The XLSX output should be a real OOXML zip containing sheet XML."""
    out = tmp_path / "report.xlsx"
    generate_xlsx_fallback(output_path=out, config=SAMPLE_CONFIG, data=SAMPLE_DATA)
    assert out.exists()
    assert out.stat().st_size > 1000
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert any(n.startswith("xl/worksheets/sheet") for n in names)


def test_xlsx_fallback_includes_summary_sheet(tmp_path):
    """When summary content is present, a second 'Summary' sheet should appear."""
    out = tmp_path / "report.xlsx"
    generate_xlsx_fallback(output_path=out, config=SAMPLE_CONFIG, data=SAMPLE_DATA)
    with zipfile.ZipFile(out) as zf:
        # The workbook.xml lists all sheets; check it has at least 2.
        wb_xml = zf.read("xl/workbook.xml").decode("utf-8", errors="ignore")
        # Count <sheet> entries
        assert wb_xml.count("<sheet ") >= 2


# ── PDF fallback ─────────────────────────────────────────────────────────


def test_pdf_fallback_produces_valid_pdf(tmp_path):
    """The PDF should start with %PDF- and end with %%EOF."""
    out = tmp_path / "report.pdf"
    generate_pdf_fallback(output_path=out, config=SAMPLE_CONFIG, data=SAMPLE_DATA)
    assert out.exists()
    head = out.read_bytes()[:8]
    tail = out.read_bytes()[-32:]
    assert head.startswith(b"%PDF-"), f"bad PDF header: {head!r}"
    assert b"%%EOF" in tail, f"bad PDF trailer: {tail!r}"


# ── HTML utility ─────────────────────────────────────────────────────────


def test_html_utility_contains_title_and_table(tmp_path):
    """The HTML output should include the title and a data table."""
    out = tmp_path / "report.html"
    generate_html_utility(output_path=out, config=SAMPLE_CONFIG, data=SAMPLE_DATA)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "Q3 Sales Report" in text
    assert "<table" in text
    assert "APAC" in text  # from sample data


def test_html_utility_escapes_html_in_content(tmp_path):
    """User-supplied content must be HTML-escaped to prevent injection."""
    out = tmp_path / "report.html"
    cfg = {"title": "<script>alert('xss')</script>", "summary": "<b>bold</b>"}
    generate_html_utility(output_path=out, config=cfg, data=[])
    text = out.read_text(encoding="utf-8")
    # The raw <script> tag should NOT appear in the output
    assert "<script>alert" not in text
    # It should be escaped
    assert "&lt;script&gt;" in text


# ── Markdown utility ─────────────────────────────────────────────────────


def test_md_utility_has_title_and_table(tmp_path):
    """The Markdown output should have a title heading and a pipe table."""
    out = tmp_path / "report.md"
    generate_md_utility(output_path=out, config=SAMPLE_CONFIG, data=SAMPLE_DATA)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Q3 Sales Report")
    assert "| region | revenue |" in text
    assert "| APAC |" in text


def test_md_utility_escapes_pipe_in_cells(tmp_path):
    """Pipe characters in cells would break the table; they must be escaped."""
    out = tmp_path / "report.md"
    generate_md_utility(
        output_path=out,
        config=SAMPLE_CONFIG,
        data=[{"region": "NA|EU", "revenue": 100}],
    )
    text = out.read_text(encoding="utf-8")
    assert "NA\\|EU" in text


# ── Helper function unit tests ──────────────────────────────────────────


def test_rows_from_data_normalizes():
    """_rows_from_data should handle list-of-dicts, single dict, and other shapes."""
    assert _rows_from_data([{"a": 1}]) == [{"a": 1}]
    assert _rows_from_data({"a": 1}) == [{"a": 1}]
    assert _rows_from_data("garbage") == []
    assert _rows_from_data([{"a": 1}, "bad", {"b": 2}]) == [{"a": 1}, {"b": 2}]


def test_meta_from_config_returns_defaults_for_missing():
    """_meta_from_config must never crash on missing fields."""
    meta = _meta_from_config({})
    assert meta["title"] == "Report"
    assert meta["summary"] == ""
    assert meta["key_findings"] == []
    assert meta["kpis"] == []