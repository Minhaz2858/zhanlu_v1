"""Task 10: docx SKILL.md must contain a 'Choosing a Document Type' section
with a 4-question rubric, 5-type table (memo/report/letter/proposal/minutes),
a structural template per type, and type-mismatch warnings.
"""
from pathlib import Path
import re

DOCX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "docx" / "SKILL.md"


def _read() -> str:
    return DOCX_SKILL.read_text(encoding="utf-8")


def _section(name: str) -> str:
    text = _read()
    m = re.search(rf"^## {re.escape(name)}$.*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else ""


def test_doctype_section_exists():
    text = _read()
    assert re.search(r"^## Choosing a Document Type$", text, re.MULTILINE), (
        "docx SKILL.md must contain `## Choosing a Document Type`"
    )


def test_doctype_section_comes_before_creating_new_documents():
    text = _read()
    dt = re.search(r"^## Choosing a Document Type$", text, re.MULTILINE)
    cnd = re.search(r"^## Creating New Documents$", text, re.MULTILINE)
    assert dt and cnd, "section markers missing"
    assert dt.start() < cnd.start(), (
        "Choosing a Document Type must come BEFORE Creating New Documents"
    )


def test_doctype_section_has_four_question_rubric():
    body = _section("Choosing a Document Type")
    # 4 numbered questions
    points = re.findall(r"^\s*(\d+)\.\s+\*\*", body, re.MULTILINE)
    in_range = sorted({int(p) for p in points if 1 <= int(p) <= 4})
    assert in_range == [1, 2, 3, 4], (
        f"Document-type rubric must have 4 questions. Found: {in_range}"
    )


def test_doctype_table_lists_all_five_types():
    body = _section("Choosing a Document Type")
    for doc_type in ["Memo", "Report", "Letter", "Proposal", "Minutes"]:
        assert doc_type in body, f"Document-type table must list {doc_type!r}"


def test_doctype_section_has_per_type_templates():
    body = _section("Choosing a Document Type")
    # Each type should have a `#### <Type> template` subsection
    for doc_type in ["Memo", "Report", "Letter", "Proposal", "Minutes"]:
        assert re.search(rf"^#### {doc_type} template", body, re.MULTILINE), (
            f"Each document type must have a '#### {doc_type} template' subsection"
        )


def test_doctype_section_has_type_mismatch_warnings():
    body = _section("Choosing a Document Type").lower()
    assert "type-mismatch" in body or "mismatch" in body, (
        "Document-type section must have type-mismatch warnings"
    )
    # At least 3 specific call-outs
    matches = sum(1 for w in ["status update", "pitch deck", "newsletter", "contract", "resume", "cv"] if w in body)
    assert matches >= 3, f"Type-mismatch warnings must list ≥3 specific examples, found {matches}"


def test_doctype_section_advises_asking_when_unsure():
    body = _section("Choosing a Document Type").lower()
    assert "ask" in body, "Document-type section must advise the agent to ask when unsure"
