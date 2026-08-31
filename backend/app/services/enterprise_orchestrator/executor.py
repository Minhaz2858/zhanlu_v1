"""Multi-Facet Parallel Query Executor.

Composes the 3-6 facets produced by ``profiler.py`` against live data
sources. Each facet returns a normalized ``FacetResult``; per-facet
failures are isolated (one bad facet does not abort the others) and
degenerate results (the exact "1 row of generic aggregates" bug pattern)
are flagged ``available=False``.

Design spec reference: §8 Facet Executor — Hybrid Execution.

Two execution modes:

1. **service_call facets** — invoked via reflection against a hardcoded
   whitelist of services (see ``profiler.SERVICE_WHITELIST``). The
   executor refuses to dispatch to any service the LLM emits that is
   not in the whitelist, even if such a service exists. No arbitrary
   code execution is allowed.

2. **ad_hoc_query facets** — invoked via the existing ``ask_data_agent``
   tool (Two-Phase NL2SQL: Phase A picks tables from the TOC, Phase B
   generates SQL from the full DDL of those tables). Reuses the existing
   sub-agent loop, query-composer cache, and result cache.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from typing import Any, Awaitable, Callable, TypedDict

from app.services.enterprise_orchestrator.profiler import (
    SERVICE_WHITELIST,
    FacetSpec,
    EnterpriseIntent,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (design spec §8 — Per-facet timeout, degenerate-row gate)
# ---------------------------------------------------------------------------
PER_FACET_TIMEOUT_S = 60.0
# Aggregate-only degenerate pattern (the bug from user's screenshot: 1 row,
# {Total: 51601.685, Max volume: 51601.685, Min volume: 51601.685, ...}).
_AGGREGATE_VALUE_HINTS = frozenset({
    "total", "max", "min", "avg", "average", "count", "sum",
})
# A row where all non-id values are numeric aggregations AND all numeric
# values are equal (or nearly so) → degenerate pivoted aggregate.
_DEGENERATE_EQ_TOLERANCE = 1e-6


class FacetResult(TypedDict, total=False):
    """One facet's normalized execution outcome."""

    facet_id: str
    kind: str  # "service_call" | "ad_hoc_query"
    purpose: str  # "primary" | "auxiliary" | "contextual"
    rows: list[dict]
    summary: str
    source_sql: str
    source_label: str
    row_count: int
    warnings: list[str]
    available: bool
    unavailable_reason: str
    execution_log: list[dict]
    error: str  # populates only on hard failure


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def execute_facets(
    intent: EnterpriseIntent,
    db: Any | None = None,
    kb: Any | None = None,
    context: dict | None = None,
    user_id: str | None = None,
    service_invoker: Callable[[str, dict], Awaitable[Any]] | None = None,
    ad_hoc_invoker: Callable[[dict], Awaitable[Any]] | None = None,
) -> dict[str, FacetResult]:
    """Execute all planned facets in parallel with partial-failure isolation.

    Args:
        intent:        The ``EnterpriseIntent`` returned by profiler.
        db:            SQLAlchemy session (used by ad-hoc invoker).
        kb:            Bound ``KnowledgeBase`` row (used by service_call).
        context:       Extra context passed through to ``ask_data_agent``
                       (e.g. bound_kb_ids, concept_catalog, requested_metrics).
        user_id:       Acting user.
        service_invoker: Async callable ``(service, args) -> Any``. Defaults
                       to a reflection-based dispatcher over the whitelist.
        ad_hoc_invoker:  Async callable ``(facet_args) -> Any``. Defaults to
                       the existing ``ask_data_agent`` tool.

    Returns: ``{facet_id: FacetResult, ...}`` for ALL planned facets —
    failed facets appear with ``available=False, unavailable_reason=...``.
    NEVER raises. The orchestrator checks ``available`` to decide which
    sections to render.
    """
    facets = list(intent.get("facets") or [])
    if not facets:
        return {}

    svc_invoker = service_invoker or _default_service_invoker()
    ad_invoker = ad_hoc_invoker or _default_ad_hoc_invoker(kb=kb, context=context, user_id=user_id, db=db)

    coros = [
        asyncio.create_task(
            _execute_one_facet(
                facet,
                service_invoker=svc_invoker,
                ad_hoc_invoker=ad_invoker,
            ),
            name=f"facet:{facet.get('facet_id', '?')}",
        )
        for facet in facets
    ]
    # asyncio.gather with return_exceptions=True guarantees one facet's
    # crash does NOT abort the others. Per-facet timeouts are enforced
    # INSIDE _execute_one_facet via asyncio.wait_for, so the gather
    # returns as soon as the slowest facet completes (or times out).
    gathered = await asyncio.gather(*coros, return_exceptions=True)

    results: dict[str, FacetResult] = {}
    for facet, outcome in zip(facets, gathered):
        facet_id = facet.get("facet_id") or "?"
        if isinstance(outcome, Exception):
            logger.warning(
                "executor: facet %s raised %s; marking unavailable",
                facet_id, type(outcome).__name__,
            )
            results[facet_id] = _unavailable_result(
                facet,
                reason=f"{type(outcome).__name__}: {str(outcome)[:180]}",
            )
        else:
            results[facet_id] = outcome
    return results


