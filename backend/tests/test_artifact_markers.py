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


def test_strip_markers_handles_marker_only_text():
    text = '◤MD_DOCX◤{"filename":"A.docx"}◤END_MD_DOCX◤'
    assert strip_markers(text) == ""


def test_marker_with_whitespace_in_json():
    text = '◤MD_DOCX◤{ "filename" : "A.docx" , "md_path" : "a.md" }◤END_MD_DOCX◤'
    markers = find_markers(text)
    assert len(markers) == 1
    assert markers[0].payload["filename"] == "A.docx"


def test_marker_with_unicode_filename():
    text = '◤MD_DOCX◤{"filename":"报告.docx","md_path":"a.md"}◤END_MD_DOCX◤'
    markers = find_markers(text)
    assert len(markers) == 1
    assert markers[0].payload["filename"] == "报告.docx"


def test_unsupported_kind_is_skipped():
    text = '◤PDF◤{"filename":"a.pdf"}◤END_PDF◤'
    assert find_markers(text) == []


def test_empty_string_inputs():
    assert find_markers("") == []
    assert strip_markers("") == ""


def test_payload_must_be_object():
    text = '◤MD_DOCX◤["a","b"]◤END_MD_DOCX◤'
    assert find_markers(text) == []
