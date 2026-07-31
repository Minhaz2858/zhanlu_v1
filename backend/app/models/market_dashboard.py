"""Read-only SQLAlchemy Table objects for EDIA's MySQL warehouse.

Tables are autoloaded on first use — we never call Base.metadata.create_all
and never issue INSERT/UPDATE/DELETE. This module mirrors EDIA's
backend/src/db/market_db.py mapping verbatim.
"""
import logging
from typing import Optional

from sqlalchemy import MetaData, Table

from app.core.mysql_db import get_mysql_engine

logger = logging.getLogger(__name__)

# product_id -> {table, value_col, label_en, label_zh, group}
PRODUCT_TABLE_MAP: dict[str, dict] = {
    "naphtha": {
        "table": "裂解C5",
        "value_col": "日本石脑油Japanese naphtha",
        "label_en": "Japanese Naphtha",
        "label_zh": "日本石脑油",
        "group": "upstream",
    },
    "mixed_c5": {
        "table": "裂解C5",
        "value_col": "扬子裂解C5Yangzi Cracking C5",
        "label_en": "Yangzi Cracked C5",
        "label_zh": "扬子裂解C5",
        "group": "upstream",
    },
    "isoprene": {
        "table": "异戊二烯",
        "value_col": "华东市场价East China market price",
        "label_en": "Isoprene (East China)",
        "label_zh": "异戊二烯",
        "group": "midstream",
    },
    "piperylene": {
        "table": "间戊二烯",
        "value_col": "华东市场价East China market price",
        "label_en": "Piperylene (East China)",
        "label_zh": "间戊二烯",
        "group": "midstream",
    },
    "dcpd": {
        "table": "双环戊二烯",
        "value_col": "华东市场价East China market price",
        "label_en": "DCPD (East China)",
        "label_zh": "双环戊二烯",
        "group": "midstream",
    },
    "sis_rubber": {
        "table": "SIS",
        "value_col": "巴陵1105Baling 1105",
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
