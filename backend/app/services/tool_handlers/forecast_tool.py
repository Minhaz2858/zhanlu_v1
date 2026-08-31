"""Forecasting tools — forecast_discover, forecast_run, forecast_get, forecast_accuracy,
forecast_rules, forecast_report, forecast_ppt.

Heavy compute tools (discover, run) are gated (``enabled_by_default=False``)
and intended for the automation/subagent layer. Lightweight read tools (get,
accuracy, rules, report, ppt) are safe for the user-facing BI agent.

Tool surface follows the exact ``db_tools.py`` pattern — async handlers,
OpenAI-format schemas, and registry registration at module import.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.forecasting import (
    ForecastTarget,
    ForecastRun,
    ForecastAccuracyLog,
    ForecastBusinessRule,
)
from app.services.artifacts.artifact_service import ArtifactService
from app.services.forecasting.engine import ForecastEngine
from app.services.forecasting.pptx_payload import ForecastPayloadAssembler
from app.services.forecasting.report import WeeklyReportGenerator
from app.services.forecasting.what_if import compute_what_if
from app.services.tool_handlers.artifact_tool import _create_artifact_tool
from app.services.tool_handlers.db_tools import _require_kb_id
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ======================================================================
# Helpers
# ======================================================================

def _resolve_org_context(context: dict | None) -> tuple[str, str]:
    """Extract ``org_id`` and ``app_id`` from the agent runtime context.

    Falls back to ``"default-org"`` / ``"default-app"`` when called
    outside an agent context (e.g. cron automation).
    """
    ctx = context or {}
    return (
        ctx.get("org_id", "default-org"),
        ctx.get("app_id", "default-app"),
    )


def _forecast_engine(db: Session):
    """Return a ``ForecastEngine`` for the current session.

    The legacy engine is the sole forecasting backend (the unified
    ``ForecastingAgentService`` facade was removed with the market
    dashboard feature).
    """
    return ForecastEngine(db)


def _serialize_target(t: ForecastTarget) -> dict:
    """Convert a ForecastTarget row to a safe dict for tool responses."""
    ds = t.datasource or {}
    # Extract material_code from product_key for SKU targets
    # (e.g. "<product>.<code>" → "<code>")
    material_code = None
    parts = t.product_key.split(".")
    if len(parts) >= 2:
        material_code = parts[-1]

    return {
        "id": t.id,
        "product_key": t.product_key,
        "name": t.name,
        "level": t.level,
        "quality_grade": t.quality_grade,
        "status": t.status,
        "granularity": ds.get("granularity"),
        "source": t.source,
        "include_in_weekly_report": t.include_in_weekly_report,
        "report_order": t.report_order,
        # SKU fields (None for md_t_lz_price products)
        "material_code": material_code,
        "datasource_table": ds.get("table"),
        "datasource_where": ds.get("where"),
    }


def _serialize_rule(r: ForecastBusinessRule) -> dict:
    """Convert a ForecastBusinessRule row to a safe dict for tool responses."""
    return {
        "id": r.id,
        "target_id": r.target_id,
        "rule_type": r.rule_type,
        "params": r.params,
        "status": r.status,
        "source": r.source,
        "confidence": r.confidence,
        "approved_by_id": r.approved_by_id,
        "approved_at": r.approved_at.isoformat() if r.approved_at else None,
        "created_date": r.created_date.isoformat() if r.created_date else None,
    }


# ======================================================================
# forecast_discover  (heavy — gated)
# ======================================================================

async def _forecast_discover(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Scan a bound KnowledgeBase for forecastable time series and register
    them as ``ForecastTarget`` rows with ``status='discovered'``.

    Required args:
        data_source_id  (str): The bound KB id to scan.

    Optional args:
        max_tables      (int): Max tables to inspect (default 50).
    """
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err

    max_tables = int(args.get("max_tables", 50))
    org_id, app_id = _resolve_org_context(context)

    def _do_work() -> dict:
        engine = _forecast_engine(db)
        targets = engine.discover_and_register(kb_id, org_id, app_id, max_tables)
        return {
            "success": True,
            "targets": [_serialize_target(t) for t in targets],
            "count": len(targets),
        }

    try:
        return await asyncio.to_thread(_do_work)
    except Exception as exc:
        logger.warning("forecast_discover failed (kb=%s): %s", kb_id, exc)
        return {"success": False, "error": f"Discovery failed: {exc}"}