# ---------------------------------------------------------------------------
# Internal: per-facet execution
# ---------------------------------------------------------------------------
async def _execute_one_facet(
    facet: FacetSpec,
    service_invoker: Callable[[str, dict], Awaitable[Any]],
    ad_hoc_invoker: Callable[[dict], Awaitable[Any]],
) -> FacetResult:
    kind = facet.get("kind")
    facet_id = facet.get("facet_id") or "unknown"
    purpose = facet.get("purpose") or "auxiliary"
    log: list[dict] = []

    async def run() -> FacetResult:
        t0 = time.monotonic()
        if kind == "service_call":
            service = facet.get("service") or ""
            if service not in SERVICE_WHITELIST:
                return _unavailable_result(
                    facet,
                    reason=f"service '{service}' not in whitelist",
                )
            args = dict(facet.get("args") or {})
            t1 = time.monotonic()
            try:
                raw = await service_invoker(service, args)
            except Exception as exc:
                log.append({
                    "step": "service_invoke",
                    "latency_ms": int((time.monotonic() - t1) * 1000),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                })
                return _unavailable_result(
                    facet,
                    reason=f"{type(exc).__name__}: {str(exc)[:180]}",
                    log=log,
                )
            log.append({
                "step": "service_invoke",
                "latency_ms": int((time.monotonic() - t1) * 1000),
                "status": "ok",
            })
            return _normalize_service_result(facet, raw, log)
        if kind == "ad_hoc_query":
            nl = facet.get("natural_language") or ""
            if not nl.strip():
                return _unavailable_result(facet, reason="empty natural_language")
            table_suggestions = list(facet.get("suggested_tables") or [])
            t1 = time.monotonic()
            try:
                raw = await ad_hoc_invoker({
                    "question": nl,
                    "suggested_tables": table_suggestions,
                })
            except Exception as exc:
                log.append({
                    "step": "ad_hoc_invoke",
                    "latency_ms": int((time.monotonic() - t1) * 1000),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                })
                return _unavailable_result(
                    facet,
                    reason=f"{type(exc).__name__}: {str(exc)[:180]}",
                    log=log,
                )
            log.append({
                "step": "ad_hoc_invoke",
                "latency_ms": int((time.monotonic() - t1) * 1000),
                "status": "ok",
            })
            return _normalize_ad_hoc_result(facet, raw, log)
        return _unavailable_result(facet, reason=f"unknown facet kind '{kind}'")

    try:
        return await asyncio.wait_for(run(), timeout=PER_FACET_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "executor: facet %s timed out after %.0fs",
            facet_id, PER_FACET_TIMEOUT_S,
        )
        log.append({
            "step": "timeout",
            "latency_ms": int(PER_FACET_TIMEOUT_S * 1000),
            "status": "timeout",
        })
        return _unavailable_result(
            facet,
            reason=f"execution timed out after {PER_FACET_TIMEOUT_S:.0f}s",
            log=log,
            purpose=purpose,
        )


