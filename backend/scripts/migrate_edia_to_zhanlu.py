"""One-shot migration: EDIA 5.1.2 domain knowledge → zhanlu DB rows + pack JSON.

This script re-declares EDIA's dict constants (NOT imported from EDIA — avoids
coupling to EDIA's codebase which has MySQL deps, circular imports). After
running, EDIA's code is no longer needed.

Writes:
    - forecast_targets rows (canonical products, aliases stored as JSON, source=pack)
    - forecast_business_rules rows (seasonal, causal_driver, event_override types)
    - forecast_accuracy_log rows (MAPE + naive_mape + skill_vs_naive + below_naive_baseline flag)
    - domain_pack_installs row
    - ecisco_c5c9_v1.json pack file

CLI flags:
    --org-id   (default from settings)
    --app-id   (default from settings)
    --dry-run  (print rows without writing)

Idempotent: checks for existing product_key + org_id before insert. Safe to re-run.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# ── Re-declared EDIA constants (source of truth = EDIA actual code) ───────

# _PRODUCT_MAP from EDIA forecasting_engine.py:190-223
# 19 keys → 13 canonical products after alias collapse.
# Format: {edia_key: (canonical_name, price_source)}
_PRODUCT_MAP = {
    "crude_oil":    ("crude_oil", "brent"),
    "naphtha":      ("naphtha", "japan_cif"),
    "cracked_c5":   ("cracked_c5", "yangzi"),
    "mixed_c5":     ("cracked_c5", "yangzi"),       # alias → cracked_c5
    "cracked_c9":   ("cracked_c9", "yangzi_basf"),
    "mixed_c9":     ("cracked_c9", "yangzi_basf"),   # alias → cracked_c9
    "isoprene":     ("isoprene", "east_china"),
    "piperylene":   ("piperylene", "east_china"),
    "dcpd":         ("dcpd", "east_china"),
    "raffinate_c5": ("raffinate_c5", "erp_sale_order_weighted"),
    "sis":          ("sis", "baling_1105"),
    "sis_rubber":   ("sis", "baling_1105"),           # alias → sis
    "blowing_agent": ("blowing_agent", "erp_sale_order_weighted"),
    "c5_resin":     ("c5_resin", "erp_sale_order_weighted"),
    "hydrogenated_c9": ("hydrogenated_c9", "partner_forecast_price"),
    "trimethylbenzene": ("trimethylbenzene", "partner_forecast_price"),
    "c9_resin":     ("hydrogenated_c9", "partner_forecast_price"),  # alias → hydrogenated_c9
    "styrene":      ("styrene", "sinopec_sh"),
    "butadiene":    ("butadiene", "east_china_jiangyin"),  # model-only
    "sbs":          ("sbs", "east_china_791h"),               # model-only
}

# _SEASONAL_ADJ_MAP from EDIA forecasting_service.py:33-69
# Format: {product_key: {month_range: adjustment_pct}}
# Each entry is expanded to individual month entries in the rules table.
_SEASONAL_ADJ_MAP = {
    "c5_resin":     [{"months": [11, 12, 1, 2], "adjustment_pct": -2.5},
                     {"months": [3, 4, 5, 6, 7, 8, 9], "adjustment_pct": 1.0}],
    "dcpd":         [{"months": [11, 12, 1, 2], "adjustment_pct": -3.0},
                     {"months": [3, 4, 5, 6, 7, 8], "adjustment_pct": 1.5}],
    "isoprene":     [{"months": [2, 3], "adjustment_pct": 0.8}],
    "cracked_c5":   [{"months": [1, 2], "adjustment_pct": -1.0},
                     {"months": [3, 4, 5], "adjustment_pct": 0.5}],
    "piperylene":   [{"months": [2, 3], "adjustment_pct": 1.2}],
    "styrene":      [{"months": [11, 12, 1], "adjustment_pct": -1.5}],
    "butadiene":    [{"months": [11, 12, 1, 2], "adjustment_pct": -2.0}],
}

# _ELASTICITIES from EDIA forecasting_service.py:73-91
# Format: {product_key: elasticity_to_c5}
# The naphtha→C5 cascade factor is 0.82 (from forecasting_engine.py:379).
# The crude→naphtha factor is 1.35 (from forecasting_engine.py).
_ELASTICITIES = {
    "naphtha":       1.00,
    "cracked_c5":    0.75,
    "mixed_c5":      0.75,    # same as canonical cracked_c5
    "cracked_c9":    0.65,
    "mixed_c9":      0.65,    # same as canonical cracked_c9
    "isoprene":      0.60,
    "piperylene":    0.55,
    "dcpd":          0.50,
    "raffinate_c5":  0.55,
    "sis":           0.45,
    "sis_rubber":    0.45,    # same as canonical sis
    "blowing_agent": 0.45,
    "styrene":       0.47,
    "c5_resin":      0.45,
    "hydrogenated_c9": 0.50,
    "c9_resin":      0.50,    # same as canonical hydrogenated_c9
    "trimethylbenzene": 0.50,
}

# Naphtha→C5 cascade factor (from forecasting_engine.py:379)
_NAPHTHA_C5_CASCADE = 0.82

# Crude→naphtha factor
_CRUDE_NAPHTHA_CASCADE = 1.35

# _BELOW_NAIVE_PRODUCTS from EDIA forecasting_engine.py:313-315
# Actual EDIA code value — differs from design doc.
_BELOW_NAIVE_PRODUCTS = frozenset({"cracked_c5", "cracked_c9", "mixed_c9", "styrene"})

# _POST_HOLIDAY_PRODUCTS from EDIA forecasting_engine.py:1979-1982
# +1.2% adjustment in months 2,3 when market_phase == "post_holiday_restart"
_POST_HOLIDAY_PRODUCTS = frozenset({"piperylene", "c5_resin", "cracked_c5", "isoprene", "raffinate_c5"})
_POST_HOLIDAY_MONTHS = [2, 3]
_POST_HOLIDAY_ADJUSTMENT_PCT = 1.2

# MAPE baselines from EDIA MAPE_BASELINE.md (Phase 1 Final column)
# Format: {canonical_product: {"mape": float, "naive_mape": float_or_None, "skill_vs_naive": float_or_None}}
_MAPE_BASELINES = {
    "cracked_c5":       {"mape": 0.0431, "naive_mape": 0.0370, "skill_vs_naive": 0.0061},
    "cracked_c9":       {"mape": 0.0779, "naive_mape": 0.0410, "skill_vs_naive": 0.0369},
    "isoprene":         {"mape": 0.0200, "naive_mape": 0.0417, "skill_vs_naive": -0.0217},
    "piperylene":       {"mape": 0.0559, "naive_mape": 0.0672, "skill_vs_naive": -0.0113},
    "dcpd":             {"mape": 0.0384, "naive_mape": 0.0351, "skill_vs_naive": 0.0033},
    "sis":              {"mape": 0.0124, "naive_mape": None, "skill_vs_naive": None},
    "c5_resin":         {"mape": 0.1043, "naive_mape": 0.1055, "skill_vs_naive": -0.0012},
    "blowing_agent":    {"mape": 0.0393, "naive_mape": 0.0407, "skill_vs_naive": -0.0014},
    "styrene":          {"mape": 0.0477, "naive_mape": 0.0390, "skill_vs_naive": 0.0087},
    "trimethylbenzene": {"mape": 0.2244, "naive_mape": None, "skill_vs_naive": None},
    "naphtha":          {"mape": 0.0250, "naive_mape": 0.0200, "skill_vs_naive": 0.0050},
    "hydrogenated_c9":  {"mape": 0.0300, "naive_mape": 0.0220, "skill_vs_naive": 0.0080},
    "raffinate_c5":     {"mape": None, "naive_mape": None, "skill_vs_naive": None},  # null: no backtest
    # Products excluded from weekly report (model-only / chart-only)
    "crude_oil":        {"mape": None, "naive_mape": None, "skill_vs_naive": None},
    "butadiene":        {"mape": None, "naive_mape": None, "skill_vs_naive": None},
    "sbs":              {"mape": None, "naive_mape": None, "skill_vs_naive": None},
}

# ARIMA orders from EDIA arima_orders.json
# Only 3 products have non-default orders; all others use (2,1,2).
_ARIMA_ORDERS = {
    "dcpd":         (3, 1, 2),
    "sis":          (3, 1, 3),
    "styrene":      (3, 1, 0),
}
_DEFAULT_ARIMA_ORDER = (2, 1, 2)

# Canonical products and their aliases (derived from _PRODUCT_MAP)
# Products excluded from weekly report: crude_oil, butadiene, sbs
_CANONICAL_PRODUCTS = {
    "naphtha":           {"aliases": [], "price_source": "japan_cif", "include_in_weekly_report": True, "report_order": 1},
    "cracked_c5":        {"aliases": ["mixed_c5"], "price_source": "yangzi", "include_in_weekly_report": True, "report_order": 2},
    "cracked_c9":        {"aliases": ["mixed_c9"], "price_source": "yangzi_basf", "include_in_weekly_report": True, "report_order": 3},
    "isoprene":          {"aliases": [], "price_source": "east_china", "include_in_weekly_report": True, "report_order": 4},
    "piperylene":        {"aliases": [], "price_source": "east_china", "include_in_weekly_report": True, "report_order": 5},
    "dcpd":              {"aliases": [], "price_source": "east_china", "include_in_weekly_report": True, "report_order": 6},
    "raffinate_c5":      {"aliases": [], "price_source": "erp_sale_order_weighted", "include_in_weekly_report": True, "report_order": 7},
    "sis":               {"aliases": ["sis_rubber"], "price_source": "baling_1105", "include_in_weekly_report": True, "report_order": 8},
    "blowing_agent":     {"aliases": [], "price_source": "erp_sale_order_weighted", "include_in_weekly_report": True, "report_order": 9},
    "c5_resin":          {"aliases": [], "price_source": "erp_sale_order_weighted", "include_in_weekly_report": True, "report_order": 10},
    "hydrogenated_c9":   {"aliases": ["c9_resin"], "price_source": "partner_forecast_price", "include_in_weekly_report": True, "report_order": 11},
    "trimethylbenzene":  {"aliases": [], "price_source": "partner_forecast_price", "include_in_weekly_report": True, "report_order": 12},
    "styrene":           {"aliases": [], "price_source": "sinopec_sh", "include_in_weekly_report": True, "report_order": 13},
    # Excluded from weekly report (model-only / chart-only)
    "crude_oil":         {"aliases": [], "price_source": "brent", "include_in_weekly_report": False, "report_order": None},
    "butadiene":         {"aliases": [], "price_source": "east_china_jiangyin", "include_in_weekly_report": False, "report_order": None},
    "sbs":               {"aliases": [], "price_source": "east_china_791h", "include_in_weekly_report": False, "report_order": None},
}

# ── Domain Pack JSON structure ──────────────────────────────────────────

PACK_KEY = "ecisco_c5c9"
PACK_VERSION = "1"

logger = logging.getLogger(__name__)


def _build_canonical_targets(org_id: str, app_id: str) -> list[dict]:
    """Build forecast_targets rows from canonical products."""
    rows = []
    for product_key, info in _CANONICAL_PRODUCTS.items():
        row = {
            "product_key": product_key,
            "name": product_key.replace("_", " ").title(),
            "aliases": info["aliases"],
            "org_id": org_id,
            "app_id": app_id,
            "datasource": {
                "price_source": info["price_source"],
                "table": "monthly_prices",
                "time_col": "date",
                "measure": "price",
                "dims": {"product": product_key},
                "granularity": "monthly",
                "region": "east_china",
            },
            "level": 2,  # pack-grade
            "quality_grade": None,
            "quality_stats": None,
            "status": "active",
            "source": "pack",
            "model_config": {
                "arima_order": list(_ARIMA_ORDERS.get(product_key, _DEFAULT_ARIMA_ORDER)),
                "seasonal_period": 12,
                "model_tier": "ensemble",
            },
            "include_in_weekly_report": info["include_in_weekly_report"],
            "report_order": info["report_order"],
        }
        rows.append(row)
    return rows


def _build_seasonal_rules(org_id: str, app_id: str, target_id_map: dict) -> list[dict]:
    """Build forecast_business_rules rows for seasonal adjustments."""
    rows = []
    for product_key, adjustments in _SEASONAL_ADJ_MAP.items():
        canonical = _PRODUCT_MAP.get(product_key, (product_key, ""))[0]
        target_id = target_id_map.get(canonical)
        for adj in adjustments:
            for month in adj["months"]:
                row = {
                    "target_id": target_id,
                    "org_id": org_id,
                    "app_id": app_id,
                    "rule_type": "seasonal",
                    "params": {
                        "month": month,
                        "adjustment_pct": adj["adjustment_pct"],
                    },
                    "status": "active",
                    "source": "pack",
                    "confidence": 0.9,
                }
                rows.append(row)
    return rows


def _build_causal_rules(org_id: str, app_id: str, target_id_map: dict) -> list[dict]:
    """Build forecast_business_rules rows for causal (elasticity) drivers."""
    rows = []
    for product_key, elasticity in _ELASTICITIES.items():
        canonical = _PRODUCT_MAP.get(product_key, (product_key, ""))[0]
        target_id = target_id_map.get(canonical)
        if canonical == "naphtha":
            # naphtha's driver is crude_oil
            driver = "crude_oil"
            driver_elasticity = _CRUDE_NAPHTHA_CASCADE
        else:
            # other products' driver is naphtha (with cascade)
            driver = "naphtha"
            driver_elasticity = elasticity * _NAPHTHA_C5_CASCADE
        row = {
            "target_id": target_id,
            "org_id": org_id,
            "app_id": app_id,
            "rule_type": "causal_driver",
            "params": {
                "driver": driver,
                "elasticity": elasticity,
                "cascade_factor": _NAPHTHA_C5_CASCADE if driver == "naphtha" else None,
                "dampening": driver_elasticity,
            },
            "status": "active",
            "source": "pack",
            "confidence": 0.85,
        }
        rows.append(row)
    return rows


def _build_event_override_rules(org_id: str, app_id: str, target_id_map: dict) -> list[dict]:
    """Build forecast_business_rules rows for post-holiday overrides."""
    rows = []
    for product_key in _POST_HOLIDAY_PRODUCTS:
        canonical = _PRODUCT_MAP.get(product_key, (product_key, ""))[0]
        target_id = target_id_map.get(canonical)
        row = {
            "target_id": target_id,
            "org_id": org_id,
            "app_id": app_id,
            "rule_type": "event_override",
            "params": {
                "event": "post_holiday_lull",
                "months": _POST_HOLIDAY_MONTHS,
                "adjustment_pct": _POST_HOLIDAY_ADJUSTMENT_PCT,
                "condition": "market_phase == post_holiday_restart",
            },
            "status": "active",
            "source": "pack",
            "confidence": 0.75,
        }
        rows.append(row)
    return rows


def _build_guardrail_rules(org_id: str, app_id: str) -> list[dict]:
    """Build forecast_business_rules rows for global guardrails."""
    rows = [
        {
            "target_id": None,  # global
            "org_id": org_id,
            "app_id": app_id,
            "rule_type": "guardrail",
            "params": {
                "min_history": 14,
                "max_mape": 0.30,
                "below_naive_action": "fallback_to_naive_with_warning",
            },
            "status": "active",
            "source": "pack",
            "confidence": 1.0,
        },
    ]
    return rows


def _build_accuracy_log_rows(org_id: str, app_id: str, target_id_map: dict) -> list[dict]:
    """Build forecast_accuracy_log rows from MAPE baselines."""
    rows = []
    for canonical_product, baseline in _MAPE_BASELINES.items():
        target_id = target_id_map.get(canonical_product)
        if target_id is None:
            continue
        mape = baseline["mape"]
        if mape is None:
            # Skip products with null MAPE (no backtest produced)
            continue
        naive_mape = baseline["naive_mape"]
        skill_vs_naive = baseline["skill_vs_naive"]
        # below_naive_baseline is True when skill_vs_naive > 0 (model worse than naive)
        below_naive = skill_vs_naive is not None and skill_vs_naive > 0
        # Also check _BELOW_NAIVE_PRODUCTS for the canonical key
        if canonical_product in _BELOW_NAIVE_PRODUCTS:
            below_naive = True

        row = {
            "target_id": target_id,
            "org_id": org_id,
            "app_id": app_id,
            "horizon_days": 30,
            "n_backtests": 12,
            "mape": mape,
            "naive_mape": naive_mape,
            "skill_vs_naive": skill_vs_naive,
            "below_naive_baseline": below_naive,
            "per_model": {
                "arima": mape,
                "seasonal_naive": naive_mape,
            },
        }
        rows.append(row)
    return rows


def _build_pack_json(canonical_targets: list[dict]) -> dict:
    """Build the domain pack JSON structure."""
    products = []
    for target in canonical_targets:
        product_key = target["product_key"]
        seasonal_rules = []
        if product_key in _SEASONAL_ADJ_MAP:
            for adj in _SEASONAL_ADJ_MAP[product_key]:
                seasonal_rules.append({
                    "months": adj["months"],
                    "adjustment_pct": adj["adjustment_pct"],
                })
        causal_defaults = {}
        if product_key in _ELASTICITIES:
            causal_defaults["naphtha_elasticity"] = _ELASTICITIES[product_key]
            causal_defaults["cascade_factor"] = _NAPHTHA_C5_CASCADE
        elif product_key == "naphtha":
            causal_defaults["crude_elasticity"] = _CRUDE_NAPHTHA_CASCADE

        product_entry = {
            "product_key": product_key,
            "name": target["name"],
            "aliases": target["aliases"],
            "source_hints": target["datasource"],
            "seasonal_rules": seasonal_rules,
            "causal_defaults": causal_defaults,
            "report_config": {
                "include_in_weekly_report": target["include_in_weekly_report"],
                "report_order": target["report_order"],
            },
        }
        if product_key in _BELOW_NAIVE_PRODUCTS:
            product_entry["below_naive_flag"] = True
        products.append(product_entry)

    pack = {
        "pack_key": PACK_KEY,
        "pack_version": PACK_VERSION,
        "products": products,
        "guardrails": {
            "min_history": 14,
            "max_mape": 0.30,
            "below_naive_action": "fallback_to_naive_with_warning",
        },
        "report_template": {
            "title": "Ecisco C5/C9 Weekly Forecast Report",
            "sections": [
                "summary",
                "price_forecasts_3d_7d_30d",
                "accuracy_table",
                "seasonal_adjustments",
                "risk_flags",
                "recommendations",
            ],
            "chart_defaults": {
                "type": "bar",
                "x_key": "product",
                "y_keys": ["3d_base", "7d_base", "30d_base"],
            },
        },
    }
    return pack


def run_migration(org_id: str, app_id: str, dry_run: bool = False) -> dict:
    """Execute the full migration from EDIA constants to zhanlu DB + pack JSON.

    Returns a summary dict with counts and (if dry_run) the row data.
    """
    from app.database import SessionLocal
    from app.models.forecasting import (
        ForecastTarget, ForecastBusinessRule,
        ForecastAccuracyLog, DomainPackInstall,
    )

    # ── Build all row data ──────────────────────────────────────
    canonical_targets = _build_canonical_targets(org_id, app_id)
    pack_json = _build_pack_json(canonical_targets)

    summary = {
        "targets": len(canonical_targets),
        "seasonal_rules": 0,
        "causal_rules": 0,
        "event_override_rules": 0,
        "guardrail_rules": 0,
        "accuracy_log_rows": 0,
        "pack_install": 1,
        "dry_run": dry_run,
    }

    if dry_run:
        # Print summary without DB writes
        logger.info("DRY RUN — no DB writes will be performed")
        for target in canonical_targets:
            logger.info("  Target: %s (aliases=%s, level=%d, source=pack)",
                        target["product_key"], target["aliases"], target["level"])
        logger.info("Pack JSON would be written to packs/%s_v%s.json", PACK_KEY, PACK_VERSION)
        summary["pack_json"] = pack_json
        return summary

    # ── Write to DB ─────────────────────────────────────────────
    db = SessionLocal()
    try:
        # 1. Write forecast_targets (idempotent: check by product_key + org_id)
        target_id_map = {}  # canonical_product_key → UUID
        for target_data in canonical_targets:
            existing = db.query(ForecastTarget).filter(
                ForecastTarget.product_key == target_data["product_key"],
                ForecastTarget.org_id == org_id,
                ForecastTarget.is_deleted == False,
            ).first()
            if existing:
                # Update existing row
                for key, value in target_data.items():
                    if key not in ("id", "created_at", "updated_at", "is_deleted"):
                        setattr(existing, key, value)
                target_id_map[target_data["product_key"]] = existing.id
                logger.debug("Updated existing target: %s", target_data["product_key"])
            else:
                target = ForecastTarget(**target_data)
                db.add(target)
                db.flush()  # get the ID
                target_id_map[target_data["product_key"]] = target.id
                logger.debug("Created new target: %s (id=%s)", target_data["product_key"], target.id)

        # 2. Write seasonal rules
        seasonal_rows = _build_seasonal_rules(org_id, app_id, target_id_map)
        for row_data in seasonal_rows:
            # Check for existing rule (same target, same rule_type, same month)
            existing = db.query(ForecastBusinessRule).filter(
                ForecastBusinessRule.target_id == row_data["target_id"],
                ForecastBusinessRule.rule_type == "seasonal",
                ForecastBusinessRule.org_id == org_id,
                ForecastBusinessRule.is_deleted == False,
            ).first()
            if not existing or existing.params.get("month") != row_data["params"]["month"]:
                rule = ForecastBusinessRule(**row_data)
                db.add(rule)
        summary["seasonal_rules"] = len(seasonal_rows)

        # 3. Write causal rules
        causal_rows = _build_causal_rules(org_id, app_id, target_id_map)
        for row_data in causal_rows:
            existing = db.query(ForecastBusinessRule).filter(
                ForecastBusinessRule.target_id == row_data["target_id"],
                ForecastBusinessRule.rule_type == "causal_driver",
                ForecastBusinessRule.org_id == org_id,
                ForecastBusinessRule.is_deleted == False,
            ).first()
            if not existing:
                rule = ForecastBusinessRule(**row_data)
                db.add(rule)
            else:
                # Update existing causal rule with latest elasticity data
                for key, value in row_data.items():
                    if key not in ("id", "created_at", "updated_at", "is_deleted"):
                        setattr(existing, key, value)
        summary["causal_rules"] = len(causal_rows)

        # 4. Write event override rules
        event_rows = _build_event_override_rules(org_id, app_id, target_id_map)
        for row_data in event_rows:
            existing = db.query(ForecastBusinessRule).filter(
                ForecastBusinessRule.target_id == row_data["target_id"],
                ForecastBusinessRule.rule_type == "event_override",
                ForecastBusinessRule.org_id == org_id,
                ForecastBusinessRule.is_deleted == False,
            ).first()
            if not existing:
                rule = ForecastBusinessRule(**row_data)
                db.add(rule)
        summary["event_override_rules"] = len(event_rows)

        # 5. Write guardrail rules
        guardrail_rows = _build_guardrail_rules(org_id, app_id)
        for row_data in guardrail_rows:
            existing = db.query(ForecastBusinessRule).filter(
                ForecastBusinessRule.rule_type == "guardrail",
                ForecastBusinessRule.target_id == None,
                ForecastBusinessRule.org_id == org_id,
                ForecastBusinessRule.is_deleted == False,
            ).first()
            if not existing:
                rule = ForecastBusinessRule(**row_data)
                db.add(rule)
        summary["guardrail_rules"] = len(guardrail_rows)

        # 6. Write accuracy log rows
        accuracy_rows = _build_accuracy_log_rows(org_id, app_id, target_id_map)
        for row_data in accuracy_rows:
            existing = db.query(ForecastAccuracyLog).filter(
                ForecastAccuracyLog.target_id == row_data["target_id"],
                ForecastAccuracyLog.org_id == org_id,
                ForecastAccuracyLog.is_deleted == False,
            ).first()
            if not existing:
                log_entry = ForecastAccuracyLog(**row_data)
                db.add(log_entry)
        summary["accuracy_log_rows"] = len(accuracy_rows)

        # 7. Write domain_pack_installs row
        existing_pack = db.query(DomainPackInstall).filter(
            DomainPackInstall.pack_key == PACK_KEY,
            DomainPackInstall.org_id == org_id,
            DomainPackInstall.is_deleted == False,
        ).first()
        if not existing_pack:
            pack_install = DomainPackInstall(
                pack_key=PACK_KEY,
                pack_version=PACK_VERSION,
                config=pack_json,
                org_id=org_id,
                app_id=app_id,
                installed_at=datetime.utcnow(),
            )
            db.add(pack_install)
        else:
            existing_pack.pack_version = PACK_VERSION
            existing_pack.config = pack_json
        db.commit()
        logger.info("Migration committed successfully")

    except Exception as e:
        db.rollback()
        logger.error("Migration failed: %s", e)
        raise
    finally:
        db.close()

    # ── Write pack JSON file ────────────────────────────────────
    pack_dir = Path(__file__).parent.parent / "app" / "services" / "forecasting" / "packs"
    pack_dir.mkdir(parents=True, exist_ok=True)
    pack_path = pack_dir / f"{PACK_KEY}_v{PACK_VERSION}.json"
    pack_path.write_text(json.dumps(pack_json, indent=2, ensure_ascii=False))
    logger.info("Pack JSON written to %s", pack_path)

    summary["pack_json_path"] = str(pack_path)
    return summary


def main():
    parser = argparse.ArgumentParser(description="EDIA → zhanlu migration script")
    parser.add_argument("--org-id", default=None, help="Organization ID (default from settings)")
    parser.add_argument("--app-id", default=None, help="Application ID (default from settings)")
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing to DB")
    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Get org_id/app_id from settings if not provided
    from app.config import settings
    org_id = args.org_id or settings.DEFAULT_ORG_ID or "default"
    app_id = args.app_id or settings.DEFAULT_APP_ID or "default"

    logger.info("Running migration with org_id=%s, app_id=%s, dry_run=%s", org_id, app_id, args.dry_run)

    summary = run_migration(org_id=org_id, app_id=app_id, dry_run=args.dry_run)

    # Print summary
    print("\n=== Migration Summary ===")
    for key, value in summary.items():
        if key != "pack_json":  # skip the full JSON in summary
            print(f"  {key}: {value}")
    print("\nDone.")


if __name__ == "__main__":
    main()