FORECAST_DISCOVER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_discover",
        "description": (
            "Scan a bound data source for forecastable time-series columns "
            "(date + numeric measure) and register them as forecast targets. "
            "This is a heavy schema-scanning operation — call it once after "
            "connecting a new data source, not on every chat turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The bound KnowledgeBase/data-source ID to scan.",
                },
                "max_tables": {
                    "type": "integer",
                    "description": "Maximum number of tables to inspect (default 50).",
                    "default": 50,
                },
            },
            "required": ["data_source_id"],
        },
    },
}


# ======================================================================
# forecast_run  (heavy — gated)
# ======================================================================

async def _forecast_run(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Run the full forecasting pipeline for one target or all active targets.

    Optional args:
        target_id       (str): Forecast one target. If omitted, runs all active
                               targets in the org.
        horizons        (list[int]): Forecast horizons in days (default [3,7,30]).
        seasonal_period (int): Expected seasonal cycle (default 7).
    """
    target_id = args.get("target_id")
    product_id = (args.get("product_id") or "").strip().lower() or None
    horizons = [int(h) for h in (args.get("horizons") or [3, 7, 30])]
    seasonal_period = int(args.get("seasonal_period", 7))
    org_id, _app_id = _resolve_org_context(context)

    def _do_work() -> dict:
        from app.services.forecasting.answer_format import (
            format_run_answer,
            select_headline_day,
        )
        from app.services.forecasting.truth_gate import wrap_forecast_result

        engine = _forecast_engine(db)

        if target_id:
            target = db.get(ForecastTarget, target_id)
            if not target or target.is_deleted:
                return {"success": False, "error": f"ForecastTarget {target_id!r} not found."}

            anchored = engine.compute_target_anchored(
                target_id, horizons, seasonal_period
            )
            if anchored is None:
                return {"success": False, "error": f"Compute failed for target {target_id!r}."}

            run = anchored["run"]
            raw_runs: list[dict] = []
            if run is not None:
                # Canonical per-horizon answers — same read-model the
                # dashboard uses, so a chat re-run quotes the numbers the
                # dashboard will show on next load.
                per_horizon = {str(h): format_run_answer(run, day=h) for h in horizons}
                headline_day = select_headline_day(horizons)
                headline = per_horizon[str(headline_day)]
                raw_runs = [
                    {
                        "target_id": run.target_id,
                        "horizon_days": headline_day,
                        "point_estimate": headline["point_estimate"],
                        "scenarios": headline["scenarios"],
                        "horizons": per_horizon,
                        "headline": headline,
                        "explanation": headline["explanation"],
                        "methodology": (run.model_detail or {}).get("metric", "ensemble"),
                        "confidence": run.confidence,
                        "below_naive_baseline": run.below_naive_baseline,
                        "as_of_date": run.as_of_date.isoformat() if run.as_of_date else None,
                        "model_detail": run.model_detail,
                    }
                ]

            gated = wrap_forecast_result(
                raw_runs=raw_runs,
                anchor_rows=anchored["anchor_rows"],
                source_table=anchored["source_table"],
                sample_size=anchored["sample_size"],
            )
            return gated

        if product_id:
            # Run all SKU targets for an ERP product family
            prefix = f"{product_id}."
            sku_targets = db.query(ForecastTarget).filter(
                ForecastTarget.product_key.like(f"{prefix}%"),
                ForecastTarget.org_id == org_id,
                ForecastTarget.is_deleted == False,  # noqa: E712
            ).all()
            if not sku_targets:
                return {"success": False, "error": f"No SKU targets found for product '{product_id}'."}
            results = []
            headline_day = select_headline_day(horizons)
            for tgt in sku_targets:
                anchored = engine.compute_target_anchored(
                    str(tgt.id), horizons, seasonal_period
                )
                if anchored and anchored.get("run"):
                    run = anchored["run"]
                    headline = format_run_answer(run, day=headline_day)
                    results.append({
                        "target_id": run.target_id,
                        "product_key": tgt.product_key,
                        "material_code": tgt.product_key.split(".")[-1] if "." in tgt.product_key else None,
                        "horizon_days": headline_day,
                        "point_estimate": headline["point_estimate"],
                        "scenarios": headline["scenarios"],
                        "confidence": run.confidence,
                        "below_naive_baseline": run.below_naive_baseline,
                    })
            return {"success": True, "runs": results, "count": len(results)}

        # bulk path — unchanged
        runs = engine.compute_all(org_id, horizons, seasonal_period)
        return {
            "success": True,
            "runs": [
                {
                    "target_id": r.target_id,
                    "below_naive_baseline": r.below_naive_baseline,
                    "confidence": r.confidence,
                    "as_of_date": r.as_of_date.isoformat() if r.as_of_date else None,
                    "model_detail": r.model_detail,
                }
                for r in runs
            ],
            "count": len(runs),
        }

    try:
        return await asyncio.to_thread(_do_work)
    except Exception as exc:
        logger.warning("forecast_run failed: %s", exc)
        return {"success": False, "error": f"Forecast run failed: {exc}"}


FORECAST_RUN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_run",
        "description": (
            "Execute the full forecasting pipeline (quality scoring → model "
            "fitting → ensemble blend → honesty gate → scenario generation) "
            "for one target or all active targets in the org. This is heavy "
            "ML compute — invoke it during nightly automation or when the "
            "user explicitly asks to re-run, NOT to answer read questions "
            "(use forecast_get / forecast_brief for those). The response "
            "carries a per-horizon 'horizons' map plus a 'headline' answer "
            "(7-day when requested) in the same canonical format the "
            "dashboard shows."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": (
                        "Forecast a specific target. Omit or set to null to "
                        "run all active targets for the org."
                    ),
                },
                "product_id": {
                    "type": "string",
                    "description": (
                        "ERP product family to run all SKU targets for "
                        "(e.g. 'product_a', 'product_b'). "
                        "Alternative to target_id for batch SKU runs."
                    ),
                },
                "horizons": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Forecast horizons in days (default [3,7,30]).",
                    "default": [3, 7, 30],
                },
                "seasonal_period": {
                    "type": "integer",
                    "description": "Expected seasonal cycle in days (default 7 for weekly).",
                    "default": 7,
                },
            },
            "required": [],
        },
    },
}


# ======================================================================
# forecast_list_skus  (lightweight read — SKU drill-down)
# ======================================================================

async def _forecast_list_skus(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """List all SKU-level forecast targets for an ERP product family.

    Required args:
        product_id  (str): The dashboard product_id (e.g. 'product_a',
                           'product_b').
    """
    product_id = (args.get("product_id") or "").strip().lower()
    if not product_id:
        return {"success": False, "error": "product_id is required."}

    org_id, _app_id = _resolve_org_context(context)
    prefix = f"{product_id}."

    targets = db.query(ForecastTarget).filter(
        ForecastTarget.product_key.like(f"{prefix}%"),
        ForecastTarget.org_id == org_id,
        ForecastTarget.is_deleted == False,  # noqa: E712
    ).order_by(ForecastTarget.report_order.asc()).all()

    if not targets:
        return {
            "success": True,
            "skus": [],
            "count": 0,
            "message": f"No SKU targets found for product '{product_id}'.",
        }

    return {
        "success": True,
        "skus": [_serialize_target(t) for t in targets],
        "count": len(targets),
    }


FORECAST_LIST_SKUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_list_skus",
        "description": (
            "List all SKU-level forecast targets for an ERP product family "
            "(product_a or product_b). Returns material_code, target_id, "
            "report_order, and whether each SKU is the primary (highest volume) "
            "for its family. Safe for the user-facing agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "string",
                    "description": (
                        "The dashboard product_id to list SKUs for. "
                        "Must be an ERP product: 'product_a' or 'product_b'."
                    ),
                },
            },
            "required": ["product_id"],
        },
    },
}


# ======================================================================
# forecast_get  (lightweight read)
# ======================================================================

async def _forecast_get(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Read the most recent cached forecast for a target.

    Required args:
        target_id  (str): The ForecastTarget id.
    Optional args:
        day        (int): Horizon for the canonical answer block (default 7,
                          matching the dashboard's "next week" card).
    """
    target_id = args.get("target_id")
    if not target_id:
        return {"success": False, "error": "target_id is required."}
    day = int(args.get("day", 7))

    from app.services.forecasting.answer_format import format_run_answer

    engine = _forecast_engine(db)
    run = engine.get_latest_run(target_id)

    if run is None:
        return {"success": False, "error": f"No forecast found for target {target_id!r}."}

    return {
        "success": True,
        # Legacy raw shape (full results dict, all horizons).
        "forecast": {
            "results": run.results,
            "below_naive_baseline": run.below_naive_baseline,
            "confidence": run.confidence,
            "as_of_date": run.as_of_date.isoformat() if run.as_of_date else None,
            "model_detail": run.model_detail,
        },
        # Canonical read-model for the requested horizon — the exact number
        # and stored explanation the dashboard shows. Quote this verbatim.
        "answer": format_run_answer(run, day=day),
    }


FORECAST_GET_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_get",
        "description": (
            "Read the most recent cached forecast for a target. Returns the "
            "raw point-forecast results plus a canonical 'answer' block for "
            "the requested horizon (default 7 days = 'next week'): "
            "answer.point_estimate is the exact number the market dashboard "
            "shows for this product + horizon, and answer.explanation is the "
            "stored forecast rationale. Quote these verbatim — never round, "
            "average horizons, or re-derive values. Safe for the user-facing agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "The ForecastTarget id to read.",
                },
                "day": {
                    "type": "integer",
                    "description": (
                        "Forecast horizon for the canonical answer block "
                        "(default 7 = 'next week'; use 3 or 30 to match the "
                        "user's requested period)."
                    ),
                    "default": 7,
                },
            },
            "required": ["target_id"],
        },
    },
}