# ---------------------------------------------------------------------------
# Internal: normalization + degenerate-row detection
# ---------------------------------------------------------------------------
def _normalize_service_result(
    facet: FacetSpec,
    raw: Any,
    log: list[dict] | None = None,
) -> FacetResult:
    """Map a service_call result (reflected service payload)
    into a FacetResult."""
    log = log or []
    rows, source_label, source_sql, summary = _extract_service_payload(raw)
    avail, why = _check_degeneracy(rows)
    if not avail:
        # Clear degenerate rows so the synthesizer never renders them —
        # the unavailable reason explains the gap.
        rows = []
    return FacetResult(
        facet_id=facet.get("facet_id") or "unknown",
        kind="service_call",
        purpose=facet.get("purpose") or "auxiliary",
        rows=rows,
        summary=summary or "",
        source_sql=source_sql or "",
        source_label=source_label or "",
        row_count=len(rows),
        warnings=[],
        available=avail,
        unavailable_reason="" if avail else why,
        execution_log=log,
    )


def _normalize_ad_hoc_result(
    facet: FacetSpec,
    raw: Any,
    log: list[dict] | None = None,
) -> FacetResult:
    """Map an ask_data_agent result into a FacetResult."""
    log = log or []
    rows: list[dict] = []
    source_sql = ""
    source_label = ""
    summary = ""
    warnings: list[str] = []
    if isinstance(raw, dict):
        rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
        source_sql = raw.get("sql") or raw.get("source_sql") or ""
        source_label = (
            raw.get("source_name")
            or raw.get("source_label")
            or raw.get("source_id")
            or "ask_data_agent"
        )
        summary = raw.get("answer") or raw.get("summary") or ""
        if not raw.get("success", True):
            warnings.append("ad-hoc tool returned success=False")
        if raw.get("truncated"):
            warnings.append("ad-hoc tool truncated its result")
    elif isinstance(raw, list):
        rows = raw
    else:
        warnings.append(f"ad-hoc tool returned unexpected type: {type(raw).__name__}")
    avail, why = _check_degeneracy(rows)
    if not avail:
        # keep degenerate rows hidden by clearing them — the
        # synthesizer renders this section as "(data unavailable)".
        rows = []
    return FacetResult(
        facet_id=facet.get("facet_id") or "unknown",
        kind="ad_hoc_query",
        purpose=facet.get("purpose") or "auxiliary",
        rows=rows,
        summary=summary or "",
        source_sql=source_sql or "",
        source_label=source_label or "",
        row_count=len(rows),
        warnings=warnings,
        available=avail,
        unavailable_reason="" if avail else why,
        execution_log=log,
    )


def _extract_service_payload(raw: Any) -> tuple[list[dict], str, str, str]:
    """Best-effort coercion of a service return value into (rows,
    source_label, source_sql, summary). Empty rows list means the
    service returned a stub or non-tabular payload; in that case the
    degenerate-row check will mark it unavailable."""
    if raw is None:
        return [], "", "", ""
    if isinstance(raw, list):
        rows = [r for r in raw if isinstance(r, dict)]
        return rows, "service", "", ""
    if isinstance(raw, dict):
        # Common shapes:
        #   {"rows": [...], "summary": "...", "source": "...", "sql": "..."}
        #   {"kpi": {...}, "source": "...", "summary": "...", "stub": bool}
        rows = raw.get("rows")
        if isinstance(rows, list):
            return (
                [r for r in rows if isinstance(r, dict)],
                str(raw.get("source") or "service"),
                str(raw.get("sql") or raw.get("source_sql") or ""),
                str(raw.get("summary") or raw.get("answer") or ""),
            )
        kpi = raw.get("kpi")
        if isinstance(kpi, dict):
            # Flatten kpi-block into a single derived row so the
            # synthesizer still has something to inspect, but flag as
            # likely degenerate via _check_degeneracy.
            derived = {k: v for k, v in kpi.items() if isinstance(v, (int, float, str, type(None)))}
            if derived:
                rows = [derived]
            return (
                rows,
                str(raw.get("source") or "service"),
                "",
                str(raw.get("summary") or ""),
            )
        # Last-resort: render dict-of-scalars as one row.
        scalar_row = {
            k: v for k, v in raw.items()
            if isinstance(v, (int, float, str, type(None))) and not k.startswith("_")
        }
        if scalar_row:
            return [scalar_row], "service", "", str(raw.get("summary") or "")
        return [], str(raw.get("source") or "service"), "", str(raw.get("summary") or "")
    return [], "service", "", f"service returned unexpected type {type(raw).__name__}"


