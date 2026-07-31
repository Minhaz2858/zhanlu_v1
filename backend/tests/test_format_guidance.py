"""Report-structure guidance is injected for prose formats only."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def test_prose_formats_get_report_structure():
    for fmt in ("html", "docx", "pdf", "md", ""):
        guidance = ax._format_guidance(fmt)
        assert "Executive summary" in guidance, fmt
        assert "Key metrics" in guidance
        assert "one-page summary" in guidance
        assert "Never narrate tool calls" in guidance


def test_rigid_formats_unchanged():
    for fmt in ("pptx", "xlsx", "csv", "json"):
        guidance = ax._format_guidance(fmt)
        assert "Executive summary" not in guidance, fmt
