"""Enterprise Knowledge Graph — product value-chain graph and product resolver.

Ported from a reference implementation (knowledge_graph.py, product_resolver.py,
edia_ontology.py).  Adapted from module-level singletons and SQLite to
class-based / function-based patterns with no external DB dependency.

Provides:
- build_c5_c9_graph()    — product graph (nodes + edges) from domain config
- resolve_product_id()   — map user language to canonical product_id
- extract_product_ids_in_text() — NLU multi-product extraction
- query_upstream()       — find upstream products of a given product
- query_downstream()     — find downstream products of a given product
- list_supported_product_ids()  — all known product IDs

The product catalog is defined by the app's domain configuration; with no
config the graph and resolver behave generically (empty catalog).
"""

from __future__ import annotations

from .models import ProductNode, ProductType, Relationship, RelationshipType, KnowledgeGraph
from .graph import (
    build_c5_c9_graph,
    get_domain_graph,
    query_upstream,
    query_downstream,
    query_chain,
    format_graph_summary,
)
from .resolver import (
    resolve_product_id,
    extract_product_ids_in_text,
    split_product_tokens,
    product_id_to_context_label,
    list_supported_product_ids,
    get_alias_mapping,
    looks_like_sinopec_listing_query,
    default_sinopec_listing_product_ids,
)

__all__ = [
    "ProductNode",
    "ProductType",
    "Relationship",
    "RelationshipType",
    "KnowledgeGraph",
    "build_c5_c9_graph",
    "get_domain_graph",
    "query_upstream",
    "query_downstream",
    "query_chain",
    "format_graph_summary",
    "resolve_product_id",
    "extract_product_ids_in_text",
    "split_product_tokens",
    "product_id_to_context_label",
    "list_supported_product_ids",
    "get_alias_mapping",
    "looks_like_sinopec_listing_query",
    "default_sinopec_listing_product_ids",
]
