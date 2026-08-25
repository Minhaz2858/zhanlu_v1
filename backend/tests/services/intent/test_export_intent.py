import pytest
from app.services.intent.export_intent import detect_export_intent


@pytest.mark.parametrize("msg,expected_format", [
    ("give me in docx", "docx"),
    ("export as PDF", "pdf"),
    ("now as PowerPoint", "pptx"),
    ("make a Word file", "docx"),
    ("in Word", "docx"),
    ("docx", "docx"),
    ("in pdf", "pdf"),
    ("as xlsx", "xlsx"),
])
def test_strong_patterns(msg, expected_format):
    intent = detect_export_intent(msg)
    assert intent is not None
    assert intent.confidence == "strong"
    assert intent.format == expected_format


@pytest.mark.parametrize("msg", [
    "export the same", "download it", "save as that",
    "re-export the report", "reformat it",
])
def test_weak_patterns(msg):
    intent = detect_export_intent(msg)
    assert intent is not None
    assert intent.confidence == "weak"
    assert intent.format is None


@pytest.mark.parametrize("msg", [
    "give me Contract Performance for last month",
    "show me the analysis", "regenerate the report", "hi",
    "what is the margin?",
])
def test_no_export_intent(msg):
    assert detect_export_intent(msg) is None
