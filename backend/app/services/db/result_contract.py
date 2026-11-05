"""Query result contract — the ONE authoritative decision for whether a data
tool call succeeded.

WHY THIS MODULE EXISTS (root-cause fix, 2026-10-27)
---------------------------------------------------
A user asked "give me last week sales report please". The turn ran 4.5+
minutes and executed the same three SQL questions SIX times each. The
database was never slow (~0.9-1.4 s) and the NL2SQL pipeline never broke:
every live ``observation_records`` row for that turn carried
``success = t`` with real rows.

What actually happened:

  RC-1  ``query_database_tool`` read ``getattr(result, "observations",
        None)`` — but ``ExecutionResult`` (the FSM's return type) has NO
        ``observations`` and NO ``execution`` attribute. Both lookups
        returned ``None``, so the tool always saw ``observations == []``
        and reported "The NL2SQL pipeline did not execute any query —
        no bound data source reached the pipeline". Execution status and
        tool-call status were CONFLATED. Because the lookups used safe
        ``getattr`` defaults, the defect raised nothing and logged nothing.
  RC-3  That false failure matched no permanent marker in
        ``agent_tools._is_retryable``, whose documented default is
        "unknown errors default to retryable" — so the reliability layer
        re-ran the ENTIRE NL2SQL pipeline (schema selection, source
        verification, LLM SQL generation, execution, self-check) per retry.

THE CONTRACT
------------
``success`` is a COMPUTED property (approved design A1). It is derived from
two independent axes so that contradictory combinations cannot be built:

  execution_status : did SQL actually run against the source?
  result_status    : did we successfully package what came back?

A valid query that returns ZERO rows is a SUCCESS (execution SUCCEEDED,
result EMPTY). An extraction failure AFTER SQL executed is NOT a success,
and must never trigger a second SQL execution (A2) — recovery reads the
durable ``observation_records`` by execution id only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

logger = logging.getLogger(__name__)


class FailureKind(str, Enum):
    """Typed failure reason. Fed to the tool result as ``failure_kind``,
    which ``agent_tools._is_retryable`` consults BEFORE any message-string
    matching (approved design A3)."""

    NONE = "none"
    #: The source was reachable but the statement failed/timed out — worth a
    #: bounded retry (the transient class that genuinely warrants one).
    TRANSIENT_EXECUTION_FAILURE = "transient_execution_failure"
    #: No bound source reached the pipeline, or the source is misconfigured.
    #: Retrying reproduces it identically — never retried.
    PERMANENT_BINDING_FAILURE = "permanent_binding_failure"
    #: SQL ran, but the result could not be assembled into rows. Recover from
    #: the EXISTING result only; never re-execute the pipeline.
    RESULT_EXTRACTION_FAILURE = "result_extraction_failure"


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_EXECUTED = "not_executed"


class ResultStatus(str, Enum):
    ASSEMBLED = "assembled"
    EMPTY = "empty"
    EXTRACTION_FAILED = "extraction_failed"


@dataclass(frozen=True)
class NormalizedQueryResult:
    """The single shape every data-tool result flows through.

    ``success`` is deliberately NOT a field: it is the computed property
    below, so a caller cannot assert success that the statuses contradict.
    """

    execution_status: ExecutionStatus
    result_status: ResultStatus
    failure_kind: FailureKind = FailureKind.NONE
    rows: list[dict] = field(default_factory=list)
    sql: str | None = None
    source_id: str | None = None
    source_name: str | None = None
    #: Orthogonal to success: a truncated-but-real result is a success with
    #: complete=False (approved design — test 8).
    complete: bool = True
    warnings: list[str] = field(default_factory=list)
    #: execution_id, observation seqs, kb ids — survives recovery (A2).
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Human-readable, addressed to the agent. Never a stack trace.
    diagnostic: str | None = None
    #: Actionable next step for the agent, when the failure class has one.
    hint: str | None = None
    #: Raw failure text for diagnosis (plan-node errors, DB errors).
    detail: str | None = None
    #: True when the source was unreachable (environment problem). Kept as an
    #: explicit flag because callers branch on it.
    unreachable: bool = False
    #: Staged timings (ms). Distinguishes a slow DATABASE from slow
    #: ORCHESTRATION — the question the 2026-10-27 investigation could not
    #: answer from logs because duration_ms was always NULL (RC-2).
    timing: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """THE authoritative success decision (A1).

        True only when SQL demonstrably ran AND its result was packaged —
        including the legitimate zero-row case. An extraction failure never
        counts as success, even though execution succeeded.
        """
        return self.execution_status is ExecutionStatus.SUCCEEDED and self.result_status in (
            ResultStatus.ASSEMBLED,
            ResultStatus.EMPTY,
        )

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_tool_result(self) -> dict[str, Any]:
        """Project onto the tool-result dict the agent loop consumes.

        ``failure_kind`` is always present so the reliability layer's typed
        classification takes precedence over its string-marker heuristics.
        """
        base: dict[str, Any] = {
            "success": self.success,
            "rows": self.rows,
            "row_count": self.row_count,
            "sql": self.sql,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "complete": self.complete,
            "execution_status": self.execution_status.value,
            "result_status": self.result_status.value,
            "failure_kind": self.failure_kind.value,
            "provenance": self.provenance,
            "timing": dict(self.timing),
        }
        if self.warnings:
            base["warnings"] = self.warnings
        if not self.success:
            base["error"] = self.diagnostic or "The data query could not be completed."
            if self.hint:
                base["hint"] = self.hint
            if self.detail:
                base["detail"] = self.detail
            if self.unreachable:
                base["unreachable"] = True
        return base


# ── Extraction ────────────────────────────────────────────────────────────


def extract_observations(result: Any, db: Any) -> tuple[list[Any], str | None]:
    """Return ``(observations, execution_id)`` from an FSM ``ExecutionResult``.

    Two lookups, in order:
      1. the object's own ``observations`` attribute, when it has one;
      2. otherwise the durable ``observation_records`` rows for
         ``result.execution_id`` — the REAL source of truth (RC-1 fix).

    ``execution_id`` is ``None`` when it cannot be established; callers must
    treat that as RESULT_EXTRACTION_FAILURE rather than inferring "no rows".
    Never raises: a DB error degrades to an extraction failure, never to a
    fabricated empty result.
    """
    own = list(getattr(result, "observations", None) or [])
    execution_id = getattr(result, "execution_id", None)
    if own:
        return own, execution_id

    if not execution_id:
        return [], None

    try:
        from app.models.execution import ObservationRecord

        rows = (
            db.query(ObservationRecord)
            .filter(ObservationRecord.execution_id == execution_id)
            .order_by(ObservationRecord.seq.asc())
            .all()
        )
        return list(rows), execution_id
    except Exception as exc:  # noqa: BLE001 — degrade to typed failure
        logger.warning(
            "result_contract: observation lookup failed for execution %s: %s",
            execution_id,
            exc,
        )
        return [], execution_id


def _result_data(obs: Any) -> dict | None:
    rd = getattr(obs, "result_data", None)
    return rd if isinstance(rd, dict) else None


def _is_success_obs(obs: Any) -> bool:
    """An observation is successful unless explicitly marked otherwise."""
    flag = getattr(obs, "success", None)
    if flag is False:
        return False
    rd = _result_data(obs)
    if isinstance(rd, dict) and rd.get("success") is False:
        return False
    return True


def _is_transient_message(text: str) -> bool:
    low = (text or "").lower()
    return any(
        marker in low
        for marker in (
            "timed out",
            "timeout",
            "connection reset",
            "connection refused",
            "could not connect",
            "can't connect",
            "lost connection",
            "too many connections",
            "deadlock",
            "temporarily unavailable",
        )
    )


#: Failure text that proves the pipeline never had a source to run against.
_BINDING_MARKERS = (
    "no bound data source",
    "bound_kb_ids=[]",
    "did not execute any query",
    "no bound data source reached the pipeline",
)


def extract_plan_node_errors(result: Any) -> list[str]:
    """Failed plan-node errors from ``ExecutionResult.plan_summary``.

    Ported from the 2026-10-20 fix (conv c341f610): the REAL failure reasons
    often live on plan nodes, while observations carry neither rows nor
    errors — so a network outage was misreported as a binding failure. Without
    this, a timed-out source would be classified as a permanent binding fault
    and silently never retried.
    """
    errors: list[str] = []
    summary = getattr(result, "plan_summary", None) or {}
    if not isinstance(summary, dict):
        return errors
    for node in summary.get("nodes") or []:
        try:
            if isinstance(node, dict) and node.get("status") == "failed" and node.get("error"):
                errors.append(str(node["error"])[:300])
        except Exception:  # noqa: BLE001 — malformed node: skip, never raise
            continue
    return errors


def _normalize_impl(
    result: Any,
    db: Any,
    *,
    question: str = "",
    timing: dict[str, Any] | None = None,
) -> NormalizedQueryResult:
    """Map an FSM ``ExecutionResult`` onto the contract (inner form)."""
    observations, execution_id = extract_observations(result, db)
    plan_errors = extract_plan_node_errors(result)

    provenance: dict[str, Any] = {"execution_id": execution_id}

    if not observations:
        # No observation records at all. The plan nodes may still explain WHY
        # — classify from them rather than defaulting to "no result record".
        if plan_errors:
            return _classify_no_execution(
                plan_errors, None, provenance, question
            )
        return NormalizedQueryResult(
            execution_status=ExecutionStatus.NOT_EXECUTED,
            result_status=ResultStatus.EXTRACTION_FAILED,
            failure_kind=FailureKind.RESULT_EXTRACTION_FAILURE,
            provenance=provenance,
            diagnostic=(
                "The data pipeline produced no usable result record for this "
                "question, so nothing can be reported. Do NOT retry this exact "
                "call and never invent figures — tell the user the query could "
                "not be completed."
            ),
        )

    rows: list[dict] = []
    sql: str | None = None
    source_id: str | None = None
    source_name: str | None = None
    complete = True
    warnings: list[str] = []
    executed_any = False
    executed_empty = False
    generated_only_sql: str | None = None
    failure_messages: list[str] = []
    obs_seqs: list[Any] = []
    #: DB-side execution time reported by the connector, when present.
    db_elapsed_ms: float | None = None

    for obs in observations:
        obs_seqs.append(getattr(obs, "seq", None))
        otype = getattr(obs, "observation_type", "") or ""
        rd = _result_data(obs)

        if not _is_success_obs(obs):
            err = getattr(obs, "error_message", None) or (
                rd.get("error") if isinstance(rd, dict) else None
            )
            if err:
                failure_messages.append(str(err)[:300])
            continue

        if otype != "nl2sql" or not isinstance(rd, dict):
            continue

        obs_sql = rd.get("sql") or None
        if obs_sql and not sql:
            sql = obs_sql
        if rd.get("source_id") and not source_id:
            source_id = rd.get("source_id")
        if rd.get("source_name") and not source_name:
            source_name = rd.get("source_name")

        for _k in ("elapsed_ms", "duration_ms"):
            _v = rd.get(_k)
            if isinstance(_v, (int, float)):
                db_elapsed_ms = (db_elapsed_ms or 0.0) + float(_v)
                break
        for _obs_ms in (getattr(obs, "duration_ms", None),):
            if isinstance(_obs_ms, (int, float)):
                db_elapsed_ms = (db_elapsed_ms or 0.0) + float(_obs_ms)

        data = rd.get("data")
        if isinstance(data, list):
            # The live branch ALWAYS carries "data" — even empty — because the
            # query WAS executed. Its presence is proof of execution.
            executed_any = True
            if data:
                rows = data
            else:
                executed_empty = True
        elif obs_sql and rd.get("executed") is not True:
            # SQL text with no "data": generated, never executed.
            generated_only_sql = generated_only_sql or obs_sql

        if rd.get("truncated") is True:
            complete = False
            warnings.append("Result was truncated at the row cap.")

    provenance["observation_seqs"] = obs_seqs
    timing = {
        "db_execution_ms": db_elapsed_ms,
        "observation_count": len(observations),
        "sql_generated": bool(sql or generated_only_sql),
    }
    provenance["timing"] = timing

    # ── Execution succeeded and rows were assembled ──────────────────────
    if executed_any and rows:
        return NormalizedQueryResult(
            execution_status=ExecutionStatus.SUCCEEDED,
            result_status=ResultStatus.ASSEMBLED,
            rows=rows,
            sql=sql,
            source_id=source_id,
            source_name=source_name,
            complete=complete,
            warnings=warnings,
            provenance=provenance,
            diagnostic=question,
        )

    # ── Executed, genuinely zero rows — a SUCCESS (test 7) ───────────────
    if executed_any and executed_empty:
        return NormalizedQueryResult(
            execution_status=ExecutionStatus.SUCCEEDED,
            result_status=ResultStatus.EMPTY,
            rows=[],
            sql=sql,
            source_id=source_id,
            source_name=source_name,
            complete=complete,
            warnings=warnings
            + ["The query executed against the bound source and matched no rows."],
            provenance=provenance,
            diagnostic=question,
        )

    # ── Nothing executed: classify WHY (typed, A3) ───────────────────────
    return _classify_no_execution(
        failure_messages + plan_errors, generated_only_sql, provenance, question
    )


# ── Durable VERIFY verdict (:420 micro-fix) ───────────────────────────────


def _load_durable_verification(db: Any, execution_id: str | None) -> dict | None:
    """Load the FSM VERIFY verdict the execution durably committed.

    The FSM stores ``VerificationResult.to_dict()`` at
    ``executions.confidence_factors["verification"]`` and commits (synexia/
    fsm.py, VERIFY state). Must be called while the session is open — the
    same constraint as the observation recovery above. Returns None when no
    verdict exists (never ran, execution row gone). A lookup failure is
    LOGGED and degrades to None — never swallowed silently, never fabricated.
    """
    if db is None or not execution_id:
        return None
    try:
        from app.models.execution import Execution

        row = db.query(Execution).filter(Execution.id == execution_id).first()
        factors = getattr(row, "confidence_factors", None)
        if isinstance(factors, dict):
            verdict = factors.get("verification")
            if isinstance(verdict, dict):
                return verdict
    except Exception as exc:  # noqa: BLE001 — degrade, but SAY SO
        logger.warning(
            "result_contract: verification lookup failed for execution %s: %s",
            execution_id, exc,
        )
    return None


def _attach_durable_verification(normalized: NormalizedQueryResult, db: Any) -> None:
    """Put the durable VERIFY verdict into provenance (when one exists).

    The key is ABSENT when no verdict was produced — a missing verification
    stays distinguishable from a failed one downstream.
    """
    try:
        verdict = _load_durable_verification(
            db, (normalized.provenance or {}).get("execution_id")
        )
    except Exception as exc:  # noqa: BLE001 — provenance is never critical
        logger.warning("result_contract: verification attach failed: %s", exc)
        return
    if isinstance(verdict, dict) and isinstance(normalized.provenance, dict):
        normalized.provenance["verification"] = verdict


def project_verification(provenance: dict | None) -> tuple[bool | None, str | None]:
    """Project the durable VERIFY verdict onto the tool result's
    ``(verified, verify_detail)`` fields.

    Semantics (the :420 defect contract):
      * verdict with ``passed=True``  → ``(True, "all checks passed")``
      * verdict with ``passed=False`` → ``(False, "<failing checks>")`` —
        a FAILED verification, never a silent pass
      * no verdict / malformed shape  → ``(None, None)`` — MISSING
        verification, distinct from both True and False
    Never raises.
    """
    try:
        if not isinstance(provenance, dict):
            return (None, None)
        verdict = provenance.get("verification")
        if not isinstance(verdict, dict):
            return (None, None)
        passed = verdict.get("passed")
        if passed not in (True, False):
            # Malformed/legacy shape: "verification happened" is not provable
            # — treat as missing rather than guessing a verdict.
            return (None, None)
        checks = verdict.get("checks")
        failed = [
            f"{c.get('check')}: {c.get('detail')}"
            for c in (checks or [])
            if isinstance(c, dict) and not c.get("ok")
        ]
        if passed is True:
            return (True, "all checks passed")
        return (False, "; ".join(failed) or "verification failed")
    except Exception as exc:  # noqa: BLE001 — projection never breaks the tool
        logger.warning("result_contract: verification projection failed: %s", exc)
        return (None, None)


def normalize_query_result(
    result: Any,
    db: Any,
    *,
    question: str = "",
    started_at: float | None = None,
) -> NormalizedQueryResult:
    """Map an FSM ``ExecutionResult`` onto the contract.

    This is the ONLY place a data-tool outcome becomes success/failure.
    ``started_at`` (a ``time.monotonic()`` stamp taken by the caller) lets the
    contract report total wall time next to DB time, so a future investigation
    can tell slow-orchestration from slow-database without log archaeology.
    """
    import time as _time

    t0 = started_at if started_at is not None else _time.monotonic()
    normalized = _normalize_impl(result, db, question=question)
    _attach_durable_verification(normalized, db)
    try:
        total_ms = int((_time.monotonic() - t0) * 1000)
    except Exception:  # noqa: BLE001
        total_ms = None
    if isinstance(normalized.provenance, dict):
        timing = dict(normalized.provenance.get("timing") or {})
        timing["total_ms"] = total_ms
        db_ms = timing.get("db_execution_ms")
        if isinstance(db_ms, (int, float)) and total_ms:
            timing["orchestration_ms"] = max(0, total_ms - int(db_ms))
            timing["db_share_pct"] = round(100.0 * db_ms / max(total_ms, 1), 1)
        normalized.provenance["timing"] = timing
        # to_tool_result() reads .timing; keep the dataclass field in sync.
        object.__setattr__(normalized, "timing", timing)
        _log_timing(normalized, question)
    return normalized


def _log_timing(normalized: NormalizedQueryResult, question: str) -> None:
    """One structured line per data-tool execution (no rows, no secrets)."""
    t = normalized.timing or {}
    logger.info(
        "query_timing: status=%s/%s kind=%s rows=%d complete=%s "
        "db_ms=%s total_ms=%s orchestration_ms=%s db_share_pct=%s "
        "obs=%s execution_id=%s q=%r",
        normalized.execution_status.value,
        normalized.result_status.value,
        normalized.failure_kind.value,
        normalized.row_count,
        normalized.complete,
        t.get("db_execution_ms"),
        t.get("total_ms"),
        t.get("orchestration_ms"),
        t.get("db_share_pct"),
        t.get("observation_count"),
        (normalized.provenance or {}).get("execution_id"),
        (question or "")[:80],
    )


def _classify_no_execution(
    messages: list[str],
    generated_only_sql: str | None,
    provenance: dict[str, Any],
    question: str,
) -> NormalizedQueryResult:
    """Typed classification for "no rows came back".

    Order matters: an ENVIRONMENT failure (transient) must be recognised
    before the binding heuristics, otherwise a timed-out warehouse is
    misreported as a permanent binding fault and never retried. This mirrors
    the historical 2026-10-20 ordering fix.
    """
    joined = " | ".join(messages).lower()

    # 1. Transient: every recorded reason is a connection/timeout class.
    if messages and all(_is_transient_message(m) for m in messages):
        return NormalizedQueryResult(
            execution_status=ExecutionStatus.FAILED,
            result_status=ResultStatus.EXTRACTION_FAILED,
            failure_kind=FailureKind.TRANSIENT_EXECUTION_FAILURE,
            sql=generated_only_sql,
            complete=False,
            provenance=provenance,
            unreachable=True,
            detail="; ".join(messages)[:1500],
            diagnostic=(
                "The data source could not be reached — the query failed on a "
                "connection/timeout error: " + ("; ".join(messages)[:300])
                + ". This is an environment problem, not empty data and not a "
                "binding failure. Do not invent figures."
            ),
            hint=(
                "This is an environment problem (network/VPN/database down), NOT "
                "empty data and NOT a binding failure. Do NOT retry the same call "
                "and never invent figures — tell the user the data source is "
                "unreachable right now and to check connectivity."
            ),
        )

    # 2. Binding: the pipeline provably had no source to run against.
    if any(marker in joined for marker in _BINDING_MARKERS) or (
        not messages and not generated_only_sql
    ):
        return NormalizedQueryResult(
            execution_status=ExecutionStatus.NOT_EXECUTED,
            result_status=ResultStatus.EXTRACTION_FAILED,
            failure_kind=FailureKind.PERMANENT_BINDING_FAILURE,
            sql=generated_only_sql,
            provenance=provenance,
            detail="; ".join(messages)[:1500] or None,
            diagnostic=(
                "No bound data source reached the pipeline, so no query was "
                "executed. This is a binding/configuration problem and retrying "
                "it identically will reproduce it. Never tell the user there are "
                "no matching rows."
            ),
            hint=(
                "This is a pipeline/binding failure, NOT empty data. Do NOT tell "
                "the user there are no matching rows, and do NOT retry the same "
                "call — retrying reproduces it. Tell the user the query could not "
                "be completed."
            ),
        )

    # 3. SQL text exists but nothing executed.
    if generated_only_sql:
        return NormalizedQueryResult(
            execution_status=ExecutionStatus.NOT_EXECUTED,
            result_status=ResultStatus.EXTRACTION_FAILED,
            failure_kind=FailureKind.PERMANENT_BINDING_FAILURE,
            sql=generated_only_sql,
            provenance=provenance,
            detail="; ".join(messages)[:1500] or None,
            diagnostic=(
                "The NL2SQL pipeline did not execute any query — SQL was "
                "generated but never run against a bound data source. Retrying "
                "identically will reproduce this. Never present the generated "
                "SQL as a result."
            ),
            hint=(
                "This is a pipeline failure, NOT empty data. Do NOT tell the user "
                "there are no matching rows. Tell the user the query could not be "
                "completed."
            ),
        )

    # 4. Unknown structural failure — treated as transient so a bounded retry
    #    can still recover, but never silently reported as empty data.
    return NormalizedQueryResult(
        execution_status=ExecutionStatus.FAILED,
        result_status=ResultStatus.EXTRACTION_FAILED,
        failure_kind=FailureKind.TRANSIENT_EXECUTION_FAILURE,
        complete=False,
        provenance=provenance,
        detail="; ".join(messages)[:1500] or None,
        diagnostic=(
            "The NL2SQL pipeline did not execute any query — it failed before "
            "producing a result: "
            + ("; ".join(messages)[:500] or "unknown pipeline error")
            + ". Never invent figures."
        ),
        hint=(
            "This is a pipeline failure, NOT empty data. Do NOT tell the user "
            "there are no matching rows. Tell the user the query could not be "
            "completed."
        ),
    )


#: ``failure_kind`` values that must never trigger a re-execution of the
#: SQL pipeline (A3). ``transient_execution_failure`` is deliberately absent:
#: that class is the one that legitimately warrants a bounded retry.
NON_RETRYABLE_FAILURE_KINDS = frozenset(
    {
        FailureKind.PERMANENT_BINDING_FAILURE.value,
        FailureKind.RESULT_EXTRACTION_FAILURE.value,
    }
)


def iter_failure_kinds() -> Iterable[str]:
    """All enum values — handy for tests and diagnostics."""
    return (k.value for k in FailureKind)
