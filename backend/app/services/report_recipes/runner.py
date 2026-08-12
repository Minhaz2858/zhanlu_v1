"""Report Recipe Runner — deterministic recipe execution.

Executes a recipe's SQL bundle (or resolved metrics) via QueryService
(read-only, direct), enforces validation rules, and assembles sections.
The LLM NEVER decides what SQL to run — recipes are data, not code.

Soft-fail: any execution error returns a structured failure result;
validation failures return success=False with details.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.report_recipe import ReportRecipe
from app.services.db.query_service import QueryService

logger = logging.getLogger(__name__)


def run_recipe(
    db: Session,
    recipe: ReportRecipe,
    *,
    kb_id: str | None = None,
) -> dict[str, Any]:
    """Execute a report recipe and return assembled sections.

    Returns:
        {
            "success": bool,
            "sections": [{title, data, source_key}],
            "validation_results": [{rule, passed, detail}],
            "charts": [...],  # if chart specs present (rendered by caller)
            "error": str | None,
        }
    """
    try:
        # ── 1. Collect SQL to execute ──
        sql_entries = _resolve_sql(db, recipe, kb_id)
        if not sql_entries:
            return {
                "success": False,
                "sections": [],
                "validation_results": [],
                "charts": recipe.charts or [],
                "error": "No sql_bundle or resolvable metrics found for this recipe",
            }

        # ── 2. Execute SQL via QueryService (read-only) ──
        qs = QueryService(db)
        results: dict[str, list[dict]] = {}
        for entry in sql_entries:
            key = entry["key"]
            sql = entry["sql"]
            entry_kb = entry.get("kb_id") or kb_id
            if not entry_kb:
                results[key] = []
                continue
            try:
                res = qs.execute(entry_kb, sql, max_rows=500)
                results[key] = res.get("rows", [])
            except Exception as e:
                logger.warning("recipe '%s': SQL '%s' failed: %s", recipe.name, key, e)
                return {
                    "success": False,
                    "sections": [],
                    "validation_results": [],
                    "charts": recipe.charts or [],
                    "error": f"SQL execution failed for '{key}': {e}",
                }

        # ── 3. Validation rules ──
        validation_results = _run_validation_rules(recipe, results)
        all_passed = all(v["passed"] for v in validation_results)

        # ── 4. Assemble sections ──
        sections = _assemble_sections(recipe, results)

        return {
            "success": all_passed,
            "sections": sections,
            "validation_results": validation_results,
            "charts": recipe.charts or [],
            "error": None if all_passed else "Validation rules failed",
        }
    except Exception as e:
        logger.exception("recipe '%s': runner failed", recipe.name)
        return {
            "success": False,
            "sections": [],
            "validation_results": [],
            "charts": [],
            "error": str(e),
        }


def _resolve_sql(
    db: Session, recipe: ReportRecipe, kb_id: str | None
) -> list[dict[str, str]]:
    """Collect SQL entries from sql_bundle or required_metrics."""
    entries: list[dict[str, str]] = []

    # Direct SQL bundle takes precedence
    if recipe.sql_bundle:
        for item in recipe.sql_bundle:
            sql = item.get("sql", "")
            if sql:
                entries.append({
                    "key": item.get("key", f"query_{len(entries)}"),
                    "sql": sql,
                    "kb_id": item.get("kb_id"),
                })
        return entries

    # Resolve via MetricDefinition
    if recipe.required_metrics:
        from app.models.metric_definition import MetricDefinition

        for metric_name in recipe.required_metrics:
            q = db.query(MetricDefinition).filter(
                MetricDefinition.name == metric_name,
                MetricDefinition.is_deleted == False,  # noqa: E712
            )
            if kb_id:
                q = q.filter(MetricDefinition.datasource_id == kb_id)
            md = q.first()
            if md and md.base_sql:
                entries.append({
                    "key": metric_name,
                    "sql": md.base_sql,
                    "kb_id": md.datasource_id,
                })
            else:
                logger.debug(
                    "recipe '%s': metric '%s' not found or has no base_sql",
                    recipe.name, metric_name,
                )

    return entries


def _run_validation_rules(
    recipe: ReportRecipe, results: dict[str, list[dict]]
) -> list[dict[str, Any]]:
    """Enforce post-execution validation rules."""
    validations: list[dict[str, Any]] = []
    for rule in (recipe.validation_rules or []):
        rule_name = rule.get("rule", "")
        source_key = rule.get("source_key", "")
        data = results.get(source_key, [])
        passed = True
        detail = ""

        if rule_name == "non_empty":
            passed = len(data) > 0
            detail = f"{len(data)} rows" if passed else "0 rows"
        elif rule_name == "min_rows":
            min_count = rule.get("params", {}).get("min", 1)
            passed = len(data) >= min_count
            detail = f"{len(data)} rows (min {min_count})"
        elif rule_name == "max_rows":
            max_count = rule.get("params", {}).get("max", 10000)
            passed = len(data) <= max_count
            detail = f"{len(data)} rows (max {max_count})"
        else:
            # Unknown rule — pass by default (extensible)
            passed = True
            detail = f"unknown rule '{rule_name}' — skipped"

        validations.append({
            "rule": rule_name,
            "source_key": source_key,
            "passed": passed,
            "detail": detail,
        })
    return validations


def _assemble_sections(
    recipe: ReportRecipe, results: dict[str, list[dict]]
) -> list[dict[str, Any]]:
    """Build output sections from execution results."""
    sections: list[dict[str, Any]] = []
    for section in (recipe.sections or []):
        title = section.get("title", "")
        source_key = section.get("source_key", "")
        template = section.get("template", "")
        data = results.get(source_key, [])
        sections.append({
            "title": title,
            "source_key": source_key,
            "data": data,
            "template": template,
        })
    return sections


def list_recipes(
    db: Session,
    *,
    project_id: str | None = None,
    include_global: bool = True,
) -> list[ReportRecipe]:
    """List enabled recipes for a project (and optionally global ones)."""
    from sqlalchemy import or_

    q = db.query(ReportRecipe).filter(
        ReportRecipe.is_enabled == True,  # noqa: E712
        ReportRecipe.is_deleted == False,  # noqa: E712
    )
    if project_id:
        if include_global:
            q = q.filter(
                or_(
                    ReportRecipe.project_id == project_id,
                    ReportRecipe.project_id.is_(None),
                )
            )
        else:
            q = q.filter(ReportRecipe.project_id == project_id)
    else:
        q = q.filter(ReportRecipe.project_id.is_(None))
    return q.order_by(ReportRecipe.name).all()


def get_recipe(db: Session, name: str) -> ReportRecipe | None:
    """Fetch a single recipe by name."""
    return (
        db.query(ReportRecipe)
        .filter(
            ReportRecipe.name == name,
            ReportRecipe.is_deleted == False,  # noqa: E712
        )
        .first()
    )
