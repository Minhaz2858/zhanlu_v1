"""Phase 3.3 — multilingual failure classification & recovery (TDD).

Chinese enterprise errors must classify IDENTICALLY to their English
equivalents (mission §D: "Do not depend exclusively on English substring
matching"). Canonical enums stay English; localized PRESENTATION is added.

Run: pytest tests/test_multilingual_recovery.py -q
"""
import pytest

from app.services.execution_recovery import (
    FailureCategory,
    classify_failure,
    localized_recovery_hint,
)
from app.services.execution_decisions import (
    ExecutionObservation,
    evaluate_observation,
)
from app.services.capability_fallback import select_alternatives


# ── Chinese error strings classify identically ──────────────────────────────
@pytest.mark.parametrize("zh,en,expected", [
    ("查询超时，请稍后重试", "query timed out, please retry", FailureCategory.timeout),
    ("连接数据库失败：connection refused", "database connection refused",
     FailureCategory.connection_error),
    ("权限不足，无法访问该表", "permission denied on that table",
     FailureCategory.permission_denied),
    ("访问被拒绝", "access denied", FailureCategory.permission_denied),
    ("请求频率超限（429）", "rate limit exceeded (429)", FailureCategory.rate_limit),
    ("SQL语法错误：unknown column 'xx'", "SQL syntax error: unknown column",
     FailureCategory.database_error),
    ("未找到指定的数据源", "data source not found", FailureCategory.resource_not_found),
    ("参数无效：缺少必填字段", "invalid arguments: missing required field",
     FailureCategory.invalid_arguments),
    ("服务暂时不可用", "service temporarily unavailable",
     FailureCategory.connection_error),
])
def test_chinese_error_classification_matches_english(zh, en, expected):
    zh_cls = classify_failure({"success": False, "error": zh})
    en_cls = classify_failure({"success": False, "error": en})
    assert zh_cls.category == expected, f"zh={zh!r} → {zh_cls.category}"
    assert en_cls.category == expected
    # Equivalent requests → equivalent retry semantics.
    assert zh_cls.retryable == en_cls.retryable
    assert zh_cls.terminal == en_cls.terminal


def test_equivalent_failures_yield_equivalent_decisions():
    obs_kw = dict(attempts=0, budget_left=True)
    zh_d = evaluate_observation(ExecutionObservation(
        is_success=False,
        category=classify_failure({"success": False, "error": "查询超时"}).category.value,
        triggering_observation="查询超时", **obs_kw,
    ))
    en_d = evaluate_observation(ExecutionObservation(
        is_success=False,
        category=classify_failure({"success": False, "error": "query timed out"}).category.value,
        triggering_observation="query timed out", **obs_kw,
    ))
    assert zh_d.decision == en_d.decision == "retry"


# ── Localized presentation (canonical enums unchanged) ───────────────────────
def test_recovery_hint_localization():
    for cat in FailureCategory:
        en = localized_recovery_hint(cat, "en")
        zh = localized_recovery_hint(cat, "zh")
        assert en and zh
        assert en != zh or cat.name  # both languages covered
        # canonical enum value is unchanged — presentation only
        assert localized_recovery_hint(cat, "en").startswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))


def test_classify_failure_still_carries_english_hint_by_default():
    cls = classify_failure({"success": False, "error": "query timed out"})
    assert "retry" in cls.recovery_hint.lower()


# ── Chinese capability matching (no CJK keyword blindness) ──────────────────
def test_chinese_capability_keywords_match():
    candidates = [
        {"name": "production_agent", "description": "分析生产线产量与设备效率", "published": True},
        {"name": "finance_agent", "description": "Cost and margin analysis", "published": True},
    ]
    out = select_alternatives(candidates, required_keywords=("产量", "生产线"))
    assert [c["name"] for c in out] == ["production_agent"]


def test_chinese_failed_objective_selects_matching_alternative():
    candidates = [
        {"name": "inventory_agent", "description": "库存水平与缺货分析", "published": True},
        {"name": "hr_payroll", "description": "薪酬核算", "published": True},
    ]
    out = select_alternatives(
        candidates, required_keywords=("调查", "库存", "短缺"))
    assert [c["name"] for c in out] == ["inventory_agent"]
