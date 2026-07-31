"""market_dashboard models — product→table map + autoload helper."""
from app.models import market_dashboard as m


def test_product_table_map_has_six_known_products():
    assert set(m.PRODUCT_TABLE_MAP.keys()) == {
        "naphtha", "mixed_c5", "isoprene", "piperylene", "dcpd", "sis_rubber"
    }


def test_product_table_map_shape():
    cfg = m.PRODUCT_TABLE_MAP["naphtha"]
    assert cfg["table"] == "裂解C5"
    assert cfg["value_col"] == "日本石脑油Japanese naphtha"
    assert "label_en" in cfg and "label_zh" in cfg


def test_list_known_product_ids_includes_mapping():
    ids = set(m.list_known_product_ids())
    assert "naphtha" in ids
    assert "isoprene" in ids


def test_get_product_table_returns_none_without_engine(monkeypatch):
    monkeypatch.setattr("app.models.market_dashboard.get_mysql_engine", lambda: None)
    assert m.get_product_table("naphtha") is None
