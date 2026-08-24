"""Tests for dashboard guard's inline-analytics escape hatch."""
import pytest

from app.services.dashboard_turn_guard import (
    _is_inline_analytics_intent,
    is_live_dashboard_request,
    should_force_create_dashboard,
)


class TestIsInlineAnalyticsIntent:
    """Verify inline-analytics detection for EN and ZH messages."""

    @pytest.mark.parametrize("msg", [
        "i want July 2026 sales report (volume, revenue, margin, inventory)",
        "give me supply chain data for last 30 days",
        "show me revenue by product",
        "compare June and July sales",
        "top 10 customers last quarter",
        "list the top 5 materials by revenue",
        "give me a breakdown of inventory levels",
        "show me the gross margin analysis",
        "drill down into customer 103350",
        "month over month comparison",
        "give me the YoY change",
        "revenue report by region",
        "inventory data overview",
        "show me supply-chain KPIs",
    ])
    def test_en_positive(self, msg):
        assert _is_inline_analytics_intent(msg) is True, f"Expected True for: {msg}"

    @pytest.mark.parametrize("msg", [
        "build a dashboard",
        "create a live dashboard",
        "open the sales dashboard",
        "I want to build a metrics board",
    ])
    def test_en_negative_dashboard(self, msg):
        assert _is_inline_analytics_intent(msg) is False, (
            f"Expected False for: {msg} (dashboard request)"
        )

    @pytest.mark.parametrize("msg", [
        "给我七月份销售报告",
        "本月库存情况如何",
        "营收排名前10的产品",
        "对比六月和七月的销售",
        "毛利率分析",
        "上个月供应链数据",
        "看看客户收入排名",
    ])
    def test_zh_positive(self, msg):
        assert _is_inline_analytics_intent(msg) is True, f"Expected True for: {msg}"

    def test_empty(self):
        assert _is_inline_analytics_intent("") is False

    def test_none(self):
        assert _is_inline_analytics_intent(None) is False


class TestShouldForceCreateDashboardInlineEscape:
    """Verify should_force_create_dashboard respects inline-analytics intent."""

    def _tool_calls_with_schema_design(self):
        return [
            {"name": "describe_schema"},
            {"name": "uiux_design_system"},
        ]

    def test_inline_analytics_blocks_speculative_forcing(self):
        result = should_force_create_dashboard(
            "i want July 2026 sales report (volume, revenue, margin, inventory)",
            self._tool_calls_with_schema_design(),
            has_dashboard_tool=True,
            is_dashboard_project=True,
        )
        assert result is False, (
            "Path 2 speculative forcing fired on inline-analytics query"
        )

    def test_give_me_supply_chain_blocks_speculative_forcing(self):
        result = should_force_create_dashboard(
            "give me supply chain data for last 30 days",
            self._tool_calls_with_schema_design(),
            has_dashboard_tool=True,
            is_dashboard_project=True,
        )
        assert result is False

    def test_dashboard_request_still_forces(self):
        result = should_force_create_dashboard(
            "build a dashboard",
            [
                {"name": "list_data_sources"},
                {"name": "describe_schema"},
                {"name": "uiux_design_system"},
            ],
            has_dashboard_tool=True,
            is_dashboard_project=True,
        )
        assert result is True, "Path 1 must still fire for explicit dashboard requests"

    def test_no_schema_design_no_forcing(self):
        result = should_force_create_dashboard(
            "i want July 2026 sales report",
            [{"name": "ask_data_agent"}],
            has_dashboard_tool=True,
            is_dashboard_project=True,
        )
        assert result is False

    def test_no_dashboard_tool_no_forcing(self):
        result = should_force_create_dashboard(
            "build a dashboard",
            self._tool_calls_with_schema_design(),
            has_dashboard_tool=False,
            is_dashboard_project=True,
        )
        assert result is False

    def test_chinese_inline_analytics_blocks_speculative(self):
        result = should_force_create_dashboard(
            "给我七月份销售报告",
            self._tool_calls_with_schema_design(),
            has_dashboard_tool=True,
            is_dashboard_project=True,
        )
        assert result is False


class TestIsLiveDashboardRequestStillExplicit:
    @pytest.mark.parametrize("msg", [
        "build a dashboard",
        "open the sales dashboard",
        "create a metrics board",
        "做仪表盘",
        "数据看板",
    ])
    def test_explicit_dashboard_detected(self, msg):
        assert is_live_dashboard_request(msg) is True

    @pytest.mark.parametrize("msg", [
        "i want July 2026 sales report",
        "give me supply chain data",
        "给我七月份销售报告",
        "本月库存",
    ])
    def test_inline_analytics_not_detected_as_dashboard(self, msg):
        assert is_live_dashboard_request(msg) is False