"""Verify docx SKILL.md documents XSD validation behavior + pack.py wires it."""
import os


def test_docx_skill_documents_xsd_validation():
    p = os.path.join(
        os.path.dirname(__file__),
        "..", "skills", "docx", "SKILL.md",
    )
    with open(p) as f:
        content = f.read()
    assert "XSD Validation" in content
    assert "validate.py" in content or "validators/" in content
    # The example error output
    assert "FAILED" in content


def test_docx_pack_py_calls_xsd_validation():
    """pack.py must call the XSD validators — the SKILL.md claims this is
    the default, and we want a contract test so it doesn't regress."""
    p = os.path.join(
        os.path.dirname(__file__),
        "..", "skills", "docx", "scripts", "office", "pack.py",
    )
    with open(p) as f:
        content = f.read()
    assert "validate" in content
    # Default validate=True in the function signature
    assert "validate: bool = True" in content or "validate=True" in content
    # Validators are actually iterated
    assert "v.validate()" in content or ".validate()" in content
