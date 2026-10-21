"""Deterministic quote calculator (roadmap P2) — no LLM, no hidden state.

Cost model (explicit, assumption-listed):
  material  = mass_g/1000 × cost_per_kg × waste_factor(1.3)
  machining = (base_min + Σ feature_min) / 60 × hourly_rate
  setup     = setup_cost / quantity
  unit      = material + machining + setup
  total     = unit × quantity, quoted as ±20% range
"""
import pytest

from app.services.dfm.quote import (
    CATALOG,
    compute_quote,
    estimate_mass_g,
)


# ── catalog ───────────────────────────────────────────────────────────────

def test_catalog_has_material_costs_and_process_rates():
    assert CATALOG["material_cost_per_kg"]["6061-t6"] > 0
    assert CATALOG["process"]["cnc"]["rate_per_hr"] > 0
    assert CATALOG["process"]["sheet_metal"]["setup_cost"] > 0


# ── mass estimation ───────────────────────────────────────────────────────

def test_estimate_mass_from_volume_and_material():
    # 100 cm³ of 6061 (2.70 g/cm³) = 270 g
    assert estimate_mass_g(volume_cm3=100.0, material="6061-t6") == pytest.approx(270.0)


def test_estimate_mass_direct_pass_through():
    assert estimate_mass_g(mass_g=500.0) == pytest.approx(500.0)


def test_estimate_mass_requires_input():
    with pytest.raises(ValueError):
        estimate_mass_g()


# ── quote math ────────────────────────────────────────────────────────────

def _base_req(**over):
    req = {
        "part_name": "Enclosure",
        "material": "6061-t6",
        "process": "cnc",
        "quantity": 10,
        "mass_g": 500.0,
    }
    req.update(over)
    return req


def test_quote_basic_math():
    q = compute_quote(_base_req())
    c = CATALOG
    mat_unit = 0.5 * c["material_cost_per_kg"]["6061-t6"] * c["waste_factor"]
    minutes = c["process"]["cnc"]["base_minutes"]  # no features
    mach_unit = minutes / 60 * c["process"]["cnc"]["rate_per_hr"]
    setup_unit = c["process"]["cnc"]["setup_cost"] / 10
    unit = mat_unit + mach_unit + setup_unit
    assert q["unit_cost"] == pytest.approx(unit, rel=1e-6)
    assert q["total_mid"] == pytest.approx(unit * 10, rel=1e-6)
    assert q["total_low"] == pytest.approx(unit * 10 * 0.8, rel=1e-6)
    assert q["total_high"] == pytest.approx(unit * 10 * 1.2, rel=1e-6)
    assert q["currency"] == "USD"


def test_quote_features_add_machining_time():
    q0 = compute_quote(_base_req())
    q1 = compute_quote(_base_req(feature_counts={"holes": 4}))
    per_hole = CATALOG["feature_minutes"]["cnc"]["holes"]
    assert q1["unit_cost"] == pytest.approx(
        q0["unit_cost"] + 4 * per_hole / 60 * CATALOG["process"]["cnc"]["rate_per_hr"],
        rel=1e-6,
    )


def test_quote_quantity_amortizes_setup():
    q10 = compute_quote(_base_req(quantity=10))
    q100 = compute_quote(_base_req(quantity=100))
    # material + machining identical per unit; setup share drops 10x
    setup = CATALOG["process"]["cnc"]["setup_cost"]
    assert q100["unit_cost"] == pytest.approx(
        q10["unit_cost"] - setup / 10 + setup / 100, rel=1e-6
    )
    assert q100["total_mid"] > q10["total_mid"]  # more parts, more total


def test_quote_sheet_metal_uses_bends_and_setup():
    q = compute_quote(_base_req(process="sheet_metal", material="5052-h32",
                                feature_counts={"bends": 4}))
    c = CATALOG
    mat_unit = 0.5 * c["material_cost_per_kg"]["5052-h32"] * c["waste_factor"]
    minutes = (c["process"]["sheet_metal"]["base_minutes"]
               + 4 * c["feature_minutes"]["sheet_metal"]["bends"])
    mach_unit = minutes / 60 * c["process"]["sheet_metal"]["rate_per_hr"]
    assert q["unit_cost"] == pytest.approx(
        mat_unit + mach_unit + c["process"]["sheet_metal"]["setup_cost"] / 10, rel=1e-6)


def test_quote_requires_mass_or_volume():
    with pytest.raises(ValueError):
        compute_quote({k: v for k, v in _base_req().items() if k != "mass_g"})


def test_quote_rejects_quantity_below_one():
    with pytest.raises(ValueError):
        compute_quote(_base_req(quantity=0))


def test_quote_rejects_unknown_process():
    with pytest.raises(ValueError):
        compute_quote(_base_req(process="waterjet"))


def test_quote_unknown_material_maps_to_generic_cost():
    q = compute_quote(_base_req(material="unobtanium"))
    assert q["material"] == "generic"


def test_quote_assumptions_always_listed():
    q = compute_quote(_base_req())
    assert isinstance(q["assumptions"], list) and len(q["assumptions"]) >= 3
    assert any("estimate" in a.lower() for a in q["assumptions"])
