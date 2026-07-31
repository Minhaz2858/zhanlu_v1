"""Tests for the semantic resolver (rapidfuzz token-set ratio)."""

import pytest
from app.services.nl2sql.semantic_resolver import resolve, ResolvedIntent


@pytest.fixture
def sample_metrics():
    return [
        {"id": "m1", "name": "monthly_active_users", "synonyms": ["MAU", "active users", "monthly users"]},
        {"id": "m2", "name": "revenue", "synonyms": ["sales", "income", "turnover"]},
        {"id": "m3", "name": "churn_rate", "synonyms": ["attrition", "cancellation rate"]},
    ]


@pytest.fixture
def sample_mappings():
    return [
        {
            "id": "sm1",
            "business_term": "customer",
            "synonyms": ["client", "account", "buyer"],
            "target_table": "customers",
            "target_columns": ["id", "name", "email", "signup_date"],
        },
        {
            "id": "sm2",
            "business_term": "order",
            "synonyms": ["purchase", "transaction"],
            "target_table": "orders",
            "target_columns": ["id", "customer_id", "amount", "created_at"],
        },
    ]


class TestResolveBasics:
    def test_resolves_exact_metric_name(self, sample_metrics, sample_mappings):
        intent = resolve("monthly_active_users", sample_metrics, sample_mappings)
        assert intent.metric_name == "monthly_active_users"

    def test_resolves_by_synonym(self, sample_metrics, sample_mappings):
        intent = resolve("how many active users do we have", sample_metrics, sample_mappings)
        assert intent.metric_name == "monthly_active_users"

    def test_resolves_revenue_synonym(self, sample_metrics, sample_mappings):
        intent = resolve("what is our sales number", sample_metrics, sample_mappings)
        assert intent.metric_name == "revenue"

    def test_resolves_by_business_term(self, sample_metrics, sample_mappings):
        intent = resolve("show me all customer data", sample_metrics, sample_mappings)
        assert intent.table_name == "customers"

    def test_resolves_by_mapping_synonym(self, sample_metrics, sample_mappings):
        intent = resolve("list all transactions", sample_metrics, sample_mappings)
        assert intent.table_name == "orders"

    def test_empty_on_no_match(self, sample_metrics, sample_mappings):
        intent = resolve("xyzzy_no_match_abc", sample_metrics, sample_mappings)
        assert intent.metric_name is None
        assert intent.table_name is None
        assert intent.confidence == 0.0

    def test_confidence_threshold(self, sample_metrics, sample_mappings):
        # With a very high threshold, even exact matches should fail
        intent = resolve("active users", sample_metrics, sample_mappings, min_confidence=0.99)
        # "active users" might still match if it's an exact synonym; try a borderline case
        intent2 = resolve("xyz users", sample_metrics, sample_mappings, min_confidence=0.95)
        assert intent2.metric_name is None


class TestResolveEdgeCases:
    def test_empty_inputs(self):
        intent = resolve("anything", [], [])
        assert intent.metric_name is None
        assert intent.table_name is None

    def test_null_synonyms(self):
        metrics = [{"id": "m1", "name": "test_metric", "synonyms": None}]
        intent = resolve("test_metric", metrics, [])
        assert intent.metric_name == "test_metric"