# ======================================================================
# forecast_accuracy  (lightweight read)
# ======================================================================

async def _forecast_accuracy(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Read backtest accuracy metrics for a target.

    Required args:
        target_id  (str): The ForecastTarget id.
    """
    target_id = args.get("target_id")
    if not target_id:
        return {"success": False, "error": "target_id is required."}

    engine = _forecast_engine(db)
    logs = engine.get_accuracy(target_id)

    return {"success": True, "accuracy_log": logs, "count": len(logs)}


FORECAST_ACCURACY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_accuracy",
        "description": (
            "Read backtest accuracy metrics for a forecast target. Returns "
            "per-horizon MAPE, naive-baseline comparison, skill score, and "
            "per-model MAPE breakdown. Safe for the user-facing agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "The ForecastTarget id to read accuracy for.",
                },
            },
            "required": ["target_id"],
        },
    },
}


# ======================================================================
# forecast_rules  (CRUD — lightweight)
# ======================================================================

async def _forecast_rules(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Manage business rules for a target (or global guardrails).

    Required args:
        action  (str): One of ``list``, ``propose``, ``activate``, ``pause``.

    Optional args (by action):
        list:       target_id (optional — null for global guardrails)
        propose:    rule_type (seasonal/causal_driver/event_override/guardrail),
                    params (JSON dict), target_id (optional)
        activate:   rule_id (str)
        pause:      rule_id (str)
    """
    action = args.get("action", "list")
    target_id = args.get("target_id")
    org_id, app_id = _resolve_org_context(context)

    try:
        # ── list ──────────────────────────────────────────────
        if action == "list":
            query = db.query(ForecastBusinessRule).filter(
                ForecastBusinessRule.org_id == org_id,
                ForecastBusinessRule.is_deleted == False,
            )
            if target_id is not None:
                query = query.filter(ForecastBusinessRule.target_id == target_id)
            rules = query.order_by(ForecastBusinessRule.created_date.desc()).all()
            return {
                "success": True,
                "rules": [_serialize_rule(r) for r in rules],
                "count": len(rules),
            }

        # ── propose ───────────────────────────────────────────
        if action == "propose":
            rule_type = args.get("rule_type")
            params = args.get("params")

            if not rule_type:
                return {"success": False, "error": "rule_type is required for propose."}
            if rule_type not in ("seasonal", "causal_driver", "event_override", "guardrail"):
                return {
                    "success": False,
                    "error": (
                        f"Unknown rule_type {rule_type!r}. "
                        "Must be seasonal, causal_driver, event_override, or guardrail."
                    ),
                }
            if not isinstance(params, dict):
                return {"success": False, "error": "params must be a JSON object."}

            rule = ForecastBusinessRule(
                org_id=org_id,
                app_id=app_id,
                target_id=target_id,  # nullable for global guardrails
                rule_type=rule_type,
                params=params,
                status="proposed",
                source="chat",
            )
            db.add(rule)
            db.commit()
            db.refresh(rule)
            return {"success": True, "rule": _serialize_rule(rule), "status": "proposed"}

        # ── activate ──────────────────────────────────────────
        if action == "activate":
            rule_id = args.get("rule_id")
            if not rule_id:
                return {"success": False, "error": "rule_id is required for activate."}

            rule = db.get(ForecastBusinessRule, rule_id)
            if not rule or rule.is_deleted:
                return {"success": False, "error": f"Rule {rule_id!r} not found."}

            rule.status = "active"
            rule.approved_by_id = user_id
            rule.approved_at = datetime.now(timezone.utc)
            db.commit()
            return {"success": True, "rule": _serialize_rule(rule), "status": "active"}

        # ── pause ─────────────────────────────────────────────
        if action == "pause":
            rule_id = args.get("rule_id")
            if not rule_id:
                return {"success": False, "error": "rule_id is required for pause."}

            rule = db.get(ForecastBusinessRule, rule_id)
            if not rule or rule.is_deleted:
                return {"success": False, "error": f"Rule {rule_id!r} not found."}

            rule.status = "paused"
            db.commit()
            return {"success": True, "rule": _serialize_rule(rule), "status": "paused"}

        # ── unknown ───────────────────────────────────────────
        return {
            "success": False,
            "error": f"Unknown action {action!r}. Must be list, propose, activate, or pause.",
        }

    except Exception as exc:
        logger.warning("forecast_rules failed (action=%s): %s", action, exc)
        db.rollback()
        return {"success": False, "error": f"Rules operation failed: {exc}"}


FORECAST_RULES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_rules",
        "description": (
            "Manage business rules for forecasting targets. "
            "Supports listing rules, proposing new rules (from chat), "
            "activating proposed rules, and pausing active rules. "
            "Rules can be target-specific (seasonal/causal/event) or "
            "global guardrails (null target_id). Safe for the user-facing agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "propose", "activate", "pause"],
                    "description": "Operation: list rules, propose a new rule, activate/approve a proposed rule, or pause an active rule.",
                },
                "target_id": {
                    "type": "string",
                    "description": "ForecastTarget id for target-specific rules. Omit or set to null for global guardrails.",
                },
                "rule_type": {
                    "type": "string",
                    "enum": ["seasonal", "causal_driver", "event_override", "guardrail"],
                    "description": "Rule type (required for propose): seasonal, causal_driver, event_override, guardrail.",
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Rule parameters as a JSON object (required for propose). "
                        "seasonal: {month, adjustment_pct}. "
                        "causal_driver: {driver, elasticity}. "
                        "event_override: {event, month, adjustment_pct}. "
                        "guardrail: {min_history, max_mape}."
                    ),
                },
                "rule_id": {
                    "type": "string",
                    "description": "Rule id (required for activate and pause).",
                },
            },
            "required": ["action"],
        },
    },
}


