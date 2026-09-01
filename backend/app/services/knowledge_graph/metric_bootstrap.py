"""Business Semantic Layer — project_metric bootstrap.

LLM-proposes curable per-project business metrics from the structural catalog.
Proposals always land as ``status='proposed'`` — a human must approve them via
the Data Map Metrics tab before they are injected into NL2SQL prompts. Domain
content (SQL patterns, aliases, bindings) lives here as *data*, never in global
prompts (platform convention: prompts stay domain-free).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_METRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "definition": {"type": "string"},
                    "sql_expression": {"type": "string"},
                    "query_pattern": {"type": "string"},
                    "unit": {"type": "string"},
                    "default_aggregation": {
                        "type": "string",
                        "enum": ["sum", "avg", "max", "min", "count"],
                    },
                    "bindings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "string"},
                                "measure_columns": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "date_column": {"type": "string"},
                                "dimensions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "required": ["name", "definition", "sql_expression"],
            },
        }
    },
    "required": ["metrics"],
}

_SYSTEM_PROMPT = (
    "You are a business-metrics analyst for a CEO-facing BI assistant. Given a "
    "data catalog, propose a small set of high-value business metrics (KPI) the "
    "CEO is likely to ask about. For each metric provide: a canonical name; a "
    "list of aliases (including common Chinese and English variants); a one-line "
    "business definition; a canonical SQL expression; a reusable SQL query "
    "pattern; a unit; a default aggregation (sum/avg/max/min/count); and the "
    "table binding (table, measure columns, date column, dimensions).\n"
    "Be conservative: propose only metrics whose columns actually exist in the "
    "catalog. Return at most 8 metrics. Do not invent columns or tables."
)


def _build_prompt(tables: list[dict]) -> str:
    lines: list[str] = ["Catalog tables:"]
    for t in tables:
        cols = ", ".join(
            f"{c.get('column_name')} ({c.get('data_type')})" for c in t.get("columns", [])
        )
        lines.append(f"- {t.get('table_name')}: {cols}")
    return "\n".join(lines)


async def bootstrap_project_metrics(
    db: Session,
    project_id: str,
    kb_id: str,
    tables: list[dict],
) -> list[dict]:
    """LLM-propose ``project_metric`` rows; persist as ``proposed``.

    Returns the list of created dicts (id + name). Never approves. Idempotent
    per name+project (unique constraint) — existing names are skipped. Flag
    ``KG_METRIC_BOOTSTRAP_ENABLED`` gates the call at the indexer; this function
    assumes the gate has already been checked but re-checks defensively.
    """
    from app.config import settings

    if not settings.KG_METRIC_BOOTSTRAP_ENABLED:
        return []
    if not tables:
        return []

    from app.services.llm_service import call_llm
    from app.models.knowledge_catalog import ProjectMetric

    try:
        result = await call_llm(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_prompt(tables)},
            ],
            temperature=0.2,
            response_json_schema=_METRIC_SCHEMA,
            task_type="metric_bootstrap",
        )
    except Exception:
        logger.exception("metric_bootstrap: LLM call failed — no metrics proposed")
        return []

    data = result.get("data") if isinstance(result, dict) else {}
    if isinstance(data, list):
        metrics = data
    else:
        metrics = (data or {}).get("metrics", [])

    existing = {
        m.name
        for m in db.query(ProjectMetric).filter(
            ProjectMetric.project_id == project_id,
            ProjectMetric.is_deleted.is_(False),
        ).all()
    }

    created: list[dict] = []
    for m in metrics[:8]:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or "").strip()
        if not name or name in existing:
            continue
        row = ProjectMetric(
            id=str(uuid.uuid4()),
            project_id=project_id,
            kb_id=kb_id,
            name=name,
            aliases=m.get("aliases") or [],
            definition=m.get("definition"),
            sql_expression=m.get("sql_expression"),
            query_pattern=m.get("query_pattern"),
            unit=m.get("unit"),
            default_aggregation=m.get("default_aggregation"),
            bindings=m.get("bindings") or [],
            source="llm",
            status="proposed",  # human approval required
        )
        db.add(row)
        existing.add(name)
        created.append({"id": row.id, "name": name})

    if created:
        db.commit()
        logger.info(
            "metric_bootstrap: project=%s kb=%s proposed %d metrics",
            project_id, kb_id, len(created),
        )
    return created


def _metric_to_dict(m: Any) -> dict:
    """Serialize a ProjectMetric row for API responses."""
    return {
        "id": m.id,
        "project_id": m.project_id,
        "kb_id": m.kb_id,
        "name": m.name,
        "aliases": m.aliases or [],
        "definition": m.definition,
        "sql_expression": m.sql_expression,
        "query_pattern": m.query_pattern,
        "unit": m.unit,
        "default_aggregation": m.default_aggregation,
        "bindings": m.bindings or [],
        "source": m.source,
        "status": m.status,
        "created_date": m.created_date.isoformat() if m.created_date else None,
    }
