"""Ontology core — the fourfold business layer (P0-2, Enterprise AI OS).

Public surface: queries (seeding/lists), data (grounded reads +
risk-tiered actions), coverage (honest per-domain readiness), serialize.
"""

from app.services.ontology.data import execute_action, read_object_data
from app.services.ontology.queries import (
    ensure_domains,
    get_action,
    get_domain,
    get_object,
    get_object_by_name,
    list_actions,
    list_bindings,
    list_domains,
    list_links,
    list_objects,
)

__all__ = [
    "ensure_domains",
    "get_action",
    "get_domain",
    "get_object",
    "get_object_by_name",
    "list_actions",
    "list_bindings",
    "list_domains",
    "list_links",
    "list_objects",
    "read_object_data",
    "execute_action",
]