# ======================================================================
# forecast_report  (lightweight read — weekly report assembler)
# ======================================================================

async def _forecast_report(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Generate or retrieve the weekly forecast report.

    Reads cached ForecastRun rows for all targets where
    ``include_in_weekly_report=True`` and assembles a structured markdown
    brief.  Pure read — no ML, no LLM.

    Optional args:
        action        (str): ``"generate"`` (default) to build from cache,
                             ``"get"`` to retrieve the last saved artifact.
        save_artifact (bool): If True, persist the report as an ``"md"``
                             artifact via ArtifactService (default False).
    """
    action = args.get("action", "generate")
    save_artifact = args.get("save_artifact", False)
    org_id, app_id = _resolve_org_context(context)

    try:
        if action == "generate":
            gen = WeeklyReportGenerator(db)
            report = gen.generate(org_id, app_id)

            response: dict = {
                "success": True,
                "report": {
                    "markdown": report.markdown,
                    "summary": report.summary,
                    "products_count": len(report.products),
                    "generated_at": report.generated_at.isoformat(timespec="seconds"),
                    "as_of_date": report.as_of_date.isoformat() if report.as_of_date else None,
                },
            }

            # ── Optional artifact persistence ──────────────────
            if save_artifact:
                try:
                    svc = ArtifactService(db)
                    title = (
                        f"Weekly Forecast Brief — "
                        f"{report.as_of_date.strftime('%Y-%m-%d') if report.as_of_date else report.generated_at.strftime('%Y-%m-%d')}"
                    )
                    artifact = svc.create_artifact(
                        artifact_type="md",
                        title=title,
                        description=report.markdown,
                        org_id=org_id,
                        app_id=app_id,
                    )
                    response["artifact_id"] = artifact.id
                except Exception as art_exc:
                    logger.warning("forecast_report artifact save failed: %s", art_exc)
                    response["artifact_warning"] = f"Artifact save failed (report still generated): {art_exc}"

            return response

        if action == "get":
            # Retrieve last saved artifact — stub for future use.
            # For now we return a message telling the user to generate.
            return {
                "success": True,
                "report": None,
                "message": (
                    "No previously saved report found. "
                    "Use action='generate' to build a fresh weekly report."
                ),
            }

        return {
            "success": False,
            "error": f"Unknown action {action!r}. Must be 'generate' or 'get'.",
        }

    except Exception as exc:
        logger.warning("forecast_report failed (action=%s): %s", action, exc)
        return {"success": False, "error": f"Report generation failed: {exc}"}


FORECAST_REPORT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_report",
        "description": (
            "Generate or retrieve the weekly forecast brief. "
            "Assembles cached forecasts for all products marked "
            "include_in_weekly_report into a structured markdown "
            "report with executive summary, per-product scenario "
            "tables, honesty-gate flags, and accuracy metrics. "
            "Pure read — safe for the user-facing agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["generate", "get"],
                    "description": "Generate a fresh report from cached forecasts, or get the last saved artifact.",
                    "default": "generate",
                },
                "save_artifact": {
                    "type": "boolean",
                    "description": "If true, persist the report as an 'md' artifact via ArtifactService (default false).",
                    "default": False,
                },
            },
            "required": [],
        },
    },
}


# ======================================================================
# forecast_ppt  (lightweight read — PPT deck from cached forecasts)
# ======================================================================

async def _forecast_ppt(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Generate a forecast PPT deck from cached forecast data.

    Reads from ForecastRun cache (via WeeklyReportGenerator), assembles
    a ReportCardPayload, and renders via the existing create_artifact flow.
    Pure read — no ML, no LLM.

    Optional args:
        target_id    (str): Specific product to feature in the scenario chart.
                            Falls back to the first product with a forecast.
        horizon      (str): "3", "7", or "30" (default "7").
        save_artifact (bool): If True (default), persist and render the .pptx.
                              If False, return the payload dict only.
    """
    target_id: str | None = args.get("target_id")
    horizon: str = args.get("horizon", "7")
    save_artifact: bool = args.get("save_artifact", True)
    org_id, app_id = _resolve_org_context(context)

    try:
        # 1. Generate the WeeklyReport
        report_gen = WeeklyReportGenerator(db)
        report = report_gen.generate(org_id, app_id)

        # 2. Assemble the ReportCardPayload
        assembler = ForecastPayloadAssembler(db)
        payload = assembler.assemble(
            report, org_id=org_id, target_id=target_id, horizon=horizon
        )

        # 3. Delegate to the existing create_artifact flow for PPTX rendering
        if save_artifact:
            result = await _create_artifact_tool(
                args={
                    "type": "pptx",
                    "title": payload.title,
                    "payload": payload.model_dump(),
                },
                db=db,
                user_id=user_id,
                context=context,
            )
            return result

        # Return payload dict without persisting
        return {
            "success": True,
            "title": payload.title,
            "mode": "payload_only",
            "payload": payload.model_dump(),
        }

    except Exception as exc:
        logger.warning("forecast_ppt failed: %s", exc)
        return {"success": False, "error": f"PPT generation failed: {exc}"}


FORECAST_PPT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_ppt",
        "description": (
            "Generate a themed PowerPoint deck from cached forecast data. "
            "Reads WeeklyReport, assembles a ReportCardPayload, and renders "
            "a slide deck with scenario charts, KPI tiles, forecast tables, "
            "accuracy metrics, and honesty-gate warnings. Returns artifact_id, "
            "file_url, and preview_url for the generated .pptx file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": (
                        "Optional specific product ID to feature in the scenario chart. "
                        "If omitted, the first product with a forecast is used."
                    ),
                },
                "horizon": {
                    "type": "string",
                    "enum": ["3", "7", "30"],
                    "default": "7",
                    "description": "Forecast horizon to chart (3, 7, or 30 days).",
                },
                "save_artifact": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Save the generated .pptx as an artifact. "
                        "When False, returns the payload without persisting."
                    ),
                },
            },
        },
    },
}


