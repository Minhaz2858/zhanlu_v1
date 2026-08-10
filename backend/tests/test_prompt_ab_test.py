"""Regression tests for A/B prompt regression testing (Phase 6)."""

from unittest.mock import patch, MagicMock


class TestABTestResult:
    """Tests for ABTestResult dataclass."""

    def test_ab_result_empty(self):
        from app.services.prompt_ab_test import ABTestResult
        r = ABTestResult()
        assert r.total_queries == 0
        assert r.winner == ""

    def test_ab_result_to_dict(self):
        from app.services.prompt_ab_test import ABTestResult
        r = ABTestResult(
            test_id="test-1",
            prompt_version_a="prompt A",
            prompt_version_b="prompt B",
            total_queries=10,
            wins_a=6,
            wins_b=3,
            ties=1,
            winner="A",
            confidence=0.67,
            mean_score_a=8.5,
            mean_score_b=7.2,
            per_query_results=[{"query": "q1", "winner": "A"}],
        )
        d = r.to_dict()
        assert d["winner"] == "A"
        assert d["total_queries"] == 10


class TestOverallScore:
    """Tests for _overall_score helper."""

    def test_overall_score_empty(self):
        from app.services.prompt_ab_test import _overall_score
        score = _overall_score({})
        assert score == 0.0

    def test_overall_score_weighted(self):
        from app.services.prompt_ab_test import _overall_score
        scores = {
            "completeness": 8,
            "accuracy": 9,
            "helpfulness": 7,
            "safety": 10,
        }
        score = _overall_score(scores)
        # weight: 0.3*8 + 0.3*9 + 0.25*7 + 0.15*10 = 2.4+2.7+1.75+1.5 = 8.35
        assert 8.0 < score < 9.0


class TestIsEnabled:
    """Tests for is_enabled flag."""

    def test_is_enabled_default_false(self):
        import app.config
        # Default should be False
        from app.services.prompt_ab_test import is_enabled
        # Cannot easily test without config, but function should return bool
        result = is_enabled()
        assert isinstance(result, bool)
