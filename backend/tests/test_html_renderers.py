"""
Test the HTML → DOCX and HTML → PDF renderers for byte validity.

Exercises:
  - render_html_to_docx produces valid ZIP (DOCX is a ZIP)
  - render_html_to_pdf produces valid PDF (starts with %PDF)
  - Graceful fallback when pandoc/weasyprint unavailable
"""

import pytest
import io
import zipfile
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


SAMPLE_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Test Report</title></head>
<body>
<h1>Test Report</h1>
<p>This is a sample report generated for testing purposes.</p>
<h2>Section 1</h2>
<p>Content for section one with <strong>bold text</strong> and <em>italics</em>.</p>
<h3>Subsection 1.1</h3>
<ul>
  <li>Item one</li>
  <li>Item two</li>
  <li>Item three</li>
</ul>
<h2>Section 2</h2>
<p>Another section with a table:</p>
<table>
  <thead><tr><th>Name</th><th>Value</th></tr></thead>
  <tbody>
    <tr><td>Alpha</td><td>100</td></tr>
    <tr><td>Beta</td><td>200</td></tr>
    <tr><td>Gamma</td><td>300</td></tr>
  </tbody>
</table>
<p>End of report.</p>
</body>
</html>
"""


# -- HTML → DOCX tests --


def test_html_to_docx_produces_valid_zip():
    """render_html_to_docx output is a valid ZIP (DOCX is ZIP-based)."""
    from app.services.artifacts.exporters.html_docx import render_html_to_docx

    result = render_html_to_docx(SAMPLE_HTML)
    assert result is not None
    assert len(result) > 0

    # DOCX files are ZIP archives
    buf = io.BytesIO(result)
    assert zipfile.is_zipfile(buf), "DOCX output is not a valid ZIP file"

    # Verify expected DOCX structure
    with zipfile.ZipFile(buf) as z:
        names = z.namelist()
        assert "[Content_Types].xml" in names, "Missing [Content_Types].xml"
        assert any("word/document" in n for n in names), "Missing word/document.xml"


def test_html_to_docx_handles_minimal_html():
    """render_html_to_docx handles minimal HTML gracefully."""
    from app.services.artifacts.exporters.html_docx import render_html_to_docx

    result = render_html_to_docx(b"<p>Hello World</p>")
    assert result is not None
    assert len(result) > 0


def test_html_to_docx_python_docx_fallback():
    """When pandoc is unavailable, python-docx fallback still produces a ZIP."""
    # This test exercises the fallback path. The renderer tries pandoc first,
    # then falls back to python-docx. We don't mock — if pandoc works, we
    # still verify the output is valid.
    from app.services.artifacts.exporters.html_docx import render_html_to_docx
    from app.services.artifacts.exporters.html_docx import _via_python_docx

    # Directly test the python-docx fallback
    result = _via_python_docx(SAMPLE_HTML)
    assert result is not None
    assert len(result) > 0
    buf = io.BytesIO(result)
    assert zipfile.is_zipfile(buf), "python-docx fallback did not produce a valid ZIP"


# -- HTML → PDF tests --


def test_html_to_pdf_produces_valid_pdf():
    """render_html_to_pdf output starts with %PDF magic bytes."""
    from app.services.artifacts.exporters.html_pdf import render_html_to_pdf

    try:
        result = render_html_to_pdf(SAMPLE_HTML)
    except RuntimeError as exc:
        pytest.skip(f"Neither weasyprint nor LibreOffice available: {exc}")

    assert result is not None
    assert len(result) > 0
    assert result.startswith(b"%PDF"), (
        f"PDF output does not start with '%PDF', got: {result[:20]!r}"
    )


def test_html_to_pdf_handles_minimal_html():
    """render_html_to_pdf handles minimal HTML."""
    from app.services.artifacts.exporters.html_pdf import render_html_to_pdf

    try:
        result = render_html_to_pdf(b"<p>Hello</p>")
    except RuntimeError as exc:
        pytest.skip(f"Neither weasyprint nor LibreOffice available: {exc}")

    assert result is not None
    assert len(result) > 0


# -- Content preservation tests --


def test_html_to_docx_preserves_content():
    """DOCX output contains key terms from the source HTML."""
    from app.services.artifacts.exporters.html_docx import render_html_to_docx

    result = render_html_to_docx(SAMPLE_HTML)

    # DOCX is ZIP with XML inside; search raw bytes for text
    text_content = result.decode("utf-8", errors="replace").lower()
    assert "test report" in text_content or "Test Report" in result.decode("latin-1", errors="replace")


def test_html_to_docx_handles_empty_html():
    """Empty HTML input still produces a minimal valid DOCX."""
    from app.services.artifacts.exporters.html_docx import render_html_to_docx

    result = render_html_to_docx(b"")
    assert result is not None
    assert len(result) > 0
    buf = io.BytesIO(result)
    assert zipfile.is_zipfile(buf)
