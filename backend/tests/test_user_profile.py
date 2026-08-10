"""Tests for the user profile store (experience layer Phase A).

Per (agent_app_id, user_id) JSON profile: preferred language, frequently
asked products, preferred depth/format. Learned implicitly from user content
and explicitly from feedback. Injected into the system prompt.
"""

import sys
import os
import json

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.user_profile import (
    UserProfile,
    update_user_profile,
    get_user_profile,
    get_profile_prompt,
    add_feedback,
)


@pytest.fixture
def tmp_store(tmp_path):
    return str(tmp_path)


class TestUserProfileDefaults:
    def test_missing_profile_returns_empty(self, tmp_store):
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.agent_app_id == "agent-1"
        assert p.user_id == "user-1"
        assert p.language == ""
        assert p.top_products == []
        assert p.depth_pref == ""

    def test_profile_roundtrip(self, tmp_store):
        update_user_profile(
            "agent-1", "user-1", "苯乙烯的价格是多少", storage_dir=tmp_store,
        )
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.agent_app_id == "agent-1"
        assert p.user_id == "user-1"


class TestLanguageDetection:
    def test_chinese_content(self, tmp_store):
        update_user_profile("agent-1", "user-1", "苯乙烯今天多少钱", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.language == "zh"

    def test_english_content(self, tmp_store):
        update_user_profile("agent-1", "user-1", "what is the price of styrene", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.language == "en"

    def test_mixed_content_prefers_chinese(self, tmp_store):
        update_user_profile("agent-1", "user-1", "苯乙烯 price 多少", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.language == "zh"


class TestTopProducts:
    def test_chinese_product_detected(self, tmp_store):
        update_user_profile("agent-1", "user-1", "苯乙烯的行情怎么样", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert any(x.name == "苯乙烯" for x in p.top_products)

    def test_product_count_incremented(self, tmp_store):
        update_user_profile("agent-1", "user-1", "苯乙烯的价格", storage_dir=tmp_store)
        update_user_profile("agent-1", "user-1", "苯乙烯会涨吗", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        product = next((x for x in p.top_products if x.name == "苯乙烯"), None)
        assert product is not None
        assert product.count == 2

    def test_no_product_match(self, tmp_store):
        update_user_profile("agent-1", "user-1", "今天天气怎么样", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.top_products == []


class TestDepthPreference:
    def test_detailed_request(self, tmp_store):
        update_user_profile("agent-1", "user-1", "给我详细的分析报告", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.depth_pref == "detailed"

    def test_brief_request(self, tmp_store):
        update_user_profile("agent-1", "user-1", "简单说一下就行", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.depth_pref == "brief"

    def test_feedback_overrides(self, tmp_store):
        update_user_profile("agent-1", "user-1", "简单说一下", storage_dir=tmp_store)
        add_feedback("agent-1", "user-1", rating=1, detail_pref="detailed", storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.depth_pref == "detailed"


class TestProfilePrompt:
    def test_empty_profile_no_prompt(self, tmp_store):
        assert get_profile_prompt("agent-1", "user-1", storage_dir=tmp_store) == ""

    def test_prompt_contains_profile(self, tmp_store):
        update_user_profile(
            "agent-1", "user-1", "给我详细的苯乙烯分析", storage_dir=tmp_store,
        )
        prompt = get_profile_prompt("agent-1", "user-1", storage_dir=tmp_store)
        assert "苯乙烯" in prompt
        assert "detailed" in prompt
        assert "Preferred language" in prompt
        assert "中文" in prompt

    def test_isolated_per_user(self, tmp_store):
        update_user_profile("agent-1", "user-1", "苯乙烯的价格", storage_dir=tmp_store)
        p2 = get_user_profile("agent-1", "user-2", storage_dir=tmp_store)
        assert p2.top_products == []


class TestFeedback:
    def test_positive_feedback_updates_profile(self, tmp_store):
        update_user_profile("agent-1", "user-1", "苯乙烯价格", storage_dir=tmp_store)
        add_feedback("agent-1", "user-1", rating=1, storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.thumbs_up == 1
        assert p.thumbs_down == 0

    def test_negative_feedback_updates_profile(self, tmp_store):
        update_user_profile("agent-1", "user-1", "苯乙烯价格", storage_dir=tmp_store)
        add_feedback("agent-1", "user-1", rating=-1, storage_dir=tmp_store)
        p = get_user_profile("agent-1", "user-1", storage_dir=tmp_store)
        assert p.thumbs_down == 1
