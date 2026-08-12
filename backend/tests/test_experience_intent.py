"""Tests for the rule-based intent classifier (experience layer Phase A).

Covers all seven intent classes across Chinese and English questions.
Pure function tests — no DB required.
"""

import sys
import os

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.experience_intent import (
    classify_question,
    INTENT_WEEKLY_REPORT,
    INTENT_PRICE_REPORT,
    INTENT_MARKET_ANALYSIS,
    INTENT_FORECAST,
    INTENT_COMPARISON,
    INTENT_CONVERSATIONAL,
    INTENT_GENERAL,
    INTENTS,
)


class TestClassifyBasics:
    def test_empty_returns_general(self):
        assert classify_question("") == INTENT_GENERAL
        assert classify_question("   ") == INTENT_GENERAL
        assert classify_question(None) == INTENT_GENERAL

    def test_gibberish_returns_general(self):
        assert classify_question("asdkjha sdjh aksdjh") == INTENT_GENERAL


class TestClassifyPrice:
    def test_chinese_price_question(self):
        assert classify_question("苯乙烯今天多少钱一吨") == INTENT_PRICE_REPORT

    def test_chinese_price_query(self):
        assert classify_question("查一下最新的价格") == INTENT_PRICE_REPORT

    def test_english_price(self):
        assert classify_question("what is the current price of styrene") == INTENT_PRICE_REPORT

    def test_price_with_punctuation(self):
        assert classify_question("苯乙烯报价？") == INTENT_PRICE_REPORT


class TestClassifyMarketAnalysis:
    def test_chinese_market_analysis(self):
        assert classify_question("分析一下近期市场走势") == INTENT_MARKET_ANALYSIS

    def test_supply_demand(self):
        assert classify_question("市场供需情况怎么样") == INTENT_MARKET_ANALYSIS

    def test_english_market(self):
        assert classify_question("analyze the market trend this week") == INTENT_MARKET_ANALYSIS


class TestClassifyWeeklyReport:
    def test_chinese_weekly_report(self):
        assert classify_question("生成本周C5/C9周报") == INTENT_WEEKLY_REPORT

    def test_chinese_weekly_report_short(self):
        assert classify_question("生成周报") == INTENT_WEEKLY_REPORT

    def test_chinese_weekly_report_write(self):
        assert classify_question("写一份周度报告") == INTENT_WEEKLY_REPORT

    def test_chinese_weekly_report_this_week(self):
        assert classify_question("制作本周报告") == INTENT_WEEKLY_REPORT

    def test_english_weekly_report(self):
        assert classify_question("generate the weekly report for C5 products") == INTENT_WEEKLY_REPORT

    def test_english_draft_weekly(self):
        assert classify_question("produce weekly report for all products") == INTENT_WEEKLY_REPORT

    def test_weekly_beats_market_on_mixed(self):
        # "生成周报 with market data" — weekly_report keyword score may be lower
        # but if both match, priority tie-break favors weekly_report
        assert classify_question("生成带有市场数据的周报") == INTENT_WEEKLY_REPORT


class TestClassifyForecast:
    def test_chinese_forecast(self):
        assert classify_question("预测下周苯乙烯价格走势") == INTENT_FORECAST

    def test_chinese_will_rise(self):
        assert classify_question("下周价格会涨吗") == INTENT_FORECAST

    def test_english_forecast(self):
        assert classify_question("will styrene price rise next week") == INTENT_FORECAST

    def test_english_predict(self):
        assert classify_question("predict the price for next month") == INTENT_FORECAST


class TestClassifyComparison:
    def test_chinese_comparison(self):
        assert classify_question("对比一下苯乙烯和丁二烯的价格") == INTENT_COMPARISON

    def test_chinese_which(self):
        assert classify_question("哪个产品更适合做多") == INTENT_COMPARISON

    def test_english_comparison(self):
        assert classify_question("compare styrene and butadiene prices") == INTENT_COMPARISON


class TestClassifyConversational:
    def test_chinese_greeting(self):
        assert classify_question("你好") == INTENT_CONVERSATIONAL

    def test_chinese_thanks(self):
        assert classify_question("谢谢你的帮助") == INTENT_CONVERSATIONAL

    def test_english_greeting(self):
        assert classify_question("hello") == INTENT_CONVERSATIONAL

    def test_english_thanks(self):
        assert classify_question("thanks a lot") == INTENT_CONVERSATIONAL


class TestPriority:
    def test_weekly_report_beats_comparison(self):
        # "draft weekly report with 对比" → weekly_report outranks comparison
        assert classify_question("写一份包含对比分析的周报") == INTENT_WEEKLY_REPORT

    def test_weekly_report_beats_forecast(self):
        # "weekly report with forecast" → weekly_report beats forecast
        assert classify_question("生成包含预测的周报") == INTENT_WEEKLY_REPORT

    def test_forecast_beats_price(self):
        # "预测价格" contains both forecast + price keywords -> forecast wins
        assert classify_question("预测一下价格走势") == INTENT_FORECAST

    def test_comparison_beats_price(self):
        # "对比价格" contains both comparison + price -> comparison wins
        assert classify_question("对比一下两家报价") == INTENT_COMPARISON

    def test_price_with_greeting_prefix(self):
        # "你好，请问价格" — greeting + price -> price wins (domain intent wins)
        assert classify_question("你好，请问苯乙烯价格是多少") == INTENT_PRICE_REPORT

    def test_all_intents_exported(self):
        assert len(INTENTS) == 7
        for intent in (
            INTENT_WEEKLY_REPORT,
            INTENT_PRICE_REPORT,
            INTENT_MARKET_ANALYSIS,
            INTENT_FORECAST,
            INTENT_COMPARISON,
            INTENT_CONVERSATIONAL,
            INTENT_GENERAL,
        ):
            assert intent in INTENTS
