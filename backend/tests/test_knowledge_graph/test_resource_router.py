"""Resource Router — deterministic RouteDecision contract tests.

The router maps a user question + available project resources onto one of
five routes (database / document / memory / report / multi_resource) with
conservative fallback. Contract: never raises; rules are deterministic;
fallback means "behave exactly as today".
"""

from __future__ import annotations

import pytest

from app.services.knowledge_graph.resource_router import (
    ResourceRoute,
    RouteDecision,
    route_question,
)

ALL = {"database", "document", "memory", "report"}


# ── contract ──────────────────────────────────────────────────────────────

class TestContract:
    def test_decision_shape(self):
        d = route_question("查一下上个月的库存总量", available_resources=ALL)
        assert isinstance(d, RouteDecision)
        assert isinstance(d.route, ResourceRoute)
        assert isinstance(d.resource_ids, list)
        assert 0.0 <= d.confidence <= 1.0
        assert isinstance(d.used_fallback, bool)

    def test_never_raises_on_garbage(self):
        for q in ["", None, "！！！###", "a" * 5000, "\n\t\r"]:
            d = route_question(q or "", available_resources=ALL)
            assert isinstance(d, RouteDecision)

    def test_never_raises_without_resources(self):
        d = route_question("查询库存", available_resources=None)
        assert isinstance(d, RouteDecision)

    def test_empty_question_falls_back(self):
        d = route_question("", available_resources=ALL)
        assert d.used_fallback is True
        assert d.confidence == 0.0


# ── database route ────────────────────────────────────────────────────────

class TestDatabaseRoute:
    @pytest.mark.parametrize("q", [
        "上个月的总销量是多少？",
        "统计一下各地区的平均价格",
        "how many orders were placed last week?",
        "top 10 customers by revenue",
        "数据库里有多少张表？",
    ])
    def test_database_questions(self, q):
        d = route_question(q, available_resources=ALL)
        assert d.route == ResourceRoute.DATABASE
        assert d.used_fallback is False
        assert d.confidence == 1.0


# ── document route ────────────────────────────────────────────────────────

class TestDocumentRoute:
    @pytest.mark.parametrize("q", [
        "我上传的文档里讲了什么？",
        "根据那份PDF文件的内容总结一下",
        "what does the uploaded document say about safety?",
    ])
    def test_document_questions(self, q):
        d = route_question(q, available_resources=ALL)
        assert d.route == ResourceRoute.DOCUMENT
        assert d.used_fallback is False


# ── memory route ──────────────────────────────────────────────────────────

class TestMemoryRoute:
    @pytest.mark.parametrize("q", [
        "我们上次讨论的决定是什么？",
        "还记得之前定下的目标吗？",
        "what did we decide last time?",
    ])
    def test_memory_questions(self, q):
        d = route_question(q, available_resources=ALL)
        assert d.route == ResourceRoute.MEMORY
        assert d.used_fallback is False


# ── report route ──────────────────────────────────────────────────────────

class TestReportRoute:
    @pytest.mark.parametrize("q", [
        "生成本周的销售周报",
        "帮我出一份月度报告",
        "generate the weekly report",
    ])
    def test_report_questions(self, q):
        d = route_question(q, available_resources=ALL)
        assert d.route == ResourceRoute.REPORT
        assert d.used_fallback is False

    def test_report_route_collects_matched_recipes(self):
        d = route_question(
            "生成销售周报",
            available_resources=ALL,
            recipe_names=["sales_weekly", "inventory_monthly"],
        )
        assert d.route == ResourceRoute.REPORT
        assert "sales_weekly" in d.resource_ids

    def test_report_without_recipes_falls_back(self):
        d = route_question("生成本周的销售周报", available_resources={"database"})
        assert d.used_fallback is True


# ── multi-resource route ──────────────────────────────────────────────────

class TestMultiResourceRoute:
    @pytest.mark.parametrize("q", [
        "为什么上个月销量下降了？深入分析一下原因",
        "对比数据库里的实际销量和上次报告里的预测",
        "why did revenue drop? drill down into the root cause",
    ])
    def test_multi_resource_questions(self, q):
        d = route_question(q, available_resources=ALL)
        assert d.route == ResourceRoute.MULTI_RESOURCE
        assert d.used_fallback is False


# ── fallback semantics ────────────────────────────────────────────────────

class TestFallback:
    def test_ambiguous_question_falls_back(self):
        d = route_question("你好", available_resources=ALL)
        assert d.used_fallback is True
        assert d.confidence == 0.0

    def test_fallback_prefers_database_when_available(self):
        d = route_question("你好", available_resources=ALL)
        assert d.route == ResourceRoute.DATABASE

    def test_fallback_without_database_picks_document(self):
        d = route_question("你好", available_resources={"document", "memory"})
        assert d.used_fallback is True
        assert d.route == ResourceRoute.DOCUMENT

    def test_unavailable_route_degrades_to_fallback(self):
        # document question but no documents bound
        d = route_question("我上传的文档里讲了什么？", available_resources={"database"})
        assert d.used_fallback is True
        assert d.route == ResourceRoute.DATABASE

    def test_flags_off_consumer_sees_fallback_only(self):
        """With no resource info the router must be maximally conservative."""
        d = route_question("查一下库存", available_resources=set())
        assert d.used_fallback is True
