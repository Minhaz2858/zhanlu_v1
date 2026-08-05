"""Verify that seed config wires up lz_v_* extra_sources for all eligible products."""
from app.services.forecasting.seed_ecisco_targets import ECISCO_FORECAST_TARGETS

_LZ_V_MAPPING = {
    "ecisco.cracked_c5": "lz_v_裂解c5_data",
    "ecisco.cracked_c9": "lz_v_裂解c9_data",
    "ecisco.piperylene": "lz_v_间戊二烯_data",
    "ecisco.dcpd": "lz_v_双环戊二烯_data",
    "ecisco.sis": "lz_v_sis_data",
    "ecisco.blowing_agent": "lz_v_戊烷泡发剂_data",
    "ecisco.isoprene": "lz_v_异戊二烯_data",  # already wired
    "ecisco.styrene": "lz_v_苯乙烯_data",      # already wired
}


def test_all_eligible_products_have_lz_v_extra_sources():
    """Every product with an available lz_v_* view must have extra_sources."""
    targets = {t["product_key"]: t for t in ECISCO_FORECAST_TARGETS}
    for pk, expected_table in _LZ_V_MAPPING.items():
        assert pk in targets, f"{pk} not in seed config"
        ds = targets[pk]["datasource"]
        assert "extra_sources" in ds, f"{pk} missing extra_sources"
        tables = [s["table"] for s in ds["extra_sources"]]
        assert expected_table in tables, f"{pk} missing {expected_table}, has {tables}"


def test_extra_sources_have_required_fields():
    """Each extra_source must have table, time_col, measure."""
    for t in ECISCO_FORECAST_TARGETS:
        for src in t["datasource"].get("extra_sources", []):
            assert all(k in src for k in ("table", "time_col", "measure")), \
                f"extra_source missing field in {t['product_key']}: {src}"
