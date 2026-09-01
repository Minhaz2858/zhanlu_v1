"""NL2SQL orchestrator — the main entry point for the governed NL2SQL pipeline.

Pipeline::

    ask(question, binding_id)
      → semantic_resolver.resolve (metric + mapping match)
      → validator.validate (sqlglot + allow-list)
      → adapter.explain (cost estimation)
      → policy.evaluate (allow-list / cost / row-limit)
      → adapter.query (row-limit + watchdog)
      → audit_events.write
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.services.datasources import DatasourceAdapter, QueryResult
from app.services.datasources.sqlite_adapter import SQLiteAdapter
from app.services.datasources.postgres_adapter import PostgresAdapter
from app.services.datasources.mysql_adapter import MySQLAdapter
from app.services.nl2sql.semantic_resolver import resolve, ResolvedIntent
from app.services.nl2sql.validator import validate, ValidationResult
from app.services.nl2sql.policy import evaluate, PolicyConfig, PolicyDecision

logger = logging.getLogger(__name__)


# ── lightweight validation facade (shadow mode / recipe checks) ──────────


def validate_sql(
    sql: str,
    *,
    dialect: str | None = None,
    datasource_id: str | None = None,
    allowed_tables: list[str] | None = None,
    allowed_columns: list[str] | None = None,
    block_tables: list[str] | None = None,
) -> dict[str, Any]:
    """Run governance validation + default policy on a SQL statement.

    This is the single entry point for callers that already have SQL
    (shadow-mode wiring, report-recipe checks) and only need a verdict —
    no adapter, no EXPLAIN, no execution. Never raises; on unexpected
    internal errors returns ``{"is_valid": False, "error": ...}`` so the
    caller can treat the statement as unverifiable.
    """
    try:
        v = validate(
            sql,
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            block_tables=block_tables,
        )
        p = evaluate(
            v.tables_referenced,
            0.0,  # no EXPLAIN in this path — allow/block lists + row limit only
            policy_config=PolicyConfig(
                allowed_tables=allowed_tables or [],
                allowed_columns=allowed_columns or [],
                block_tables=block_tables or [],
            ),
        )
        return {
            "is_valid": v.is_valid,
            "errors": list(v.errors),
            "warnings": list(v.warnings),
            "tables_referenced": list(v.tables_referenced),
            "sql_hash": v.sql_hash,
            "policy_allowed": p.allowed,
            "policy_reason": p.reason,
        }
    except Exception as e:  # never raise into the served path
        logger.debug("validate_sql failed (treated as unverifiable): %s", e)
        return {
            "is_valid": False,
            "errors": [f"validation error: {e}"],
            "warnings": [],
            "tables_referenced": [],
            "sql_hash": "",
            "policy_allowed": False,
            "policy_reason": "validation error",
        }


@dataclass
class NL2SQLResult:
    success: bool
    question: str
    sql: str = ""
    data: QueryResult | None = None
    intent: ResolvedIntent | None = None
    validation: ValidationResult | None = None
    policy: PolicyDecision | None = None
    error: str = ""
    duration_ms: float = 0.0
    needs_clarification: list[dict[str, Any]] | None = None
    explanation: str = ""


# ── adapter factory ───────────────────────────────────────────────────


def _build_adapter(ds_config: dict[str, Any]) -> DatasourceAdapter:
    """Build the appropriate adapter from a datasource config dict."""
    dialect = ds_config.get("dialect", "sqlite").lower()
    if dialect == "postgres" or dialect == "postgresql":
        return PostgresAdapter(
            host=ds_config.get("host", "localhost"),
            port=int(ds_config.get("port", 5432)),
            dbname=ds_config.get("database", ds_config.get("dbname", "zhanlu")),
            user=ds_config.get("username", ds_config.get("user", "zhanlu")),
            password=ds_config.get("password", ""),
            timeout_ms=int(ds_config.get("timeout_ms", 5000)),
        )
    if dialect == "mysql":
        return MySQLAdapter(
            kb=ds_config.get("kb"),
            timeout_ms=int(ds_config.get("timeout_ms", 5000)),
        )
    # Default: SQLite
    return SQLiteAdapter(
        db_path=ds_config.get("path", ds_config.get("db_path", ":memory:")),
        timeout_ms=int(ds_config.get("timeout_ms", 5000)),
    )


# ── main pipeline ─────────────────────────────────────────────────────


def ask(
    question: str,
    *,
    binding_id: str,
    db: Session,
    datasource_config: dict[str, Any] | None = None,
    metrics: list[dict[str, Any]] | None = None,
    mappings: list[dict[str, Any]] | None = None,
    policy_config: PolicyConfig | None = None,
) -> NL2SQLResult:
    """Execute the governed NL2SQL pipeline end-to-end.

    Args:
        question: Natural-language question from the user.
        binding_id: ``AgentDataBinding.id`` for audit tracing.
        db: SQLAlchemy session (used to load metrics/mappings/policy if not provided).
        datasource_config: Dict with dialect + connection params.
        metrics: Pre-loaded metric definitions (loaded from DB if ``None``).
        mappings: Pre-loaded semantic mappings (loaded from DB if ``None``).
        policy_config: Per-binding policy (loaded from DB if ``None``).

    Returns:
        ``NL2SQLResult`` containing the data, intent, validation, and audit info.
    """
    t0 = time.monotonic()
    result = NL2SQLResult(question=question, success=False)

    try:
        # ── 0. Load binding early ─────────────────────────────────
        binding = _load_binding(db, binding_id)
        if datasource_config is None:
            datasource_config = _load_datasource_config(db, binding_id)
        adapter = _build_adapter(datasource_config or {})

        allowed_tables = _safe_list(binding.get("allowed_tables")) if binding else None
        allowed_columns = _safe_list(binding.get("allowed_columns")) if binding else None
        block_tables = _safe_list(binding.get("block_tables")) if binding else None

        # ── 1. Semantic resolution (best-effort hint, not a gate) ─
        intent = resolve(
            question,
            metrics=metrics or _load_metrics(db),
            mappings=mappings or _load_mappings(db),
        )
        result.intent = intent

        # Populate explanation from resolver match
        if intent.metric_name:
            result.explanation = f"matched metric '{intent.metric_name}' (confidence: {intent.confidence:.2f})"
        elif intent.table_name:
            result.explanation = f"matched table '{intent.table_name}' (confidence: {intent.confidence:.2f})"

        # ── 1.5. Disambiguation check ──────────────────────────────
        if intent.candidates:
            metric_candidates = [
                c for c in intent.candidates if c.get("type") == "metric"
            ]
            if len(metric_candidates) >= 2:
                top_score = metric_candidates[0]["score"]
                runner_up = metric_candidates[1]["score"]
                if (top_score - runner_up) < 0.05:
                    result.needs_clarification = metric_candidates[:2]
                    result.error = "needs_clarification"
                    return result
            if not intent.metric_name and not intent.table_name:
                result.needs_clarification = intent.candidates[:3]
                result.error = "needs_clarification"
                return result

        # ── 2. Build rich context for the LLM ──────────────────────
        from app.services.nl2sql.context_builder import ContextBuilder
        ctx_builder = ContextBuilder(db)
        schema_description = ctx_builder.build(
            question, binding, datasource_config or {}
        )

        # ── 3. Generate SQL via LLM (with retry on validation fail) ─
        from app.services.data_snapshot.snapshot_service import DataSnapshotService

        snapshot_svc = DataSnapshotService(db)
        sql = ""
        nl2sql_result = None
        last_error = ""

        for attempt in range(3):
            nl2sql_result = snapshot_svc.nl2sql(
                question=question,
                schema_description=schema_description,
                datasource_id=binding.get("datasource_id") if binding else None,
                allowed_tables=allowed_tables,
            )
            sql = nl2sql_result.get("sql", "")

            if nl2sql_result.get("valid"):
                break

            last_error = "; ".join(nl2sql_result.get("errors", []))
            # Augment schema_description with the error for the next attempt
            schema_description += f"\n<error-msg>Previous attempt failed: {last_error}</error-msg>"

        result.sql = sql

        if not nl2sql_result or not nl2sql_result.get("valid"):
            result.error = f"SQL generation failed after {attempt + 1} attempt(s): {last_error}"
            _write_audit(db, binding_id, "", 0, last_error[:200], "error")
            return result

        # ── 4. Validate generated SQL ─────────────────────────────
        validation = validate(
            sql,
            allowed_tables=allowed_tables,
            allowed_columns=allowed_columns,
            block_tables=block_tables,
        )
        result.validation = validation

        if not validation.is_valid:
            result.error = "; ".join(validation.errors)
            _write_audit(db, binding_id, validation.sql_hash, 0, result.error, "denied")
            return result

        # ── 4.5. Inject row-level permission filters ────────────────
        row_filters_raw = binding.get("row_filters") if binding else None
        if row_filters_raw:
            dialect = (datasource_config or {}).get("dialect", "postgresql")
            from app.services.nl2sql.row_filter import inject
            sql = inject(sql, row_filters_raw, dialect)
            result.sql = sql
            # Re-validate the modified SQL
            validation = validate(
                sql,
                allowed_tables=allowed_tables,
                allowed_columns=allowed_columns,
                block_tables=block_tables,
            )
            result.validation = validation
            if not validation.is_valid:
                result.error = "; ".join(validation.errors)
                _write_audit(db, binding_id, validation.sql_hash, 0, result.error, "denied")
                return result

        # ── 5. Adapter explain → cost estimation ──────────────────
        if not adapter.test_connection():
            result.error = "Datasource connection failed"
            return result

        explain_result = adapter.explain(sql)
        est_cost = explain_result.estimated_cost

        # ── 6. Policy evaluation ──────────────────────────────────
        if policy_config is None:
            policy_config = _build_policy_config(binding)

        policy_decision = evaluate(
            validation.tables_referenced,
            est_cost,
            policy_config=policy_config,
        )
        result.policy = policy_decision

        if not policy_decision.allowed:
            result.error = policy_decision.reason
            _write_audit(db, binding_id, validation.sql_hash, 0, policy_decision.reason, "denied")
            return result

        # ── 7. Execute ────────────────────────────────────────────
        data = adapter.query(
            sql,
            row_limit=policy_decision.row_limit,
            timeout_ms=policy_decision.max_execution_ms,
        )
        result.data = data
        result.success = True

        # ── 8. Create immutable DataSnapshot ───────────────────────
        try:
            rows_dicts = [
                {col: val for col, val in zip(data.columns, row)}
                for row in data.rows
            ]
            col_meta = [
                {"name": col, "index": i} for i, col in enumerate(data.columns)
            ]
            snap = snapshot_svc.create_snapshot(
                sql_query=sql,
                result_data=rows_dicts,
                result_columns=col_meta,
                natural_language=question,
                datasource_id=binding.get("datasource_id") if binding else None,
                execution_id=None,
                created_by_agent_id=binding.get("agent_app_id") if binding else None,
                validate=False,  # already validated above
                allowed_tables=allowed_tables,
                query_duration_ms=int(data.duration_ms),
            )
            snapshot_id = snap.id
        except Exception as snap_err:
            logger.warning("Failed to create DataSnapshot: %s", snap_err)
            snapshot_id = ""

        # ── 9. Audit ───────────────────────────────────────────────
        _write_audit(
            db,
            binding_id,
            validation.sql_hash,
            data.row_count,
            "OK",
            "allowed",
            duration_ms=data.duration_ms,
            sql=sql,
            snapshot_id=snapshot_id,
        )

    except Exception as e:
        logger.exception("NL2SQL pipeline failed: %s", e)
        result.error = str(e)
        try:
            _write_audit(db, binding_id, "", 0, str(e)[:200], "error")
        except Exception:
            pass
    finally:
        result.duration_ms = round((time.monotonic() - t0) * 1000, 2)
        # Write telemetry log
        try:
            _sql_hash = result.validation.sql_hash if result.validation else ""
            _errors = result.validation.errors if result.validation else ([result.error] if result.error else None)
            _outcome = "success" if result.success else ("denied" if "needs_clarification" in result.error else "error")
            _write_telemetry(
                db=db,
                binding=binding,
                question=question,
                generated_sql=result.sql,
                sql_hash=_sql_hash,
                validation_errors=_errors,
                policy_decision=result.policy.decision if result.policy else None,
                row_count=result.data.row_count if result.data else None,
                duration_ms=int(result.duration_ms),
                snapshot_id=snapshot_id if "snapshot_id" in dir() else "",
                outcome=_outcome,
                explanation=result.explanation,
            )
        except Exception:
            pass

    return result


# ── schema description builder ─────────────────────────────────────────


def _build_schema_description(
    adapter: DatasourceAdapter,
    allowed_tables: list[str] | None,
    ds_config: dict[str, Any] | None,
) -> str:
    """Build a human-readable schema description for the LLM prompt."""
    parts: list[str] = []

    # Prepend per-dialect quoting rules
    dialect = (ds_config or {}).get("dialect", "postgresql")
    try:
        from app.services.nl2sql.dialect_rules import quote_rule
        parts.append(f"<dialect-rules>{quote_rule(dialect)}</dialect-rules>")
    except Exception:
        pass

    # Append M-Schema
    try:
        from app.services.datasources.m_schema import render_m_schema
        parts.append(render_m_schema(adapter, allowed_tables=allowed_tables, sample_rows=3))
    except Exception:
        parts.append(f"Database schema for {dialect}. No schema information available.")

    return "\n\n".join(parts)


# ── DB loaders ────────────────────────────────────────────────────────


def _load_metrics(db: Session) -> list[dict[str, Any]]:
    try:
        from app.models.metric_definition import MetricDefinition
        rows = db.query(MetricDefinition).filter(MetricDefinition.is_deleted == False).all()
        return [
            {"id": r.id, "name": r.name, "synonyms": r.synonyms or []}
            for r in rows
        ]
    except Exception:
        return []


def _load_mappings(db: Session) -> list[dict[str, Any]]:
    try:
        from app.models.semantic_mapping import SemanticMapping
        rows = db.query(SemanticMapping).filter(SemanticMapping.is_deleted == False).all()
        return [
            {
                "id": r.id,
                "business_term": r.business_term,
                "synonyms": r.synonyms or [],
                "target_table": r.target_table,
                "target_columns": r.target_columns or [],
            }
            for r in rows
        ]
    except Exception:
        return []


def _load_binding(db: Session, binding_id: str) -> dict[str, Any] | None:
    try:
        from app.models.agent_data_binding import AgentDataBinding
        row = db.query(AgentDataBinding).filter(
            AgentDataBinding.id == binding_id,
            AgentDataBinding.is_deleted == False,
        ).first()
        if row is None:
            return None
        return {
            "allowed_tables": row.allowed_tables,
            "allowed_columns": row.allowed_columns,
            "block_tables": getattr(row, "block_tables", None),
            "row_limit": getattr(row, "row_limit", 1000),
            "cost_threshold": getattr(row, "cost_threshold", 1000.0),
            "max_execution_ms": getattr(row, "max_execution_ms", 5000),
            "datasource_id": row.datasource_id,
        }
    except Exception:
        return None


def _load_datasource_config(db: Session, binding_id: str) -> dict[str, Any]:
    try:
        from app.models.agent_data_binding import AgentDataBinding
        from app.models.datasource import Datasource
        binding = db.query(AgentDataBinding).filter(
            AgentDataBinding.id == binding_id,
            AgentDataBinding.is_deleted == False,
        ).first()
        if binding and binding.datasource_id:
            ds = db.query(Datasource).filter(Datasource.id == binding.datasource_id).first()
            if ds:
                return ds.connection_config or {}
    except Exception:
        pass
    return {}


def _build_policy_config(binding: dict[str, Any] | None) -> PolicyConfig:
    if binding is None:
        return PolicyConfig()
    return PolicyConfig(
        allowed_tables=_safe_list(binding.get("allowed_tables")),
        allowed_columns=_safe_list(binding.get("allowed_columns")),
        block_tables=_safe_list(binding.get("block_tables")),
        row_limit=int(binding.get("row_limit", 1000)),
        cost_threshold=float(binding.get("cost_threshold", 1000.0)),
        max_execution_ms=int(binding.get("max_execution_ms", 5000)),
    )


# ── audit ─────────────────────────────────────────────────────────────


def _write_audit(
    db: Session,
    binding_id: str,
    sql_hash: str,
    row_count: int,
    reason: str,
    decision: str = "allowed",
    duration_ms: float = 0.0,
    sql: str = "",
    snapshot_id: str = "",
) -> None:
    try:
        from uuid import uuid4
        from datetime import datetime as _dt
        from app.models.audit_event import AuditEvent
        outcome = "success" if decision == "allowed" else ("denied" if decision == "denied" else "failure")
        event = AuditEvent(
            id=str(uuid4()),
            event_type="nl2sql_query",
            event_source="nl2sql",
            trace_id=binding_id,
            actor_type="system",
            actor_id=None,
            resource_type="datasource",
            resource_id=None,
            binding_id=binding_id,
            sql_text_hash=sql_hash,
            row_count=row_count,
            query_duration_ms=int(duration_ms),
            policy_decision=decision,
            policy_reasons=[reason] if reason else None,
            outcome=outcome,
            detail_json={
                "sql_hash": sql_hash,
                "row_count": row_count,
                "duration_ms": duration_ms,
                "policy_decision": decision,
                "reason": reason,
                "snapshot_id": snapshot_id,
            },
            error_message=reason if decision in ("denied", "error") else None,
            occurred_at=_dt.utcnow(),
        )
        db.add(event)
        db.commit()
    except Exception as e:
        logger.warning("Failed to write audit event: %s", e)


def _safe_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    if val is not None:
        return [str(val)]
    return []


# ── telemetry ──────────────────────────────────────────────────────────


def _write_telemetry(
    db: Session,
    binding: dict[str, Any] | None,
    question: str,
    generated_sql: str,
    sql_hash: str,
    validation_errors: list | None,
    policy_decision: str,
    row_count: int | None,
    duration_ms: int,
    snapshot_id: str,
    outcome: str,
    explanation: str,
) -> None:
    try:
        from uuid import uuid4
        from app.models.nl2sql_query_log import Nl2sqlQueryLog

        log = Nl2sqlQueryLog(
            id=str(uuid4()),
            binding_id=binding.get("datasource_id") if binding else None,  # binding ID from the dict
            agent_app_id=binding.get("agent_app_id") if binding else None,
            datasource_id=binding.get("datasource_id") if binding else None,
            question=question,
            generated_sql=generated_sql or None,
            sql_hash=sql_hash or None,
            validation_errors={"errors": validation_errors} if validation_errors else None,
            policy_decision=policy_decision or None,
            row_count=row_count,
            duration_ms=duration_ms,
            snapshot_id=snapshot_id or None,
            outcome=outcome,
            explanation=explanation or None,
        )
        db.add(log)
        db.commit()
    except Exception as e:
        logger.warning("Failed to write telemetry log: %s", e)
