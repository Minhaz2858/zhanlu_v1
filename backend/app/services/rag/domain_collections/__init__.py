"""Domain collection services — public API for the 5 domain services.

Re-exports everything from ``.services`` so callers can do::

    from app.services.rag.domain_collections import (
        DecisionService, SignalService, CausalService,
        NewsService, ProductService, build_domain_context,
    )
"""
from __future__ import annotations

from app.services.rag.domain_collections.services import (  # noqa: F401
    CausalService,
    DecisionService,
    NewsService,
    ProductService,
    SignalService,
    build_domain_context,
    get_service,
    list_all_services,
)

__all__ = [
    "CausalService",
    "DecisionService",
    "NewsService",
    "ProductService",
    "SignalService",
    "build_domain_context",
    "get_service",
    "list_all_services",
]