def _check_degeneracy(rows: list[dict]) -> tuple[bool, str]:
    """Detect the exact bug pattern: 0 rows or 1 row of generic aggregates.

    A row is "generic aggregate" when its non-id columns are all numeric
    values within a tight tolerance of each other AND names hint at
    aggregate functions (Total/Max/Min/Avg/Count/Sum).
    """
    if not rows:
        return False, "no rows returned"
    if len(rows) == 1:
        only = rows[0]
        if isinstance(only, dict) and _is_pivoted_aggregate_row(only):
            return False, "degenerate result: 1 row of generic aggregates"
    if len(rows) >= 2:
        # Heuristic: if every row has the same single numeric value AND
        # there are only 1-2 rows, it's almost certainly pivoted.
        numeric_vals: list[float] = []
        if isinstance(rows[0], dict):
            for v in rows[0].values():
                if isinstance(v, (int, float)):
                    numeric_vals.append(float(v))
        if (
            1 <= len(rows) <= 3
            and numeric_vals
            and len(set(round(v, 6) for v in numeric_vals)) == 1
            and any(
                any(hint in str(k).lower() for hint in _AGGREGATE_VALUE_HINTS)
                for k in rows[0].keys()
                if isinstance(k, str)
            )
        ):
            return False, "degenerate result: pivoted aggregate row"
    return True, ""


def _is_pivoted_aggregate_row(row: dict) -> bool:
    """A row where columns are named like aggregate functions AND all
    numeric values are equal (the user's bug screenshot)."""
    keys = [str(k).lower() for k in row.keys() if isinstance(k, str)]
    if not keys:
        return False
    agg_key_count = sum(
        1 for k in keys
        if any(hint == k.split()[0] or hint in k for hint in _AGGREGATE_VALUE_HINTS)
    )
    if agg_key_count < 2:
        return False
    numeric_vals = [v for v in row.values() if isinstance(v, (int, float))]
    if not numeric_vals:
        return False
    return max(numeric_vals) - min(numeric_vals) < _DEGENERATE_EQ_TOLERANCE or len(
        set(round(v, 6) for v in numeric_vals)
    ) == 1


def _unavailable_result(
    facet: FacetSpec,
    reason: str,
    log: list[dict] | None = None,
    purpose: str | None = None,
) -> FacetResult:
    return FacetResult(
        facet_id=facet.get("facet_id") or "unknown",
        kind=facet.get("kind") or "ad_hoc_query",
        purpose=purpose or facet.get("purpose") or "auxiliary",
        rows=[],
        summary="",
        source_sql="",
        source_label="",
        row_count=0,
        warnings=[],
        available=False,
        unavailable_reason=reason[:240],
        execution_log=log or [],
    )


