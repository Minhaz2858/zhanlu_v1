"""Semantic collection registry for the hybrid RAG layer.

Defines the 9 ChromaDB collections used by zhanlu's RAG pipeline, mirroring
The legacy decomposition. Each collection represents a distinct semantic
domain, with a stable English name, a Chinese display label, and a domain
tag for grouping.

Collections:
  1. industry_reports      — Long-form external research / industry reports
  2. weekly_reports        — Internal weekly market/operations reports
  3. past_decisions        — Historical decision summaries (human + agent)
  4. market_signals        — Real-time market signals (price, demand, supply)
  5. causal_graph_embeddings — Causal-chain embeddings (Phase 1 link)
  6. news_events           — News events (Phase 1 link)
  7. decision_outcomes     — Post-hoc decision outcomes (P&L, accuracy)
  8. product_catalog       — Product semantic aliases (C5/C9 derivatives)
  9. user_memory           — Per-user persistent memory snapshots

Naming convention:
    Full ChromaDB collection name: "domain_{org_id}_{collection_name}"
    The domain_ prefix prevents collisions with the legacy "kb_{org_id}"
    generic-document collection used by zhanlu's document_ingestion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# ---------------------------------------------------------------------------
# Collection name constants — exported for use throughout the RAG layer
# ---------------------------------------------------------------------------

INDUSTRY_REPORTS: str = "industry_reports"
WEEKLY_REPORTS: str = "weekly_reports"
PAST_DECISIONS: str = "past_decisions"
MARKET_SIGNALS: str = "market_signals"
CAUSAL_GRAPH_EMBEDDINGS: str = "causal_graph_embeddings"
NEWS_EVENTS: str = "news_events"
DECISION_OUTCOMES: str = "decision_outcomes"
PRODUCT_CATALOG: str = "product_catalog"
USER_MEMORY: str = "user_memory"

ALL_COLLECTION_NAMES: List[str] = [
    INDUSTRY_REPORTS,
    WEEKLY_REPORTS,
    PAST_DECISIONS,
    MARKET_SIGNALS,
    CAUSAL_GRAPH_EMBEDDINGS,
    NEWS_EVENTS,
    DECISION_OUTCOMES,
    PRODUCT_CATALOG,
    USER_MEMORY,
]

# ---------------------------------------------------------------------------
# Spec dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionSpec:
    """Static metadata about a semantic collection."""

    name: str  # English machine name
    chinese_label: str  # Display label for UI / logs
    domain: str  # Grouping tag for reporting / permissions


# ---------------------------------------------------------------------------
# Spec registry — order is intentional (most-frequently-queried first)
# ---------------------------------------------------------------------------


_COLLECTION_SPECS: List[CollectionSpec] = [
    CollectionSpec(
        name=INDUSTRY_REPORTS,
        chinese_label="行业研报",
        domain="external_research",
    ),
    CollectionSpec(
        name=WEEKLY_REPORTS,
        chinese_label="周报",
        domain="internal_reports",
    ),
    CollectionSpec(
        name=PAST_DECISIONS,
        chinese_label="历史决策",
        domain="decision_memory",
    ),
    CollectionSpec(
        name=MARKET_SIGNALS,
        chinese_label="市场信号",
        domain="market_intelligence",
    ),
    CollectionSpec(
        name=CAUSAL_GRAPH_EMBEDDINGS,
        chinese_label="因果图谱",
        domain="decision_support",
    ),
    CollectionSpec(
        name=NEWS_EVENTS,
        chinese_label="新闻事件",
        domain="market_intelligence",
    ),
    CollectionSpec(
        name=DECISION_OUTCOMES,
        chinese_label="决策结果",
        domain="decision_memory",
    ),
    CollectionSpec(
        name=PRODUCT_CATALOG,
        chinese_label="产品目录",
        domain="domain_knowledge",
    ),
    CollectionSpec(
        name=USER_MEMORY,
        chinese_label="用户记忆",
        domain="user_profile",
    ),
]

#: Public read-only list of all CollectionSpec entries.
COLLECTION_SPECS: List[CollectionSpec] = list(_COLLECTION_SPECS)

#: Build a name → spec lookup once at module load.
_SPEC_INDEX: dict[str, CollectionSpec] = {s.name: s for s in COLLECTION_SPECS}


def get_collection_spec(name: str) -> Optional[CollectionSpec]:
    """Look up the spec for a given collection name.

    Args:
        name: one of the 9 ALL_COLLECTION_NAMES values.

    Returns:
        The matching CollectionSpec, or None if name is unknown.
    """
    return _SPEC_INDEX.get(name)


# ---------------------------------------------------------------------------
# Collection name construction
# ---------------------------------------------------------------------------

_SAFE_ORG_CHARS = re.compile(r"[^A-Za-z0-9_\-]")


def _sanitize_org_id(org_id: str) -> str:
    """Sanitize an org_id for use in a ChromaDB collection name.

    ChromaDB collection names must be 3-63 chars and contain only
    [a-zA-Z0-9_-]. We replace anything else with '_'.

    Args:
        org_id: raw organization identifier (may contain any chars).

    Returns:
        Sanitized org_id safe for ChromaDB naming.
    """
    if not org_id:
        return "default"
    safe = _SAFE_ORG_CHARS.sub("_", org_id)
    # Collapse runs of underscores
    safe = re.sub(r"_+", "_", safe).strip("_")
    if not safe:
        return "default"
    return safe[:48]  # leave room for the collection name + prefix


def build_domain_collection_name(org_id: str, collection_name: str) -> str:
    """Construct a tenant-scoped ChromaDB collection name.

    Pattern: ``domain_{sanitized_org_id}_{collection_name}``

    Args:
        org_id: organization / tenant identifier.
        collection_name: one of ALL_COLLECTION_NAMES.

    Returns:
        ChromaDB-safe collection name (lowercase, alphanumeric+underscore+dash).
    """
    safe_org = _sanitize_org_id(org_id)
    return f"domain_{safe_org}_{collection_name}"