# ======================================================================
# forecast_brief  (lightweight read — evidence-grounded analyst brief)
# ======================================================================

async def _forecast_brief(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Return the cached evidence-grounded analyst brief for a product.

    Required args:
        product_id  (str): Dashboard product_id (e.g. 'product_a', 'product_b').
    Optional args:
        day         (int): Forecast horizon in days (default 7).
    """
    product_id = (args.get("product_id") or "").strip().lower()
    if not product_id:
        return {"success": False, "error": "product_id is required."}
    day = int(args.get("day", 7))

    def _do_work():
        from app.services.forecasting.analyst import service as analyst_service

        return analyst_service.get_analyst_brief(product_id, day=day, db=db)

    try:
        brief = await asyncio.to_thread(_do_work)
    except Exception as exc:
        logger.warning("forecast_brief failed (%s): %s", product_id, exc)
        return {"success": False, "error": f"Brief generation failed: {exc}"}
    if brief is None:
        return {"success": False,
                "error": f"No forecast data or brief available for '{product_id}'."}
    return {"success": True, "product_id": product_id, "day": day, "brief": brief}


FORECAST_BRIEF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_brief",
        "description": (
            "Get the evidence-grounded AI analyst brief for a dashboard product "
            "(e.g. 'product_a', 'product_b'): why the price is "
            "forecast to move (upstream transmission, trend, seasonality, model "
            "drivers), why the current buy/hold/sell/watch recommendation, and "
            "what triggers would change it. Use this to answer 'why' questions "
            "about the forecast. Safe read-only tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string",
                               "description": "Dashboard product_id, e.g. 'product_a'."},
                "day": {"type": "integer", "default": 7,
                        "description": "Forecast horizon in days (default 7)."},
            },
            "required": ["product_id"],
        },
    },
}


async def _forecast_what_if(args, db, user_id, context=None):
    """Simulate a forecast under upstream price shocks (market index / root feedstock).

    Mirrors ``app.routers.forecast_ops.get_what_if_simulation`` using the
    shared causal-chain elasticity math (``what_if.compute_what_if``) — no
    model re-running.
    """
    product_id = (args.get("product_id") or "").strip().lower()
    if not product_id:
        return {"success": False, "error": "no forecast target for ''"}

    market_arg = args.get("market_delta_pct")
    feedstock_arg = args.get("feedstock_delta_pct")
    try:
        market_delta_pct = float(market_arg) if market_arg is not None else 0.0
        feedstock_delta_pct = float(feedstock_arg) if feedstock_arg is not None else 0.0
    except (TypeError, ValueError) as exc:
        return {"success": False, "error": f"simulation failed: {exc}"}

    # Only reject when the caller explicitly passed zero shocks; a bare
    # product_id lookup (no delta keys) still resolves the target first.
    if market_delta_pct == 0 and feedstock_delta_pct == 0 and (
        market_arg is not None or feedstock_arg is not None
    ):
        return {"success": False,
                "error": "specify at least one price shock (market_delta_pct or feedstock_delta_pct)"}

    org_id, _app_id = _resolve_org_context(context)

    def _do_sim() -> dict:
        target = db.query(ForecastTarget).filter(
            ForecastTarget.product_key.like(f"{product_id}.%"),
            ForecastTarget.org_id == org_id,
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).order_by(ForecastTarget.report_order.asc()).first()
        if target is None:
            return {"success": False, "error": f"no forecast target for '{product_id}'"}

        try:
            result = compute_what_if(target.product_key, market_delta_pct,
                                     feedstock_delta_pct, db)
        except LookupError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("forecast_what_if failed (%s): %s", product_id, exc)
            return {"success": False, "error": f"simulation failed: {exc}"}

        adjusted = result.get("adjusted_forecast") or []
        if not adjusted:
            return {"success": False,
                    "error": "No forecast available for simulation"}

        base = result.get("base_forecast") or []
        adjustments = result.get("adjustments") or []

        # Drivers — always surface both market + feedstock rows (zero → "no shock")
        by_driver = {a.get("driver"): a for a in adjustments if a.get("driver")}
        drivers = []
        for driver, delta in (("market_index", market_delta_pct),
                              ("feedstock", feedstock_delta_pct)):
            entry = by_driver.get(driver)
            if entry is None:
                drivers.append({
                    "driver": driver,
                    "delta_pct": round(delta, 2),
                    "impact_pct": 0.0,
                    "note": "no shock" if delta == 0 else "shock not modeled",
                })
            else:
                drivers.append({
                    "driver": driver,
                    "delta_pct": entry.get("delta_pct", 0.0),
                    "impact_pct": entry.get("impact_pct", 0.0),
                    "note": entry.get("description") or f"{driver} shock",
                })

        # Horizon table — first 7 base/adjusted points
        horizon_table = []
        for i, b in enumerate(base[:7], start=1):
            a = adjusted[i - 1] if i - 1 < len(adjusted) else b
            delta_pct = round((a - b) / b * 100, 2) if b else None
            horizon_table.append({"day": i, "base": b,
                                  "adjusted": a, "delta_pct": delta_pct})

        total_impact_pct = result.get("total_impact_pct", 0.0)
        head = horizon_table[-1] if horizon_table else None
        active = [d for d in drivers if d["delta_pct"] not in (0, 0.0)]
        shock_text = " + ".join(
            f"{d['driver']} {d['delta_pct']:+.1f}%" for d in active
        )
        if head:
            narration_hint = (
                f"{shock_text} -> {product_id} {total_impact_pct:+.2f}% on day 7 "
                f"({head['base']:.2f} -> {head['adjusted']:.2f})."
            )
        else:
            narration_hint = f"{shock_text} -> {product_id} {total_impact_pct:+.2f}%."

        return {
            "success": True,
            "product_id": product_id,
            "product_key": target.product_key,
            "base_forecast": base,
            "adjusted_forecast": adjusted,
            "total_impact_pct": total_impact_pct,
            "drivers": drivers,
            "horizon_table": horizon_table,
            "narration_hint": narration_hint,
        }

    try:
        return await asyncio.to_thread(_do_sim)
    except Exception as exc:
        logger.warning("forecast_what_if failed (%s): %s", product_id, exc)
        return {"success": False, "error": f"simulation failed: {exc}"}


FORECAST_WHAT_IF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "forecast_what_if",
        "description": (
            "Simulate how a dashboard product's (e.g. 'product_a', 'product_b') "
            "forecast changes under upstream price shocks. "
            "Provide percentage changes for the market index (market_delta_pct) and/or "
            "root feedstock (feedstock_delta_pct); at least one must be non-zero. "
            "Use this for 'what if' / scenario questions about the forecast. "
            "Safe read-only tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string",
                               "description": "Dashboard product_id, e.g. 'product_a'."},
                "market_delta_pct": {"type": "number", "default": 0,
                                   "description": "Percentage change in the market index (e.g. 5.0 = +5%)."},
                "feedstock_delta_pct": {"type": "number", "default": 0,
                                      "description": "Percentage change in the root feedstock price (e.g. -3.0 = -3%)."},
            },
            "required": ["product_id"],
        },
    },
}


# ======================================================================
# Registration
# ======================================================================

for _name, _schema, _handler, _enabled, _desc in (
    (
        "forecast_discover",
        FORECAST_DISCOVER_SCHEMA,
        _forecast_discover,
        True,
        "Scan a data source for forecastable time series and register targets.",
    ),
    (
        "forecast_run",
        FORECAST_RUN_SCHEMA,
        _forecast_run,
        True,
        "Run the forecasting pipeline for one or all active targets (heavy ML).",
    ),
    (
        "forecast_get",
        FORECAST_GET_SCHEMA,
        _forecast_get,
        True,
        "Read the most recent cached forecast for a target.",
    ),
    (
        "forecast_accuracy",
        FORECAST_ACCURACY_SCHEMA,
        _forecast_accuracy,
        True,
        "Read backtest accuracy metrics for a target.",
    ),
    (
        "forecast_rules",
        FORECAST_RULES_SCHEMA,
        _forecast_rules,
        True,
        "List / propose / activate / pause business rules for forecast targets.",
    ),
    (
        "forecast_report",
        FORECAST_REPORT_SCHEMA,
        _forecast_report,
        True,
        "Generate or retrieve the weekly forecast brief from cached forecasts.",
    ),
    (
        "forecast_ppt",
        FORECAST_PPT_SCHEMA,
        _forecast_ppt,
        True,
        "Generate a themed PowerPoint deck from cached forecast data with scenario charts and KPI tiles.",
    ),
    (
        "forecast_list_skus",
        FORECAST_LIST_SKUS_SCHEMA,
        _forecast_list_skus,
        True,
        "List all SKU-level forecast targets for an ERP product family.",
    ),
    (
        "forecast_brief",
        FORECAST_BRIEF_SCHEMA,
        _forecast_brief,
        True,
        "Get the evidence-grounded AI analyst brief for a product.",
    ),
    (
        "forecast_what_if",
        FORECAST_WHAT_IF_SCHEMA,
        _forecast_what_if,
        True,
        "Simulate a forecast under upstream price shocks (market index / root feedstock) for scenario analysis",
    ),
):
    registry.register(
        name=_name,
        schema=_schema,
        handler=_handler,
        category="forecasting",
        enabled_by_default=_enabled,
        description=_desc,
    )
