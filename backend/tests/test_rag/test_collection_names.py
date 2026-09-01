"""Tests for collection_names.py — 9 semantic ChromaDB collection names.

The collections form a structured semantic decomposition of all RAG-indexed
content. Each collection has a stable English name, a Chinese display label,
and a domain tag.
"""
from __future__ import annotations

from app.services.rag.collection_names import (
    CollectionSpec,
    COLLECTION_SPECS,
    get_collection_spec,
    build_domain_collection_name,
    ALL_COLLECTION_NAMES,
    INDUSTRY_REPORTS,
    WEEKLY_REPORTS,
    PAST_DECISIONS,
    MARKET_SIGNALS,
    CAUSAL_GRAPH_EMBEDDINGS,
    NEWS_EVENTS,
    DECISION_OUTCOMES,
    PRODUCT_CATALOG,
    USER_MEMORY,
)


class TestCollectionNames:
    def test_nine_collections_defined(self):
        """Exactly 9 semantic collections per EDIA design."""
        assert len(COLLECTION_SPECS) == 9

    def test_all_collection_names_match_specs(self):
        for spec in COLLECTION_SPECS:
            assert spec.name in ALL_COLLECTION_NAMES
        # And the named constants match ALL_COLLECTION_NAMES
        assert set(ALL_COLLECTION_NAMES) == {
            INDUSTRY_REPORTS,
            WEEKLY_REPORTS,
            PAST_DECISIONS,
            MARKET_SIGNALS,
            CAUSAL_GRAPH_EMBEDDINGS,
            NEWS_EVENTS,
            DECISION_OUTCOMES,
            PRODUCT_CATALOG,
            USER_MEMORY,
        }

    def test_individual_constants_are_strings(self):
        for name in ALL_COLLECTION_NAMES:
            assert isinstance(name, str)
            assert len(name) > 0
            # Should be lowercase with underscores (ChromaDB convention)
            assert name == name.lower()

    def test_get_collection_spec_returns_spec(self):
        spec = get_collection_spec(INDUSTRY_REPORTS)
        assert spec is not None
        assert spec.name == INDUSTRY_REPORTS
        assert isinstance(spec, CollectionSpec)

    def test_get_collection_spec_unknown_returns_none(self):
        assert get_collection_spec("nonexistent_collection") is None

    def test_each_spec_has_chinese_label(self):
        for spec in COLLECTION_SPECS:
            assert spec.chinese_label, f"{spec.name} missing chinese_label"
            assert isinstance(spec.chinese_label, str)

    def test_each_spec_has_domain(self):
        for spec in COLLECTION_SPECS:
            assert spec.domain, f"{spec.name} missing domain"
            assert isinstance(spec.domain, str)

    def test_no_duplicate_collection_names(self):
        names = [spec.name for spec in COLLECTION_SPECS]
        assert len(names) == len(set(names))


class TestBuildDomainCollectionName:
    def test_basic_name_construction(self):
        result = build_domain_collection_name("acme", INDUSTRY_REPORTS)
        assert "acme" in result
        assert INDUSTRY_REPORTS in result
        # Must be safe for ChromaDB (alphanumeric + underscore + dash only)
        assert all(c.isalnum() or c in "_-" for c in result)

    def test_org_id_with_special_chars_sanitized(self):
        # Sanitize special characters
        result = build_domain_collection_name("org-123_test", NEWS_EVENTS)
        assert result.startswith("domain_")
        # Should not contain raw special chars
        assert "/" not in result
        assert " " not in result

    def test_returns_consistent_results(self):
        a = build_domain_collection_name("org1", PAST_DECISIONS)
        b = build_domain_collection_name("org1", PAST_DECISIONS)
        assert a == b

    def test_different_orgs_get_different_collections(self):
        a = build_domain_collection_name("org1", USER_MEMORY)
        b = build_domain_collection_name("org2", USER_MEMORY)
        assert a != b

    def test_naming_pattern(self):
        result = build_domain_collection_name("acme", PRODUCT_CATALOG)
        # Should start with "domain_"
        assert result.startswith("domain_")
