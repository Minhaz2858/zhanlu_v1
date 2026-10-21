"""BOM normalization + manufacturing notes (roadmap P4)."""
import pytest

from app.services.dfm.bom import mfg_notes, normalize_bom_rows


def _row(**over):
    r = {"name": "Body", "quantity": 1, "material": "6061-t6", "mass_g": 400.0}
    r.update(over)
    return r


def test_rows_get_line_numbers_and_totals():
    out = normalize_bom_rows([_row(), _row(name="Lid", mass_g=100.0)], "cnc")
    assert [r["line_no"] for r in out["rows"]] == [1, 2]
    assert out["totals"]["line_count"] == 2
    assert out["totals"]["total_quantity"] == 2
    assert out["totals"]["total_mass_g"] == pytest.approx(500.0)
    assert out["warnings"] == []


def test_duplicate_rows_aggregate():
    out = normalize_bom_rows([_row(), _row(), _row(mass_g=None)], "cnc")
    assert out["totals"]["line_count"] == 1
    assert out["rows"][0]["quantity"] == 3
    # mass: two rows have mass, one missing → sum known masses
    assert out["rows"][0]["mass_g"] == pytest.approx(800.0)


def test_missing_material_warns():
    out = normalize_bom_rows([_row(material=None)], "cnc")
    assert any("material" in w.lower() for w in out["warnings"])


def test_missing_mass_warns():
    out = normalize_bom_rows([_row(mass_g=None)], "cnc")
    assert any("mass" in w.lower() for w in out["warnings"])


def test_invalid_row_rejected():
    with pytest.raises(ValueError):
        normalize_bom_rows([{"quantity": 1}], "cnc")  # no name
    with pytest.raises(ValueError):
        normalize_bom_rows([_row(quantity=0)], "cnc")


def test_mfg_notes_cnc():
    notes = mfg_notes("cnc", {"holes": 4, "pockets": 1}, None)
    joined = "\n".join(notes)
    assert "fixture" in joined.lower()
    assert "anodiz" in joined.lower() or "finish" in joined.lower()


def test_mfg_notes_sheet_metal_mentions_bends():
    notes = mfg_notes("sheet_metal", {"bends": 4}, None)
    assert any("bend" in n.lower() for n in notes)


def test_mfg_notes_lists_outstanding_dfm_repairs():
    dfm = {"verdict": "fail", "issues": [
        {"rule": "min_wall_thickness", "severity": "error", "measured": "0.4mm",
         "required": "1.0mm", "reason": "too thin",
         "repair": {"option": "increase_wall", "value_mm": 1.0}}]}
    notes = mfg_notes("cnc", {}, dfm)
    assert any("min_wall_thickness" in n for n in notes)
