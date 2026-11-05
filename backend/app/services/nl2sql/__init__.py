"""NL2SQL orchestrator — the COMPATIBILITY entry point over the grounded path.

THE canonical implementation of text→data lives in
``app/services/db/nl_answer_service.py`` (plan → retrieval → link → SQL →
``QueryService.execute`` → canonical result set), enforced at the execution
boundary (``app/services/db/execution_gate.py``). This module used to be a
SECOND pipeline with its own adapter, its own validator, its own policy check and
its own execution call — which meant a question could be answered two different
ways depending on which entry point an agent reached, and the weaker path skipped
source readiness, the execution boundary, the grounded plan and the canonical
result set.

What remains here:

* :func:`ask` — a compatibility ADAPTER: it resolves which source the legacy
  inputs name, converts the legacy restrictions (binding / ``PolicyConfig``) into
  the ``ResolvedPolicy`` the canonical path already enforces, runs the canonical
  answer, and maps the outcome onto the legacy ``NL2SQLResult`` keys. It does not
  author SQL, does not open a connector, and cannot execute anything except
  through ``QueryService.execute``. It carries ``plan``, ``result_set``,
  ``unsupported_slots``, ``disclosure`` and ``provenance`` through: stripping
  them would hide the sentence that says part of the question went unanswered.
* :func:`validate_sql` — the verdict-only facade for callers that already have
  SQL (shadow-mode logging, report-recipe checks).

The source is whatever the caller's data says it is. There is no default
database, no "first source", and no run-against-every-source behaviour; an
unresolvable source is a refusal in business language.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.services.datasources import DatasourceAdapter, QueryResult
from app.services.datasources.sqlite_adapter import SQLiteAdapter
from app.services.datasources.postgres_adapter import PostgresAdapter
from app.services.datasources.mysql_adapter import MySQLAdapter
from app.services.nl2sql import compat
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
    """The legacy result shape, now carrying the grounded contracts too.

    Every pre-consolidation key keeps its meaning, so a caller that reads
    ``success`` / ``sql`` / ``data`` / ``validation`` / ``policy`` needs no
    change. The P3 keys at the end are ADDITIVE: a caller that tolerates extra
    keys sees the grounded plan and the canonical result set; a caller with a
    strict response model can drop them with
    ``db.nl_answer_service.legacy_result_view`` — but must not, on a user-facing
    path, drop ``disclosure`` or ``unsupported_slots``.
    """

    success: bool
    question: str
    sql: str = ""
    data: QueryResult | None = None
    #: legacy semantic-resolver hint — superseded by ``plan`` (the resolver is no
    #: longer run: the grounded plan records the same decision with evidence).
    intent: Any = None
    validation: ValidationResult | None = None
    policy: PolicyDecision | None = None
    error: str = ""
    duration_ms: float = 0.0
    needs_clarification: list[dict[str, Any]] | None = None
    explanation: str = ""

    # ── canonical outcome ────────────────────────────────────────────────
    #: the reader-facing prose the canonical path produced
    answer: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    #: the binding/source id this call was traced under (legacy ``binding_id``)
    binding_id: str = ""
    source_id: str = ""
    source_name: str = ""
    #: machine classification of a failure (``source_not_ready``,
    #: ``source_not_resolved``, ``execution_validation_failed``, …)
    error_kind: str = ""
    #: $GroundedPlan.to_dict() — what the platform decided it could answer
    plan: dict[str, Any] | None = None
    #: $CanonicalResultSet.to_dict() — what actually came back
    result_set: dict[str, Any] | None = None
    #: slots of the question the selected tables cannot support
    unsupported_slots: list[dict[str, Any]] = field(default_factory=list)
    #: the reader-facing sentence for anything unsupported / limited
    disclosure: str = ""
    #: 2B-3: the scope statement the reader was given ("" when none)
    scope_disclosure: str = ""
    #: 2B-3: reader-usable scope-refusal sentences (reroute refusals)
    scope_refusals: list[str] = field(default_factory=list)
    #: 2B-3: the execution gate's scope verdict VERBATIM
    #: (pass / unavailable / not-applicable — degraded never reads verified)
    scope_check: str = ""
    #: source, SQL fingerprints, tables, requested vs available window
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def grounded(self) -> bool:
        """True when this result carries the grounded contracts (plan + result set)."""
        return bool(self.plan and self.result_set)


# ── legacy adapter helpers (kept for the M-Schema/context builders) ─────


def _build_adapter(ds_config: dict[str, Any]) -> DatasourceAdapter:
    """Build the appropriate adapter from a datasource config dict.

    LEGACY HELPER: used by ``nl2sql.context_builder`` to render an M-Schema for
    a prompt. It is NOT on the answering path — ``ask()`` never builds an
    adapter, because SQL is executed by ``QueryService.execute`` and nowhere
    else. The dialect is REQUIRED: this used to default to ``sqlite``, so a
    binding whose config was incomplete silently ran against a dialect it was
    not connected to. An unidentified dialect raises ``UnknownDialect`` (a
    ``ValueError``) — see ``services/db/dialect.py``.
    """
    raw_dialect = ds_config.get("dialect")
    if not raw_dialect:
        from app.services.db.dialect import UnknownDialect

        raise UnknownDialect(
            "datasource config has no 'dialect' — cannot choose an adapter "
            "without assuming a database type."
        )
    from app.services.db.dialect import UnknownDialect, profile_for

    try:
        dialect = profile_for(raw_dialect).name
    except UnknownDialect as exc:
        raise UnknownDialect(
            f"{exc} Cannot route this binding to an adapter."
        ) from exc
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


# ── the compatibility adapter ──────────────────────────────────────────


def ask(
    question: str,
    *,
    db: Session,
    binding_id: str | None = None,
    kb_id: str | None = None,
    datasource_config: dict[str, Any] | None = None,
    metrics: list[dict[str, Any]] | None = None,
    mappings: list[dict[str, Any]] | None = None,
    policy_config: PolicyConfig | None = None,
    project_id: str | None = None,
    endpoint: Any = None,
    skip_narrate: bool = False,
    agent_name: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> NL2SQLResult:
    """Answer a question through the CANONICAL grounded path.

    Args:
        question: Natural-language question from the user.
        db: SQLAlchemy session.
        binding_id: legacy ``AgentDataBinding.id`` (or the source id agents are
            bound to). Kept so existing callers keep working; used only to
            RESOLVE the source and to trace the audit record.
        kb_id: the data source id, when the caller already knows it (this is what
            the workflow runtime and the tool layer hold).
        datasource_config: legacy datasource config. When it carries the source
            object/id (``kb`` / ``kb_id``), that is used directly; a bare
            connection dict is matched to a source by connection identity.
        metrics / mappings: accepted for signature compatibility. The canonical
            path resolves meaning from the source's own semantic catalog and
            retrieval, not from a keyword-to-table hint table.
        policy_config: legacy per-binding ``PolicyConfig``. Converted into the
            ``ResolvedPolicy`` the execution boundary validates against.
        project_id / endpoint / agent_name: passed through to the canonical path
            (LLM endpoint binding, per-turn accounting).
        skip_narrate: let the caller synthesize the final prose (deliverable
            turns) — the deterministic fallback still carries the disclosure.

    Returns:
        ``NL2SQLResult`` — legacy keys plus ``plan`` / ``result_set`` /
        ``unsupported_slots`` / ``disclosure`` / ``provenance``.
    """
    del metrics, mappings  # see the docstring: the canonical path resolves these
    t0 = time.monotonic()
    result = NL2SQLResult(question=question, success=False)

    if not question or not question.strip():
        result.error = "Question is required"
        result.error_kind = "empty_question"
        return result

    # ── 1. Which source does this call name? Never guessed. ────────────
    resolved = compat.resolve_source(
        db,
        kb_id=kb_id,
        binding_id=binding_id,
        datasource_config=datasource_config,
    )
    result.source_id = resolved.kb_id or str(binding_id or "")
    result.binding_id = resolved.binding_id or str(binding_id or "")
    if not resolved.ok:
        result.error = resolved.error
        result.error_kind = resolved.error_kind
        result.explanation = resolved.detail
        _write_audit(
            db, result.binding_id, "", 0, resolved.detail or resolved.error, "denied"
        )
        return result

    # ── 2. The legacy restrictions, in the shape the boundary enforces ──
    policy = compat.policy_from_binding(resolved.kb_id, resolved.binding, policy_config)
    row_filters = compat.row_filters_for(policy, resolved.kb_id)

    # ── 3. The canonical answer. Readiness, authorization, retrieval,
    #       linking, generation, the execution boundary, the grounded plan,
    #       the canonical result set and the disclosure are all its work. ──
    try:
        canonical = compat.run_canonical(
            db,
            resolved.kb_id,
            question,
            resolved_policy=policy,
            row_filters=row_filters,
            project_id=project_id,
            endpoint=endpoint,
            skip_narrate=skip_narrate,
            agent_name=agent_name,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
    except Exception as e:  # never raise into a served path
        logger.exception("nl2sql.ask: canonical answer raised: %s", e)
        result.error = str(e)
        result.error_kind = "internal_error"
        _write_audit(db, result.binding_id, "", 0, str(e)[:200], "error")
        result.duration_ms = round((time.monotonic() - t0) * 1000, 2)
        return result

    _apply_canonical(result, canonical, resolved)

    result.duration_ms = round((time.monotonic() - t0) * 1000, 2)

    # ── 4. Legacy side effects: audit trail + telemetry row ────────────
    _sql_hash = (result.validation.sql_hash if result.validation else "") or ""
    if result.success:
        row_count = len(result.rows)
        snapshot_id = _create_snapshot(db, result, resolved, question)
        _write_audit(
            db,
            result.binding_id,
            _sql_hash,
            row_count,
            "OK",
            "allowed",
            duration_ms=result.duration_ms,
            sql=result.sql,
            snapshot_id=snapshot_id,
        )
        _write_telemetry(
            db=db,
            binding=_binding_row(resolved),
            question=question,
            generated_sql=result.sql,
            sql_hash=_sql_hash,
            validation_errors=None,
            policy_decision=(
                ("allowed" if result.policy.allowed else "denied")
                if result.policy else None
            ),
            row_count=row_count,
            duration_ms=int(result.duration_ms),
            snapshot_id=snapshot_id,
            outcome="success",
            explanation=result.explanation,
        )
    else:
        decision = "denied" if result.error_kind in (
            "source_not_ready",
            "source_not_resolved",
            "execution_validation_failed",
        ) else "error"
        _write_audit(
            db,
            result.binding_id,
            _sql_hash,
            len(result.rows),
            (result.error or "")[:200],
            decision,
        )
        _write_telemetry(
            db=db,
            binding=_binding_row(resolved),
            question=question,
            generated_sql=result.sql,
            sql_hash=_sql_hash,
            validation_errors=(result.validation.errors if result.validation else None),
            policy_decision=decision,
            row_count=None,
            duration_ms=int(result.duration_ms),
            snapshot_id="",
            outcome=decision,
            explanation=result.explanation,
        )

    return result


def _binding_row(resolved: compat.SourceResolution) -> dict[str, Any] | None:
    """The legacy binding dict the audit/telemetry writers expect."""
    if resolved.binding is None:
        return {"datasource_id": resolved.kb_id, "agent_app_id": None}
    return {
        "datasource_id": getattr(resolved.binding, "datasource_id", None) or resolved.kb_id,
        "agent_app_id": getattr(resolved.binding, "agent_app_id", None),
    }


def _apply_canonical(
    result: NL2SQLResult,
    canonical: dict[str, Any],
    resolved: compat.SourceResolution,
) -> None:
    """Map a canonical answer onto the legacy result, keeping every contract.

    A single place decides what a legacy caller sees, so no key can be dropped by
    one branch and kept by another.
    """
    result.success = bool(canonical.get("success"))
    result.answer = str(canonical.get("answer") or "")
    result.rows = list(canonical.get("rows") or [])
    result.columns = list(canonical.get("columns") or [])
    result.sql = str(
        canonical.get("sql")
        or canonical.get("authored_sql")
        or ""
    )
    result.source_id = str(canonical.get("source_id") or resolved.kb_id or "")
    result.source_name = str(canonical.get("source_name") or "")
    result.error = str(canonical.get("error") or "")
    result.error_kind = str(canonical.get("error_kind") or "")
    result.explanation = _explanation(canonical)

    # The grounded contracts travel with the answer — always, on success and on
    # refusal alike (a refused statement has a plan and a result set too).
    result.plan = canonical.get("plan")
    result.result_set = canonical.get("result_set")
    result.unsupported_slots = list(canonical.get("unsupported_slots") or [])
    if result.result_set:
        result.unsupported_slots = list(
            result.unsupported_slots
            or result.result_set.get("unsupported_slots")
            or []
        )
    result.disclosure = str(
        canonical.get("disclosure")
        or (result.result_set or {}).get("disclosure")
        or canonical.get("source_readiness", {}).get("disclosure")
        or ""
    )
    result.provenance = dict(
        canonical.get("provenance")
        or (result.result_set or {}).get("provenance")
        or {}
    )
    # 2B-3 (review check 3): scope verdict + disclosure ride the legacy view
    # too — a caller that reads only NL2SQLResult must not lose the sentence
    # that says the figures may be partial.
    result.scope_disclosure = str(canonical.get("scope_disclosure") or "")
    result.scope_refusals = list(canonical.get("scope_refusals") or [])
    result.scope_check = str(canonical.get("scope_check") or "")

    result.validation = _legacy_validation(canonical, result)
    result.policy = _legacy_policy(canonical, result)
    if canonical.get("needs_clarification"):
        result.needs_clarification = canonical["needs_clarification"]

    if result.success and result.rows:
        result.data = compat._query_result_from_rows(
            result.rows, result.sql, int(canonical.get("elapsed_ms") or 0)
        )


def _explanation(canonical: dict[str, Any]) -> str:
    """A short, business-language reason for the outcome (legacy ``explanation``)."""
    if canonical.get("success"):
        plan = canonical.get("plan") or {}
        tables = ", ".join(str(t) for t in (plan.get("tables") or [])[:5])
        parts = [f"answered from {tables}"] if tables else []
        if canonical.get("disclosure"):
            parts.append("with a disclosed limitation")
        if canonical.get("short_circuit"):
            parts.append(str(canonical["short_circuit"]))
        return "; ".join(parts)
    return str(
        canonical.get("error_kind")
        or canonical.get("readiness_code")
        or canonical.get("error")
        or ""
    )


def _validation_payload(canonical: dict[str, Any], result: NL2SQLResult) -> dict[str, Any]:
    """The execution-boundary payload, from wherever the outcome carries it."""
    payload = canonical.get("validation")
    if isinstance(payload, dict) and payload:
        return payload
    plan = canonical.get("plan") or {}
    payload = plan.get("execution_validation")
    if isinstance(payload, dict) and payload:
        return payload
    payload = (canonical.get("result_set") or {}).get("execution_validation")
    if isinstance(payload, dict) and payload:
        return payload
    return {}


def _legacy_validation(
    canonical: dict[str, Any], result: NL2SQLResult
) -> ValidationResult | None:
    """The legacy validation view, derived from the boundary payload.

    Derived, never recomputed: running the legacy validator a second time could
    disagree with the statement that actually ran, and the point of the
    consolidation is that there is one verdict, produced by the boundary.
    """
    payload = _validation_payload(canonical, result)
    provenance = result.provenance or {}
    tables = list((payload.get("detail") or {}).get("tables") or [])
    if not tables and result.sql:
        tables = list(provenance.get("tables") or [])
    sql_hash = str(
        payload.get("sql_hash")
        or provenance.get("sql_fingerprint")
        or ""
    )
    if not payload:
        if not result.success and not result.sql:
            return None
        return ValidationResult(
            is_valid=result.success,
            errors=[] if result.success else [result.error] if result.error else [],
            warnings=[],
            sql_hash=sql_hash,
            tables_referenced=tables,
        )
    checks = payload.get("checks") or {}
    errors = [str(e) for e in (payload.get("errors") or [])]
    # A check that failed is the boundary's own verdict; ``ok`` is the summary.
    if not payload.get("ok", True) and not errors:
        errors = [f"{name} failed" for name, state in checks.items() if state == "fail"]
    return ValidationResult(
        is_valid=bool(payload.get("ok", result.success)),
        errors=errors,
        warnings=list((canonical.get("result_set") or {}).get("warnings") or []),
        sql_hash=sql_hash,
        tables_referenced=tables,
    )


def _legacy_policy(
    canonical: dict[str, Any], result: NL2SQLResult
) -> PolicyDecision | None:
    """The legacy policy view, derived from the boundary's ``policy`` check."""
    payload = _validation_payload(canonical, result)
    checks = (payload.get("checks") or {}) if payload else {}
    state = checks.get("policy")
    if state is None:
        return None
    denied = [
        str(e) for e in (payload.get("errors") or [])
        if "may not read" in str(e)
    ]
    return PolicyDecision(
        allowed=state != "fail",
        reason="; ".join(denied),
    )


