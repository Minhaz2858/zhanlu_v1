"""Format guidance + honesty guardrail for the automation run prompt."""
import os, sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def test_pptx_guidance_mentions_slides():
    from app.services.automation_executor import _format_guidance
    g = _format_guidance("pptx").lower()
    assert "slide" in g and "##" in g


def test_docx_guidance_mentions_sections_and_tables():
    from app.services.automation_executor import _format_guidance
    g = _format_guidance("docx").lower()
    assert "##" in g and "table" in g


def test_unknown_format_falls_back_to_html_guidance():
    from app.services.automation_executor import _format_guidance
    assert _format_guidance("weird") == _format_guidance("html")
    assert _format_guidance(None) == _format_guidance("html")


def test_guardrail_forbids_fabrication():
    from app.services.automation_executor import _HONESTY_GUARDRAIL
    g = _HONESTY_GUARDRAIL.lower()
    assert "never fabricate" in g
    assert "tools actually returned" in g


def test_prompt_builder_wires_guidance_and_guardrail():
    """Source-level: the prompt assembly must include both pieces."""
    import inspect
    from app.services import automation_executor as ax
    src = inspect.getsource(ax.execute_automation)
    assert "_format_guidance(" in src
    assert "_HONESTY_GUARDRAIL" in src
    assert "output_format" in src
