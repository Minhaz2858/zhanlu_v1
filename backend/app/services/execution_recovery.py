"""Shared failure taxonomy + bounded healing policy (Phase 2.1, 2026-10-31).

ROOT CAUSE ADDRESSED
--------------------
Failure classification was fragmented across three unrelated taxonomies
(string markers in ``agent_tools``, typed ``FailureKind`` for data tools only,
``NON_ACTIONABLE_MARKERS`` in synexia). Recovery therefore could not be
selected systematically, unknown errors defaulted to retryable (the documented
RC-1 loop), and mutating writes were retry-classified without idempotency
consultation.

This module is the SINGLE classification authority:

- ``classify_failure`` — typed result fields win, then exception type, then
  the existing string-marker semantics (REUSED via import, never copied).
- ``should_retry`` / ``should_reformulate`` — the bounded healing decisions.
- ``HealingBudget`` / ``RecoveryPolicy`` — per-call and per-task bounds.

Design rules (mission §3):
- An empty result is a SUCCESS with zero rows, never a failure.
- Permission/authorization failures are TERMINAL — never retried, never
  bypassed.
- Deterministic invalid arguments are never retried with identical arguments;
  they are eligible for the (existing, bounded) argument-reformulation path.
- Mutating (consequential) tools are only retried with idempotency evidence.
- Every classification carries a structured ``recovery_hint`` the ReAct
  observation can surface — feedback, not a hardcoded action.

Pure module: no I/O, no settings import at module level (policy reads them
lazily) — trivially testable.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any

# ── Taxonomies ────────────────────────────────────────────────────────────

class FailureCategory(str, enum.Enum):
    """Mission §3.1 failure categories."""

    invalid_arguments = "invalid_arguments"
    schema_validation_error = "schema_validation_error"
    missing_required_parameter = "missing_required_parameter"
    permission_denied = "permission_denied"
    resource_not_found = "resource_not_found"
    resource_unavailable = "resource_unavailable"
    connection_error = "connection_error"
    timeout = "timeout"
    rate_limit = "rate_limit"
    provider_error = "provider_error"
    database_error = "database_error"
    empty_result = "empty_result"
    malformed_output = "malformed_output"
    partial_result = "partial_result"
    unsupported_operation = "unsupported_operation"
    unknown_error = "unknown_error"


class FailureOutcome(str, enum.Enum):
    """The six-way outcome distinction (empty ≠ failed)."""

    success = "success"
    success_with_empty_result = "success_with_empty_result"
    success_with_partial_data = "success_with_partial_data"
    execution_failure = "execution_failure"
    authorization_failure = "authorization_failure"
    invalid_request = "invalid_request"
    temporary_infrastructure_failure = "temporary_infrastructure_failure"


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    outcome: FailureOutcome
    retryable: bool = False
    requires_modified_arguments: bool = False
    terminal: bool = False
    recovery_hint: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        """Structured observation payload (model- and event-readable)."""
        return {
            "failure_category": self.category.value,
            "outcome": self.outcome.value,
            "retryable": self.retryable,
            "requires_modified_arguments": self.requires_modified_arguments,
            "terminal": self.terminal,
            "recovery_hint": self.recovery_hint,
            "detail": self.detail,
        }


# ── Marker tables (semantics reused from agent_tools / replan_policy) ─────
_MARKERS = {
    FailureCategory.permission_denied: (
        "permission denied", "policy denied", "access denied", "not authorized",
        "unauthorized", "403", "source_verification_refused", "blocked by hook",
        # Phase 3.5 P4 audit: additional real-world phrasings.
        "do not have permission", "no permission", "not permitted",
    ),
    FailureCategory.unsupported_operation: (
        "unknown tool", "only select statements are allowed",
        "blocked statement type", "not supported", "unsupported",
        "multi-statement sql is not allowed", "operation not permitted",
    ),
    FailureCategory.resource_not_found: (
        "not found", "no such", "404", "does not exist", "unknown table",
        "unknown column",
        # Phase 3.5 P4 audit: contraction form used by MySQL/psycopg.
        "doesn't exist", "doesn‘t exist",
    ),
    FailureCategory.invalid_arguments: (
        "invalid arguments", "invalid_argument", "failed validating",
        "jsonschema", "is not of type", "is not valid under any of the",
        "was unexpected", "already exists", "conflict", "409",
    ),
    FailureCategory.timeout: (
        "timeout", "timed out", "deadline exceeded", "time out",
    ),
    FailureCategory.rate_limit: (
        "rate limit", "429", "too many requests", "quota exceeded",
        "quota exhausted", "budget exhausted", "resource_exhausted",
    ),
    FailureCategory.connection_error: (
        "connect", "refused", "reset", "temporar", "unreachable",
        "connection", "getaddrinfo", "502", "503",
    ),
    FailureCategory.invalid_arguments: (
        "invalid arguments", "invalid_argument", "failed validating",
        "jsonschema", "is not of type", "is not valid under any of the",
        "was unexpected",
    ),
    FailureCategory.missing_required_parameter: (
        "is a required property", "required parameter", "missing parameter",
        "is required", "missing required",
    ),
    FailureCategory.malformed_output: (
        "malformed output", "failed to parse", "json decode", "expecting value",
        "invalid json", "truncated response", "result_extraction_failure",
    ),
    FailureCategory.database_error: (
        "operationalerror", "programmingerror", "integrityerror", "deadlock",
        "lock wait", "sqlsyntaxerror", "database", "syntax error",
        "permanent_binding_failure",
    ),
    FailureCategory.provider_error: (
        "driver_missing", "provider", "upstream", "apify", "model returned",
    ),
}

# Phase 3.3: Chinese enterprise error markers — MERGED into the existing
# tuples (dict-literal duplicate keys would silently WIPE the English
# entries). Canonical categories stay English enums; this only makes
# CLASSIFICATION language-independent. Precedence unchanged (_MARKER_ORDER:
# permission first, timeout before generic connection).
_MARKERS_ZH = {
    FailureCategory.permission_denied: (
        "权限不足", "权限被拒绝", "无权访问", "访问被拒绝", "未授权", "拒绝访问",
        # Phase 3.5 P4 audit: additional real-world zh phrasings.
        "没有权限", "策略拒绝", "被拒绝", "权限拒绝",
    ),
    FailureCategory.unsupported_operation: (
        "不支持", "未被支持", "无法执行该操作",
    ),
    FailureCategory.rate_limit: (
        "频率超限", "请求过于频繁", "配额已用尽", "配额不足", "限流",
    ),
    FailureCategory.timeout: (
        "超时", "请求超时", "连接超时", "执行超时",
    ),
    FailureCategory.connection_error: (
        "连接失败", "连接错误", "无法连接", "网络错误", "服务暂时不可用",
        "服务不可用", "暂时不可用",
    ),
    FailureCategory.invalid_arguments: (
        "参数无效", "参数错误", "无效参数", "参数校验失败", "字段类型不匹配",
    ),
    FailureCategory.missing_required_parameter: (
        "缺少必填", "缺少参数", "必填字段", "参数缺失",
    ),
    FailureCategory.malformed_output: (
        "解析失败", "返回格式错误", "无效的json", "输出被截断",
    ),
    FailureCategory.database_error: (
        "数据库错误", "sql语法错误", "语法错误", "死锁", "锁等待", "违反唯一约束",
    ),
    FailureCategory.resource_not_found: (
        "未找到", "不存在", "没有找到", "找不到",
    ),
}
for _cat, _zh in _MARKERS_ZH.items():
    _MARKERS[_cat] = _MARKERS.get(_cat, ()) + _zh
del _cat, _zh
# Order matters: first match wins (permission before generic "not found",
# timeout before generic "connection" since "connection timeout" is a timeout).
_MARKER_ORDER = (
    FailureCategory.permission_denied,
    FailureCategory.unsupported_operation,
    FailureCategory.rate_limit,
    FailureCategory.timeout,
    FailureCategory.connection_error,
    FailureCategory.invalid_arguments,
    FailureCategory.missing_required_parameter,
    FailureCategory.malformed_output,
    FailureCategory.database_error,
    FailureCategory.provider_error,
    FailureCategory.resource_not_found,
)

_ARG_FIXABLE_CATEGORIES = {
    FailureCategory.invalid_arguments,
    FailureCategory.schema_validation_error,
    FailureCategory.missing_required_parameter,
}

_RECOVERY_HINTS = {
    FailureCategory.resource_unavailable: (
        "The resource is temporarily unavailable — bounded retry if "
        "read-only, otherwise report the limitation honestly."
    ),
    FailureCategory.empty_result: (
        "Verify filters, date range and data availability; probe the latest "
        "posted date before narrowing the window."
    ),
    FailureCategory.partial_result: (
        "Report the verified portion; identify exactly what is missing."
    ),
    FailureCategory.permission_denied: (
        "Stop — never bypass authorization; report the limitation or request "
        "approval through the governance flow."
    ),
    FailureCategory.unsupported_operation: (
        "Do not retry; choose an authorized alternative capability or report "
        "the limitation."
    ),
    FailureCategory.resource_not_found: (
        "Discover valid resources (list/describe) or report the limitation; "
        "do not retry identical arguments."
    ),
    FailureCategory.timeout: (
        "One bounded retry is safe for read-only operations; otherwise report "
        "the limitation honestly."
    ),
    FailureCategory.rate_limit: (
        "Respect retry-after guidance with backoff; do not hammer the provider."
    ),
    FailureCategory.connection_error: (
        "Bounded retry if the operation is read-only/idempotent."
    ),
    FailureCategory.invalid_arguments: (
        "Correct arguments using the tool schema and this error feedback."
    ),
    FailureCategory.schema_validation_error: (
        "Reconstruct valid arguments against the tool schema."
    ),
    FailureCategory.missing_required_parameter: (
        "Retrieve the missing parameter from context or ask the user."
    ),
    FailureCategory.malformed_output: (
        "Re-execute safely or report the limitation; never fabricate the "
        "missing output."
    ),
    FailureCategory.database_error: (
        "Inspect the database error; a syntax/binding error needs corrected "
        "SQL or arguments, a deadlock/lock may retry once."
    ),
    FailureCategory.provider_error: (
        "Use an authorized available fallback or report the provider failure."
    ),
    FailureCategory.unknown_error: (
        "Inspect the error; retry only once within budget and only for "
        "read-only operations."
    ),
}


# Phase 3.3: localized PRESENTATION of recovery hints. Canonical categories
# and the default (English) hint are unchanged — localization is opt-in at
# the presentation boundary (SSE/事件流), never in stored classifications.
_RECOVERY_HINTS_ZH = {
    FailureCategory.resource_unavailable: "资源暂时不可用——只读操作可在预算内重试，否则请如实报告限制。",
    FailureCategory.empty_result: "请核对筛选条件、日期范围与数据可用性；先查询最新数据日期再缩小窗口。",
    FailureCategory.partial_result: "请如实报告已核实部分，并明确指出缺失内容。",
    FailureCategory.permission_denied: "已停止——绝不绕过授权；请如实说明限制或通过治理流程申请审批。",
    FailureCategory.unsupported_operation: "请勿重试；请选择经授权的其他能力或如实报告限制。",
    FailureCategory.resource_not_found: "请先枚举有效资源（list/describe）或如实报告限制；请勿用相同参数重试。",
    FailureCategory.timeout: "只读操作可在预算内进行一次有界重试；否则请如实报告限制。",
    FailureCategory.rate_limit: "请遵循 retry-after 指引进行退避，不要持续请求。",
    FailureCategory.connection_error: "只读/幂等操作可进行有界重试。",
    FailureCategory.invalid_arguments: "请依据工具结构与该错误反馈修正参数。",
    FailureCategory.schema_validation_error: "请依据工具结构重建合法参数。",
    FailureCategory.missing_required_parameter: "请从上下文中补全缺失参数，或向用户询问。",
    FailureCategory.malformed_output: "请安全地重新执行或如实报告限制；绝不编造缺失的输出。",
    FailureCategory.database_error: "请检查数据库错误：语法/绑定错误需修正 SQL 或参数；死锁/锁等待可重试一次。",
    FailureCategory.provider_error: "请使用经授权的可用备选能力，或如实报告提供方故障。",
    FailureCategory.unknown_error: "请检查该错误；仅在预算内且仅对只读操作重试一次。",
}


def localized_recovery_hint(category: FailureCategory, lang: str) -> str:
    """Localized recovery hint (presentation only — never persisted into a
    classification). Unknown languages fall back to English."""
    if str(lang or "").lower().startswith("zh"):
        return _RECOVERY_HINTS_ZH.get(
            category, _RECOVERY_HINTS.get(category, "")
        )
    return _RECOVERY_HINTS.get(category, "")


# ── Classification ────────────────────────────────────────────────────────

def _transient(category: FailureCategory, detail: str = "") -> FailureClassification:
    return FailureClassification(
        category=category,
        outcome=FailureOutcome.temporary_infrastructure_failure,
        retryable=True,
        recovery_hint=_RECOVERY_HINTS[category],
        detail=detail,
    )


def _failure(category: FailureCategory, detail: str = "") -> FailureClassification:
    return FailureClassification(
        category=category,
        outcome=FailureOutcome.execution_failure,
        retryable=False,
        recovery_hint=_RECOVERY_HINTS[category],
        detail=detail,
    )


def classify_failure(
    result: dict | None = None,
    error: BaseException | None = None,
    *,
    tool_name: str = "",
    mutating: bool = False,
) -> FailureClassification:
    """Classify a tool outcome. Typed fields > exception type > markers.

    ``mutating`` marks the tool as consequential (write) — unknown failures on
    such tools are never retryable (idempotency unknown).
    """
    # ── 1. Result-dict signals ────────────────────────────────────────
    if isinstance(result, dict):
        if result.get("success"):
            if result.get("rows") == [] or result.get("error_kind") == "empty_set":
                return FailureClassification(
                    category=FailureCategory.empty_result,
                    outcome=FailureOutcome.success_with_empty_result,
                    recovery_hint=_RECOVERY_HINTS[FailureCategory.empty_result],
                )
            if result.get("truncated") or result.get("partial"):
                return FailureClassification(
                    category=FailureCategory.partial_result,
                    outcome=FailureOutcome.success_with_partial_data,
                    recovery_hint=_RECOVERY_HINTS[FailureCategory.partial_result],
                )
            return FailureClassification(
                category=FailureCategory.empty_result if not result.get("rows")
                else FailureCategory.unknown_error,
                outcome=FailureOutcome.success,
            )
        if result.get("requires_approval"):
            return FailureClassification(
                category=FailureCategory.permission_denied,
                outcome=FailureOutcome.authorization_failure,
                terminal=True,
                recovery_hint=(
                    "Request user approval; pause — the operation must never "
                    "be retried or rerouted automatically."
                ),
                detail="approval pending",
            )
        if result.get("plan_mode"):
            return FailureClassification(
                category=FailureCategory.unsupported_operation,
                outcome=FailureOutcome.invalid_request,
                terminal=True,
                recovery_hint=(
                    "A plan proposal was returned — a user decision is "
                    "required, retrying cannot change it."
                ),
                detail="plan_mode proposal",
            )
        kind = str(result.get("failure_kind") or "")
        if kind:
            if kind in ("result_extraction_failure",):
                return _failure(FailureCategory.malformed_output, kind)
            if kind in ("permanent_binding_failure",):
                return _failure(FailureCategory.database_error, kind)
            # transient_execution_failure and unknown kinds keep falling through
            # to marker/exception analysis, but stay bounded by policy.
        error_kind = str(result.get("error_kind") or "")
        if error_kind in ("driver_missing",):
            return _transient(FailureCategory.provider_error, error_kind)
        if error_kind in ("turn_sql_budget_exhausted",):
            return _failure(FailureCategory.rate_limit, error_kind)
        if error_kind in ("source_verification_refused",):
            return FailureClassification(
                category=FailureCategory.permission_denied,
                outcome=FailureOutcome.authorization_failure,
                terminal=True,
                recovery_hint=_RECOVERY_HINTS[FailureCategory.permission_denied],
                detail=error_kind,
            )
        if error_kind in ("source_verification_clarify",):
            return FailureClassification(
                category=FailureCategory.missing_required_parameter,
                outcome=FailureOutcome.invalid_request,
                requires_modified_arguments=True,
                recovery_hint=_RECOVERY_HINTS[FailureCategory.missing_required_parameter],
                detail=error_kind,
            )
        # Explicit handler retryable flag with no richer signal.
        if result.get("retryable") is True and not _match_markers(result.get("error")):
            return _transient(FailureCategory.unknown_error, "handler retryable flag")

    # ── 2. Exception type ─────────────────────────────────────────────
    if error is not None:
        return _classify_exception(error, mutating=mutating)

    # ── 3. String markers on the error message ────────────────────────
    matched = _match_markers((result or {}).get("error") if isinstance(result, dict) else None)
    if matched is not None:
        cat = matched
        if cat in (FailureCategory.permission_denied, FailureCategory.unsupported_operation):
            outcome = (
                FailureOutcome.authorization_failure
                if cat == FailureCategory.permission_denied
                else FailureOutcome.invalid_request
            )
            return FailureClassification(
                category=cat,
                outcome=outcome,
                terminal=True,
                recovery_hint=_RECOVERY_HINTS[cat],
                detail=(result or {}).get("error", "")[:300],
            )
        if cat in _ARG_FIXABLE_CATEGORIES:
            return FailureClassification(
                category=cat,
                outcome=FailureOutcome.invalid_request,
                requires_modified_arguments=True,
                recovery_hint=_RECOVERY_HINTS[cat],
                detail=(result or {}).get("error", "")[:300],
            )
        if cat in (FailureCategory.timeout, FailureCategory.connection_error, FailureCategory.rate_limit):
            return _transient(cat, (result or {}).get("error", "")[:300])
        c = _failure(cat, (result or {}).get("error", "")[:300])
        # Deadlock / lock-wait style database errors are the one transient
        # database case (existing marker semantics).
        if cat == FailureCategory.database_error and any(
            m in str((result or {}).get("error", "")).lower()
            for m in ("deadlock", "lock wait")
        ):
            return _transient(cat, c.detail)
        return c

    # ── 4. Unknown ────────────────────────────────────────────────────
    detail = ""
    if isinstance(result, dict):
        detail = str(result.get("error") or "")[:300]
    elif error is not None:
        detail = str(error)[:300]
    c = _failure(FailureCategory.unknown_error, detail)
    if mutating:
        # G3: never blindly re-drive a consequential write whose outcome is
        # unknown — the first attempt may have landed.
        c = FailureClassification(
            category=c.category, outcome=c.outcome, retryable=False,
            requires_modified_arguments=False, terminal=False,
            recovery_hint=(
                "The write outcome is unknown — do NOT retry blindly; verify "
                "the resource state first (read-only check), then decide."
            ),
            detail=c.detail,
        )
    return c


def _classify_exception(error: BaseException, *, mutating: bool = False) -> FailureClassification:
    import asyncio as _asyncio

    if isinstance(error, _asyncio.TimeoutError) or isinstance(error, TimeoutError):
        return _transient(FailureCategory.timeout, str(error)[:300])
    if isinstance(error, PermissionError):
        return FailureClassification(
            category=FailureCategory.permission_denied,
            outcome=FailureOutcome.authorization_failure,
            terminal=True,
            recovery_hint=_RECOVERY_HINTS[FailureCategory.permission_denied],
            detail=str(error)[:300],
        )
    if isinstance(error, ConnectionError):
        return _transient(FailureCategory.connection_error, str(error)[:300])
    if isinstance(error, (json.JSONDecodeError,)):
        return _failure(FailureCategory.malformed_output, str(error)[:300])
    if isinstance(error, KeyError):
        return FailureClassification(
            category=FailureCategory.missing_required_parameter,
            outcome=FailureOutcome.invalid_request,
            requires_modified_arguments=True,
            recovery_hint=_RECOVERY_HINTS[FailureCategory.missing_required_parameter],
            detail=str(error)[:300],
        )
    matched = _match_markers(str(error))
    if matched is not None:
        return classify_failure({"success": False, "error": str(error)}, mutating=mutating)
    return classify_failure({"success": False, "error": str(error)}, mutating=mutating)


def _match_markers(message: Any) -> FailureCategory | None:
    if not message:
        return None
    low = str(message).lower()
    for cat in _MARKER_ORDER:
        for marker in _MARKERS[cat]:
            if marker in low:
                return cat
    return None


# ── Bounded healing policy ────────────────────────────────────────────────

EXEC_RECOVERY_MAX_PER_TASK_DEFAULT = 4


@dataclass(frozen=True)
class RecoveryPolicy:
    max_attempts_per_call: int = 2
    max_healing_per_task: int = EXEC_RECOVERY_MAX_PER_TASK_DEFAULT
    write_idempotency_required: bool = True

    @classmethod
    def from_settings(cls) -> "RecoveryPolicy":
        try:
            from app.config import settings

            return cls(
                max_attempts_per_call=int(getattr(settings, "EXEC_RECOVERY_MAX_ATTEMPTS_PER_CALL", 2)),
                max_healing_per_task=int(getattr(settings, "EXEC_RECOVERY_MAX_PER_TASK", EXEC_RECOVERY_MAX_PER_TASK_DEFAULT)),
                write_idempotency_required=bool(
                    getattr(settings, "EXEC_RECOVERY_WRITE_IDEMPOTENCY_REQUIRED", True)
                ),
            )
        except Exception:  # noqa: BLE001 — settings optional (pure fallback)
            return cls()


def should_retry(
    classification: FailureClassification,
    *,
    attempts: int = 0,
    mutating: bool = False,
    idempotent: bool = False,
    task_healing_budget_left: bool = True,
    max_attempts: int = 2,
) -> bool:
    """The bounded retry decision. NEVER retries: permission/terminal
    failures, approval pauses, deterministic argument errors (those need the
    reformulation path), or consequential writes without idempotency."""
    if classification.retryable is not True:
        return False
    if classification.terminal:
        return False
    if attempts >= max_attempts:
        return False
    if not task_healing_budget_left:
        return False
    if mutating and not idempotent:
        return False
    return True


def should_reformulate(classification: FailureClassification) -> bool:
    """Argument-reformulation eligibility: ONLY deterministic argument
    problems (correcting them changes the outcome). Permission/infra failures
    never go through the LLM arg-repair path."""
    return (
        classification.requires_modified_arguments
        and classification.category in _ARG_FIXABLE_CATEGORIES
        and not classification.terminal
    )


class HealingBudget:
    """Per-task healing budget — bounds TOTAL recovery actions for one turn."""

    def __init__(self, max_healings: int = EXEC_RECOVERY_MAX_PER_TASK_DEFAULT):
        self._max = max(0, int(max_healings))
        self._used: dict[str, int] = {}

    @property
    def remaining(self) -> int:
        return max(0, self._max - sum(self._used.values()))

    def attempts(self, tool_name: str) -> int:
        return self._used.get(tool_name, 0)

    def consume(self, tool_name: str) -> bool:
        if self.remaining <= 0:
            return False
        self._used[tool_name] = self._used.get(tool_name, 0) + 1
        return True