# ---------------------------------------------------------------------------
# Default invokers
# ---------------------------------------------------------------------------
def _default_service_invoker() -> Callable[[str, dict], Awaitable[Any]]:
    """Reflection-based service_call invoker, restricted to whitelisted
    fully-qualified paths. We import the module lazily and cache the
    resolved method; the LLM cannot influence the lookup target.

    SERVICE_WHITELIST entries follow the form ``ClassName.method_name``
    (e.g. ``ReportService.sales_summary``). The Class → module
    mapping is the authoritative allowlist — even if a Class with the
    right name exists elsewhere, this resolver refuses it.
    """
    cache: dict[str, tuple[Any, str, str]] = {}  # service → (method, class_name, method_name)

    def _resolve(service: str) -> tuple[Any, str, str]:
        if service in cache:
            return cache[service]
        class_name, _, method_name = service.rpartition(".")
        if (
            class_name not in _CLASS_TO_MODULE
            or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", method_name)
        ):
            raise PermissionError(
                f"service '{service}' is not in allowed class set"
            )
        module_path = _CLASS_TO_MODULE[class_name]
        mod = __import__(module_path, fromlist=[class_name])
        cls = getattr(mod, class_name, None)
        method = getattr(cls, method_name, None) if cls else None
        if method is None or not callable(method):
            raise PermissionError(f"service '{service}' not callable")
        cache[service] = (method, class_name, method_name)
        return cache[service]

    async def invoker(service: str, args: dict) -> Any:
        method, class_name, method_name = _resolve(service)
        # Strict-kwargs to avoid injection of unexpected arguments.
        try:
            sig = inspect.signature(method)
            params = sig.parameters
            kwargs = {k: v for k, v in args.items() if k in params}
            # Service methods are typically unbound instance methods
            # carrying a ``self`` parameter — construct the class with
            # whatever ``args`` overlap `__init__` (excluding `self`).
            has_self = "self" in params
            if has_self:
                # Resolve the class through the authoritative _CLASS_TO_MODULE
                # map (never a hardcoded instance allowlist).
                module_path = _CLASS_TO_MODULE[class_name]
                mod = __import__(module_path, fromlist=[class_name])
                cls_obj = getattr(mod, class_name)
                init_sig = inspect.signature(cls_obj.__init__)
                init_kwargs = {
                    k: v for k, v in args.items()
                    if k in init_sig.parameters and k != "self"
                }
                inst = cls_obj(**init_kwargs)
                res = getattr(inst, method_name)(**kwargs)
            else:
                res = method(**kwargs)
            if inspect.isawaitable(res):
                return await res
            return res
        except TypeError as exc:
            raise PermissionError(f"invalid args for {service}: {exc}") from exc

    return invoker


# Class-name → module-path mapping. The profiler's SERVICE_WHITELIST
# uses the ``ClassName.method_name`` form; the executor resolves each
# class name to its actual Python module here. This is the authoritative
# whitelist — the LLM cannot dispatch to arbitrary Python code.
# Currently empty: the platform ships no reflected service classes, so
# service_call facets are refused and the ad_hoc/NL2SQL path handles
# every facet. Add entries here (plus profiler SERVICE_WHITELIST) when a
# generic service is registered.
_CLASS_TO_MODULE: dict[str, str] = {}


def _default_ad_hoc_invoker(
    *,
    kb: Any | None,
    context: dict | None,
    user_id: str | None,
    db: Any | None,
) -> Callable[[dict], Awaitable[Any]]:
    """ad_hoc_query invoker using the existing ask_data_agent tool.

    Falls back to a synthetic empty payload (NOT raising) when
    ask_data_agent cannot be imported — the orchestrator's
    "ALL facets fail" handling then takes over.
    """

    async def invoker(facet_args: dict) -> Any:
        question = facet_args.get("question") or ""
        suggested_tables = facet_args.get("suggested_tables") or []
        try:
            from app.services.tool_handlers.delegation_tools import (
                _ask_data_agent as ask_data_agent_handler,
            )

            kwargs: dict[str, Any] = {
                "question": question,
                "data_source_id": getattr(kb, "id", None) if kb else None,
            }
            inner_context: dict[str, Any] = dict(context or {})
            if suggested_tables:
                inner_context["suggested_tables"] = suggested_tables
            if kb is not None:
                inner_context.setdefault(
                    "bound_kb_ids", [getattr(kb, "id", None)]
                )
            return await ask_data_agent_handler(
                kwargs, db=db, user_id=user_id, context=inner_context or None,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "executor: ad_hoc_invoker failed (%s); returning synthetic empty",
                exc,
            )
            return {
                "success": False,
                "error": str(exc)[:200],
                "rows": [],
                "sql": "",
                "source_name": "ask_data_agent (invoker-failed)",
            }

    return invoker
