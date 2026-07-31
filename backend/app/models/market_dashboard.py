"""Read-only SQLAlchemy Table objects for EDIA's MySQL warehouse.

Tables are autoloaded on first use — we never call Base.metadata.create_all
and never issue INSERT/UPDATE/DELETE.

EDIA's warehouse uses a unified long-table schema (all tables share the
same column structure). The old EDIA market_db.py used wide-table column
names as value_col — that schema no longer exists. The warehouse now
uses 厂商名supplier_name to distinguish different price series within
the same table (e.g., 裂解C5 table contains naphtha, Brent, Yangzi C5
as different supplier_name values).
"""
import logging
from typing import Optional

from sqlalchemy import MetaData, Table

from app.core.mysql_db import get_mysql_engine

logger = logging.getLogger(__name__)

# Unified EDIA schema column names (all tables share these)
DATE_COL = "日期Time"
PRICE_COL = "价格price"
SUPPLIER_COL = "厂商名supplier_name"
PRODUCT_NAME_COL = "产品名material_name"
UNIT_COL = "单位unit"

# product_id -> {table, supplier_name, label_en, label_zh, group}
# supplier_name is the 厂商名supplier_name value that identifies the
# specific price series within the table.
PRODUCT_TABLE_MAP: dict[str, dict] = {
    "crude_oil": {
        "table": "裂解C5",
        "supplier_name": "布伦特",
        "label_en": "Crude Oil (Brent)",
        "label_zh": "原油(布伦特)",
        "group": "upstream",
    },
    "naphtha": {
        "table": "裂解C5",
        "supplier_name": "日本石脑油",
        "label_en": "Japanese Naphtha",
        "label_zh": "日本石脑油",
        "group": "upstream",
    },
    "cracked_c5": {
        "table": "裂解C5",
        "supplier_name": "裂解C5均价",
        "label_en": "Cracked C5 (Avg)",
        "label_zh": "裂解碳五(均价)",
        "group": "upstream",
    },
    "mixed_c5": {
        "table": "裂解C5",
        "supplier_name": "扬子裂解C5",
        "label_en": "Yangzi Cracked C5",
        "label_zh": "扬子裂解C5",
        "group": "upstream",
    },
    "isoprene": {
        "table": "异戊二烯",
        "supplier_name": "华东市场价",
        "label_en": "Isoprene (East China)",
        "label_zh": "异戊二烯",
        "group": "midstream",
    },
    "piperylene": {
        "table": "间戊二烯",
        "supplier_name": "华东市场价",
        "label_en": "Piperylene (East China)",
        "label_zh": "间戊二烯",
        "group": "midstream",
    },
    "dcpd": {
        "table": "双环戊二烯",
        "supplier_name": "华东市场价",
        "label_en": "DCPD (East China)",
        "label_zh": "双环戊二烯",
        "group": "midstream",
    },
    "sis_rubber": {
        "table": "SIS",
        "supplier_name": "巴陵1105",
        "label_en": "SIS Rubber (Baling 1105)",
        "label_zh": "SIS橡胶",
        "group": "downstream",
    },
    # Alias: PRODUCT_GROUP_MAP uses "sis" (not "sis_rubber")
    "sis": {
        "table": "SIS",
        "supplier_name": "巴陵1105",
        "label_en": "SIS Rubber (Baling 1105)",
        "label_zh": "SIS橡胶",
        "group": "downstream",
    },
}

# Full product group membership (spec §6) — drives which IDs are surfaced
# in each section even when no table mapping is configured yet.
PRODUCT_GROUP_MAP: dict[str, list[str]] = {
    "upstream":   ["crude_oil", "naphtha", "cracked_c5", "cracked_c9"],
    "midstream":  ["isoprene", "piperylene", "dcpd", "raffinate_c5"],
    "downstream": ["sis", "c5_resin", "blowing_agent", "styrene"],
}

# Friendly display labels for every product (whether or not a table is mapped).
PRODUCT_LABELS: dict[str, dict] = {
    "crude_oil":   {"label_en": "Crude Oil (Brent)",  "label_zh": "原油(布伦特)"},
    "naphtha":     PRODUCT_TABLE_MAP["naphtha"],
    "cracked_c5":  {"label_en": "Cracked C5",         "label_zh": "裂解碳五"},
    "cracked_c9":  {"label_en": "Cracked C9",         "label_zh": "裂解碳九"},
    "isoprene":    PRODUCT_TABLE_MAP["isoprene"],
    "piperylene":  PRODUCT_TABLE_MAP["piperylene"],
    "dcpd":        PRODUCT_TABLE_MAP["dcpd"],
    "raffinate_c5":{"label_en": "Raffinate C5",       "label_zh": "抽余C5"},
    "sis":         PRODUCT_TABLE_MAP["sis_rubber"],
    "c5_resin":    {"label_en": "C5 Petroleum Resin", "label_zh": "C5石油树脂"},
    "blowing_agent":{"label_en": "Blowing Agent",     "label_zh": "发泡剂"},
    "styrene":     {"label_en": "Styrene",            "label_zh": "苯乙烯"},
}

# Alias used by spec section 6 downstream list
PRODUCT_LABELS["sis_rubber"] = PRODUCT_TABLE_MAP["sis_rubber"]

_metadata: Optional[MetaData] = None
_table_cache: dict[str, Table] = {}


def list_known_product_ids() -> list[str]:
    """All product_ids the dashboard surfaces (mapped + unmapped)."""
    out: list[str] = []
    for ids in PRODUCT_GROUP_MAP.values():
        out.extend(ids)
    return out


def get_product_table(product_id: str) -> Optional[Table]:
    """Return the autoloaded Table for a product_id, or None if unmapped / engine off."""
    cfg = PRODUCT_TABLE_MAP.get(product_id)
    if cfg is None:
        return None
    eng = get_mysql_engine()
    if eng is None:
        return None
    if product_id not in _table_cache:
        global _metadata
        if _metadata is None:
            _metadata = MetaData()
        _table_cache[product_id] = Table(
            cfg["table"], _metadata, autoload_with=eng
        )
    return _table_cache[product_id]


# Mapping from product_id → list of material_ids in the v_forecast_price view.
# Products not in this dict have no forecast data in the warehouse and will
# return null from the forecast endpoint.
MATERIAL_FORECAST_IDS: dict[str, list[str]] = {
    "cracked_c5": ["103319", "103320", "103321", "103322"],
    "cracked_c9": ["103323", "103324", "103325", "103326"],
}
