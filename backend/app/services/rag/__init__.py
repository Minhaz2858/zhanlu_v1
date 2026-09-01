"""Hybrid RAG service package.

Provides a unified retrieval-augmented generation (RAG) layer with:
- Hybrid dense+sparse retrieval with Reciprocal Rank Fusion (RRF)
- 9 semantic ChromaDB collections (industry_reports, weekly_reports,
  past_decisions, market_signals, causal_graph_embeddings, news_events,
  decision_outcomes, product_catalog, user_memory)
- Three-tier degradation chain (KnowledgeBase → LexicalKnowledgeBase → DisabledKnowledgeBase)
- Chinese-optimized chunking and embedding

Public API exports are added below as modules are implemented.
"""
from __future__ import annotations

__all__ = [
    "hybrid_retrieval",
    "knowledge_base",
    "rag_retriever",
    "user_memory_retriever",
    "industry_report_chunker",
    "industry_report_ingester",
    "collection_names",
    "domain_collections",
]
