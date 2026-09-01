"""P0 relocation guard — zhanlu-owned assets must resolve at their new,
decoupled locations OUTSIDE the skills folder, so a skill-swap can never
break theme resolution or the P0 semantic audit.

Themes live in ``backend/data/themes/``; audit scripts live in
``backend/app/services/artifacts/audits/``. The exporters reference these
via ``_theme._THEMES_DIR`` and ``ExportService._audit_script_for()``.

These assets are zhanlu's own data/tooling (brand themes + QA audit
scripts), NOT skill methodology. Relocating them out of ``backend/skills``
is what decouples them from skill-folder changes.
"""
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_ROOT = _THIS_DIR.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


# ---------------------------------------------------------------------------
# Theme assets
# ---------------------------------------------------------------------------

def test_themes_dir_resolves_and_has_zhanlu_blue():
    from app.services.artifacts.exporters._theme import _THEMES_DIR, load_theme
    assert _THEMES_DIR.exists(), f"themes dir missing: {_THEMES_DIR}"
    assert (_THEMES_DIR / "zhanlu-blue.json").exists(), "zhanlu-blue.json missing"
    # loads with the real brand primary (not a silent fallback)
    t = load_theme("zhanlu-blue")
    assert tuple(t.primary) == (0x25, 0x63, 0xEB)


def test_themes_dir_is_outside_skills_folder():
    from app.services.artifacts.exporters._theme import _THEMES_DIR
    # The whole point of the relocation: theme data must NOT live under
    # .../skills/..., otherwise swapping the skills folder breaks decks.
    assert "skills" not in _THEMES_DIR.parts, (
        f"themes dir still inside the skills folder: {_THEMES_DIR}"
    )


# ---------------------------------------------------------------------------
# Audit scripts
# ---------------------------------------------------------------------------

def test_audit_script_for_pptx_exists():
    from app.services.artifacts.exporters.service import ExportService
    p = ExportService._audit_script_for("pptx")
    assert p is not None, "pptx audit script mapping missing"
    assert p.exists(), f"pptx audit script missing: {p}"


def test_audit_script_for_docx_exists():
    from app.services.artifacts.exporters.service import ExportService
    p = ExportService._audit_script_for("docx")
    assert p is not None, "docx audit script mapping missing"
    assert p.exists(), f"docx audit script missing: {p}"


def test_audit_scripts_outside_skills_folder():
    from app.services.artifacts.exporters.service import ExportService
    for fmt in ("pptx", "docx"):
        p = ExportService._audit_script_for(fmt)
        assert p is not None, f"{fmt} audit mapping missing"
        assert "skills" not in p.parts, (
            f"{fmt} audit script still resolved under skills/: {p}"
        )
