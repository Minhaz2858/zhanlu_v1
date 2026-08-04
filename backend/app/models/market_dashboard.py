"""Read-only SQLAlchemy Table objects for EDIA's MySQL warehouse.

Tables are autoloaded on first use — we never call Base.metadata.create_all
and never issue INSERT/UPDATE/DELETE.

The dashboard reads from TWO source families in a primary→fallback chain:

1. `md_t_lz_price` (primary) — the LIVE Longzhong/隆众网 feed that EDIA
   syncs daily. `FMATERIAL_NAME` is a composite "{product}-{supplier}"
   string (e.g., "裂解C5-布伦特", "SIS-巴陵1105"). This table is fresh
   to 2026-07-30+ for the products the chemical dashboard surfaces.
2. Long-form tables (fallback) — the legacy 裂解C5 / 异戊二烯 / etc.
   tables that EDIA stopped updating around 2026-03-20. Kept as a
   safety net so the dashboard never goes blank if the primary source
   is missing for a product.

The service layer (services/market_dashboard_service.py) handles the
fallback chain transparently — callers see one snapshot per product
regardless of which table served the data.
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

# md_t_lz_price schema (the LIVE primary source)
MD_LZ_DATE_COL = "FDATE"
MD_LZ_PRICE_COL = "FTAXPRICE"
MD_LZ_MATERIAL_COL = "FMATERIAL_NAME"


def make_md_lz_primary(name_pattern: str) -> dict:
    """Build a `primary` config block pointing at md_t_lz_price.

    The composite FMATERIAL_NAME is "{product}-{supplier}". `name_pattern`
    is the EXACT FMATERIAL_NAME value (no LIKE) — e.g. "裂解C5-布伦特".
    Use `make_md_lz_prefix_primary` for LIKE-based filtering.
    """
    return {
        "table": "md_t_lz_price",
        "name_pattern": name_pattern,
        "name_match": "exact",
        "date_col": MD_LZ_DATE_COL,
        "price_col": MD_LZ_PRICE_COL,
        "material_col": MD_LZ_MATERIAL_COL,
    }


# product_id -> {table, supplier_name, label_en, label_zh, group, primary?}
# supplier_name is the 厂商名supplier_name value that identifies the
# specific price series within the table.
#
# `regional_suppliers` (optional) restricts the per-product regional
# scanner to only the supplier_name values that actually represent data
# for this product — necessary because some EDIA tables (e.g. 裂解C5)
# store multiple products distinguished solely by supplier_name. When
# this key is absent, every distinct supplier in the table is shown.
#
# `primary` (optional) — preferred data source. When set, the service
# tries this source FIRST (md_t_lz_price, the LIVE Longzhong/隆众网
# feed updated daily through 2026-07-30+) and only falls back to the
# legacy `table`/`supplier_name` long-form table when the primary
# returns zero rows. The long-form tables (裂解C5, 异戊二烯, etc.) are
# frozen at 2026-03-20, so the primary source is what actually keeps
# the dashboard fresh.
PRODUCT_TABLE_MAP: dict[str, dict] = {
    "crude_oil": {
        "primary": make_md_lz_primary("裂解C5-布伦特"),
        "table": "裂解C5",
        "supplier_name": "布伦特",
        "regional_suppliers": ["布伦特"],
        "label_en": "Crude Oil (Brent)",
        "label_zh": "原油(布伦特)",
        "group": "upstream",
    },
    "naphtha": {
        "primary": make_md_lz_primary("裂解C5-日本石脑油"),
        "table": "裂解C5",
        "supplier_name": "日本石脑油",
        "regional_suppliers": ["日本石脑油"],
        "label_en": "Japanese Naphtha",
        "label_zh": "日本石脑油",
        "group": "upstream",
    },
    "cracked_c5": {
        "primary": make_md_lz_primary("裂解C5-裂解C5均价"),
        "table": "裂解C5",
        "supplier_name": "裂解C5均价",
        "regional_suppliers": [
            "裂解C5均价", "扬子裂解C5", "茂名裂解C5",
            "扬子巴斯夫裂解C5", "扬子裂解碳五", "扬子巴斯夫",
        ],
        "label_en": "Cracked C5 (Avg)",
        "label_zh": "裂解碳五(均价)",
        "group": "upstream",
    },
    "mixed_c5": {
        "primary": make_md_lz_primary("裂解C5-扬子裂解C5"),
        "table": "裂解C5",
        "supplier_name": "扬子裂解C5",
        "regional_suppliers": [
            "扬子裂解C5", "扬子裂解碳五", "扬子巴斯夫裂解C5", "扬子巴斯夫",
        ],
        "label_en": "Yangzi Cracked C5",
        "label_zh": "扬子裂解C5",
        "group": "upstream",
    },
    "isoprene": {
        "primary": make_md_lz_primary("异戊二烯-华东市场价"),
        "table": "异戊二烯",
        "supplier_name": "华东市场价",
        "label_en": "Isoprene (East China)",
        "label_zh": "异戊二烯",
        "group": "midstream",
    },
    "piperylene": {
        "primary": make_md_lz_primary("间戊二烯-华东市场价"),
        "table": "间戊二烯",
        "supplier_name": "华东市场价",
        "label_en": "Piperylene (East China)",
        "label_zh": "间戊二烯",
        "group": "midstream",
    },
    "dcpd": {
        "primary": make_md_lz_primary("双环戊二烯-华东市场价"),
        "table": "双环戊二烯",
        "supplier_name": "华东市场价",
        "label_en": "DCPD (East China)",
        "label_zh": "双环戊二烯",
        "group": "midstream",
    },
    "sis_rubber": {
        "primary": make_md_lz_primary("SIS-巴陵1105"),
        "table": "SIS",
        "supplier_name": "巴陵1105",
        "label_en": "SIS Rubber (Baling 1105)",
        "label_zh": "SIS橡胶",
        "group": "downstream",
    },
    # Alias: PRODUCT_GROUP_MAP uses "sis" (not "sis_rubber")
    "sis": {
        "primary": make_md_lz_primary("SIS-巴陵1105"),
        "table": "SIS",
        "supplier_name": "巴陵1105",
        "label_en": "SIS Rubber (Baling 1105)",
        "label_zh": "SIS橡胶",
        "group": "downstream",
    },
    "cracked_c9": {
        "primary": make_md_lz_primary("裂解C9-裂解C9均价"),
        "table": "裂解c9",
        "supplier_name": "裂解C9均价",
        "label_en": "Cracked C9",
        "label_zh": "裂解碳九",
        "group": "upstream",
    },
    "styrene": {
        "primary": make_md_lz_primary("苯乙烯-中石化华东"),
        "table": "苯乙烯",
        "supplier_name": "上海石化",
        "label_en": "Styrene",
        "label_zh": "苯乙烯",
        "group": "downstream",
    },
    # LZ-product schema (lz_v_*_data tables): uses biz_date(业务日期) /
    # tax_price(含税单价) columns instead of EDIA's 日期Time / 价格price.
    # Blowing Agent also has a primary in md_t_lz_price (4 regional
    # series: 华东/华南/华中/青岛).
    "blowing_agent": {
        "primary": make_md_lz_primary("戊烷发泡剂-华东市场价"),
        "table": "lz_v_戊烷泡发剂_data",
        "supplier_name": "华东市场价",
        "date_col": "biz_date(业务日期)",
        "price_col": "tax_price(含税单价)",
        "supplier_col": "supplier_name(厂商名称)",
        "regional_suppliers": [
            "华东市场价", "华南市场价", "华中市场价",
            "青岛炼化", "青岛炼化-发泡剂F7",
        ],
        "label_en": "Blowing Agent",
        "label_zh": "戊烷发泡剂",
        "group": "downstream",
    },
    # ERP sales-order tables: only sales data exists; treat as fallback
    # market prices for these unmapped downstream products.
    "raffinate_c5": {
        "table": "sale_erp_v_抽余碳五_data",
        "supplier_name": "__all__",
        "date_col": "PLANDATE",
        "price_col": "FTAXPRICE",
        "supplier_col": None,
        "regional_suppliers": ["__all__"],
        "label_en": "Raffinate C5",
        "label_zh": "抽余C5",
        "group": "midstream",
    },
    "c5_resin": {
        "table": "sale_erp_v_碳五石油树脂_data",
        "supplier_name": "__all__",
        "date_col": "PLANDATE",
        "price_col": "FTAXPRICE",
        "supplier_col": None,
        "regional_suppliers": ["__all__"],
        "label_en": "C5 Petroleum Resin",
        "label_zh": "C5石油树脂",
        "group": "downstream",
    },
}


def get_md_lz_regional_prefix(product_id: str) -> Optional[str]:
    """Return the FMATERIAL_NAME prefix that matches all regional rows
    for a product in `md_t_lz_price`. For example, all cracking-c5
    regionals start with "裂解C5-".

    Used by the regional scanner to enumerate every distinct supplier
    for a product in the primary source.
    """
    cfg = PRODUCT_TABLE_MAP.get(product_id)
    if not cfg:
        return None
    primary = cfg.get("primary")
    if not primary or primary.get("table") != "md_t_lz_price":
        return None
    name_pattern = primary.get("name_pattern") or ""
    if "-" not in name_pattern:
        return None
    return name_pattern.split("-", 1)[0] + "-"


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
