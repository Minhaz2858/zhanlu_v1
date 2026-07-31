"""market_dashboard models — product→table map + autoload helper."""
from app.models import market_dashboard as m


def test_product_table_map_has_known_products():
    keys = set(m.PRODUCT_TABLE_MAP.keys())
    # Core mapped products
    for pid in ("naphtha", "mixed_c5", "isoprene", "piperylene", "dcpd", "sis_rubber"):
        assert pid in keys, f"{pid} missing from PRODUCT_TABLE_MAP"
    # New mappings added after EDIA schema probe
    for pid in ("crude_oil", "cracked_c5", "sis"):
        assert pid in keys, f"{pid} missing from PRODUCT_TABLE_MAP"


def test_product_table_map_shape():
    cfg = m.PRODUCT_TABLE_MAP["naphtha"]
    assert cfg["table"] == "裂解C5"
    assert cfg["supplier_name"] == "日本石脑油"
    assert "label_en" in cfg and "label_zh" in cfg


def test_list_known_product_ids_includes_mapping():
    ids = set(m.list_known_product_ids())
    assert "naphtha" in ids
    assert "isoprene" in ids


def test_get_product_table_returns_none_without_engine(monkeypatch):
    monkeypatch.setattr("app.models.market_dashboard.get_mysql_engine", lambda: None)
    assert m.get_product_table("naphtha") is None
