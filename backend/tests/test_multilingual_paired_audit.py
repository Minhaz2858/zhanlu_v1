"""Phase 3.5 P4 — focused zh/en classification equivalence audit.

Extends test_multilingual_recovery with: mixed Chinese-English messages,
database-specific error strings (MySQL/Postgres rendered in Chinese ops
environments), and decision-equivalence checks. Deterministic; no LLM.

Run: pytest tests/test_multilingual_paired_audit.py -q
"""
import pytest

from app.services.execution_decisions import (
    ExecutionObservation,
    evaluate_observation,
)
from app.services.execution_recovery import (
    FailureCategory,
    classify_failure,
    localized_recovery_hint,
)


# (zh error, en error, expected category)
PAIRED = [
    # Unknown column/table → resource_not_found is the EXISTING contract
    # (discoverable via list/describe — mission recovery table row 8).
    ("数据查询失败：Unknown column 'cost' in 'field list'",
     "query failed: Unknown column 'cost' in 'field list'",
     FailureCategory.resource_not_found),
    ("表 'production_orders' 不存在", "table 'production_orders' doesn't exist",
     FailureCategory.resource_not_found),
    ("数据库连接超时", "database connection timed out", FailureCategory.timeout),
    # Chinese permission errors
    ("您没有权限访问财务数据", "you do not have permission to access financial data",
     FailureCategory.permission_denied),
    ("RBAC 策略拒绝了该操作", "RBAC policy denied this operation",
     FailureCategory.permission_denied),
    # Chinese tool validation errors
    ("工具调用失败：缺少必填参数 data_source_id",
     "tool call failed: missing required parameter data_source_id",
     FailureCategory.missing_required_parameter),
    # Chinese timeout / transient
    ("上游服务超时（30 秒）", "upstream service timed out (30s)",
     FailureCategory.timeout),
    # Chinese missing-resource
    ("未找到知识库 kb-123", "knowledge base kb-123 not found",
     FailureCategory.resource_not_found),
]


@pytest.mark.parametrize("zh,en,expected", PAIRED, ids=[p[1][:32] for p in PAIRED])
def test_paired_classification_equivalence(zh, en, expected):
    zh_cls = classify_failure({"success": False, "error": zh})
    en_cls = classify_failure({"success": False, "error": en})
    assert zh_cls.category == expected
    assert en_cls.category == expected
    assert zh_cls.retryable == en_cls.retryable
    assert zh_cls.terminal == en_cls.terminal


def test_mixed_language_message_classifies():
    # Real-world ops messages mix languages freely.
    cls = classify_failure({
        "success": False,
        "error": "执行失败: connection reset by peer (连接被重置)",
    })
    assert cls.category == FailureCategory.connection_error
    assert cls.retryable is True


@pytest.mark.parametrize("zh,en,expected", [
    ("查询超时", "query timed out", "timeout"),
    ("权限不足", "permission denied", "permission_denied"),
])
def test_equivalent_failures_yield_equivalent_decisions(zh, en, expected):
    kw = dict(attempts=0, budget_left=True, is_success=False)
    zh_d = evaluate_observation(ExecutionObservation(
        category=classify_failure({"success": False, "error": zh}).category.value,
        triggering_observation=zh, **kw))
    en_d = evaluate_observation(ExecutionObservation(
        category=classify_failure({"success": False, "error": en}).category.value,
        triggering_observation=en, **kw))
    assert zh_d.decision == en_d.decision


def test_localized_hints_cover_all_categories_both_languages():
    for cat in FailureCategory:
        en = localized_recovery_hint(cat, "en")
        zh = localized_recovery_hint(cat, "zh")
        assert en and zh, f"{cat}: missing localized hint"
        assert zh != en, f"{cat}: zh hint must differ from en"


def test_canonical_enums_untouched_by_language():
    # The category VALUE is language-independent — never translated.
    assert classify_failure({"success": False, "error": "超时"}).category.value \
        == classify_failure({"success": False, "error": "timed out"}).category.value \
        == "timeout"