def _create_snapshot(
    db: Session,
    result: NL2SQLResult,
    resolved: compat.SourceResolution,
    question: str,
) -> str:
    """Persist the immutable DataSnapshot the legacy path used to create.

    Built from rows the canonical path already fetched — this records a result,
    it does not run one.
    """
    if not result.rows or not result.sql:
        return ""
    try:
        from app.services.data_snapshot.snapshot_service import DataSnapshotService

        col_meta = [{"name": c, "index": i} for i, c in enumerate(result.columns)]
        snap = DataSnapshotService(db).create_snapshot(
            sql_query=result.sql,
            result_data=[dict(r) for r in result.rows],
            result_columns=col_meta,
            natural_language=question,
            datasource_id=result.source_id,
            knowledge_base_id=result.source_id,
            created_by_agent_id=getattr(resolved.binding, "agent_app_id", None),
            validate=False,  # verified at the execution boundary before it ran
            query_duration_ms=int(result.duration_ms),
        )
        return snap.id
    except Exception as snap_err:
        logger.warning("Failed to create DataSnapshot: %s", snap_err)
        return ""


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
        # audit_events.binding_id + trace_id are varchar(36)/varchar(64) —
        # a synthetic id longer than the column truncates at FLUSH time,
        # and a failed flush poisons the shared SQLAlchemy session for the
        # caller (next commit dies with "transaction has been rolled back").
        # Bound it defensively so this helper can never crash its caller.
        if len(binding_id) > 36:
            binding_id = binding_id[:36]
        outcome = "success" if decision == "allowed" else ("denied" if decision == "denied" else "failure")
        # Phase 7C (C1): stamp the real tenant org from the binding row —
        # before 7C every nl2sql audit row fell to the "default-org" server
        # default regardless of the requesting tenant. Fail-open: an
        # unresolvable binding still writes (org stays at the default).
        _audit_org = None
        if binding_id:
            from app.models.agent_data_binding import AgentDataBinding
            _binding = db.query(AgentDataBinding).filter(
                AgentDataBinding.id == binding_id).first()
            _audit_org = getattr(_binding, "org_id", None)
        event = AuditEvent(
            id=str(uuid4()),
            event_type="nl2sql_query",
            event_source="nl2sql",
            **({"org_id": _audit_org} if _audit_org else {}),
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
        # A flush error leaves the session in a pending-rollback state;
        # without rollback() the CALLER's next commit fails with
        # "This Session's transaction has been rolled back". Recovery
        # must happen here, in the component that broke the transaction.
        try:
            db.rollback()
        except Exception:
            pass
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
    policy_decision: str | None,
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
        # Recovery — see _write_audit: a failed flush poisons the session
        # for the caller; roll back here so the caller's transaction
        # survives a telemetry write failure.
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("Failed to write telemetry log: %s", e)
