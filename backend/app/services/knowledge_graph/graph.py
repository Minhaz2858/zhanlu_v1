"""Value-chain knowledge graph (data loaded from domain config).

Knowledge-graph construction and traversal.
Builds the product graph with PRODUCES/CONSUMES/SUBSTITUTE edges.
Products and relationships are loaded from the per-app domain config's
"knowledge_graph" block ({"products": [...], "relationships": [...]}); when
the config carries no such block the graph is empty (no products, no
relationships) — the platform stays fully generic and never crashes.
"""

from __future__ import annotations

from typing import Any

from app.services.domain_config import get_domain_config

from .models import (
    ProductNode,
    ProductType,
    Relationship,
    RelationshipType,
    KnowledgeGraph,
)

# ── Graph Data Source ─────────────────────────────────────────────────────
# All industry-specific product/relationship data lives in the per-app domain
# config (domain_configs/<agent>.json -> "knowledge_graph" block), never in
# platform code. No config → empty graph (fully generic platform).

_DOMAIN_CONFIG_NAME = ""


def _load_products(raw: Any) -> list[ProductNode]:
    """Build ProductNode instances from a config product list (fail-soft)."""
    nodes: list[ProductNode] = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        try:
            ptype = ProductType(str(item.get("type") or ""))
        except ValueError:
            ptype = ProductType.DOWNSTREAM
        nodes.append(
            ProductNode(
                id=str(item["id"]),
                name_en=str(item.get("name_en") or item["id"]),
                name_cn=str(item.get("name_cn") or ""),
                type=ptype,
                description=item.get("description"),
                typical_price_range=item.get("typical_price_range"),
                unit=str(item.get("unit") or "ton"),
            )
        )
    return nodes


def _load_relationships(raw: Any) -> list[Relationship]:
    """Build Relationship instances from a config relationship list (fail-soft)."""
    edges: list[Relationship] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        target_id = item.get("target_id")
        if not source_id or not target_id:
            continue
        try:
            rtype = RelationshipType(str(item.get("relation") or ""))
        except ValueError:
            rtype = RelationshipType.PRODUCES
        yield_rate = item.get("yield_rate")
        edges.append(
            Relationship(
                source_id=str(source_id),
                target_id=str(target_id),
                relation=rtype,
                yield_rate=float(yield_rate) if yield_rate is not None else None,
                description=item.get("description"),
            )
        )
    return edges


def _load_graph_data() -> tuple[list[ProductNode], list[Relationship]]:
    """Load products + relationships from the domain config (never raises)."""
    try:
        kg = (get_domain_config(_DOMAIN_CONFIG_NAME) or {}).get("knowledge_graph") or {}
    except Exception:  # noqa: BLE001 — fail-soft: empty graph on any config issue
        kg = {}
    return _load_products(kg.get("products")), _load_relationships(kg.get("relationships"))


# ── Graph Definition (loaded from domain config) ─────────────────────────

_C5_C9_NODES, _C5_C9_EDGES = _load_graph_data()


def build_c5_c9_graph() -> KnowledgeGraph:
    """Return a fresh instance of the product value-chain graph.

    Data comes from the domain config's "knowledge_graph" block; with no
    block present the returned graph is empty (fully generic behavior).
    """
    return KnowledgeGraph(
        nodes=list(_C5_C9_NODES),
        edges=list(_C5_C9_EDGES),
    )


# Lazy singleton — built on first call
_domain_graph: KnowledgeGraph | None = None


def get_domain_graph() -> KnowledgeGraph:
    """Return the shared (lazy-singleton) domain graph."""
    global _domain_graph
    if _domain_graph is None:
        _domain_graph = build_c5_c9_graph()
    return _domain_graph


# ── Query helpers ─────────────────────────────────────────────────────────


def query_upstream(product_id: str) -> dict:
    """Return upstream products and their relationships for a given product.

    Returns: {"product_id": str, "upstream": [{"id", "name_en", "name_cn", "relation", "yield_rate", "description"}, ...]}
    """
    g = get_domain_graph()
    node = g.get_node(product_id)
    if not node:
        return {"product_id": product_id, "upstream": [], "error": f"Unknown product: {product_id}"}

    sources = []
    for e in g.edges:
        if e.target_id != product_id:
            continue
        src = g.get_node(e.source_id)
        if src:
            sources.append({
                "id": src.id,
                "name_en": src.name_en,
                "name_cn": src.name_cn,
                "type": src.type.value,
                "relation": e.relation.value,
                "yield_rate": e.yield_rate,
                "description": e.description,
                "unit": src.unit,
            })

    return {
        "product_id": product_id,
        "product_name_en": node.name_en,
        "product_name_cn": node.name_cn,
        "product_type": node.type.value,
        "upstream": sources,
    }


def query_downstream(product_id: str) -> dict:
    """Return downstream products and their relationships for a given product.

    Returns: {"product_id": str, "downstream": [{"id", "name_en", "name_cn", "relation", "yield_rate", "description"}, ...]}
    """
    g = get_domain_graph()
    node = g.get_node(product_id)
    if not node:
        return {"product_id": product_id, "downstream": [], "error": f"Unknown product: {product_id}"}

    targets = []
    for e in g.edges:
        if e.source_id != product_id:
            continue
        tgt = g.get_node(e.target_id)
        if tgt:
            targets.append({
                "id": tgt.id,
                "name_en": tgt.name_en,
                "name_cn": tgt.name_cn,
                "type": tgt.type.value,
                "relation": e.relation.value,
                "yield_rate": e.yield_rate,
                "description": e.description,
                "unit": tgt.unit,
            })

    return {
        "product_id": product_id,
        "product_name_en": node.name_en,
        "product_name_cn": node.name_cn,
        "product_type": node.type.value,
        "downstream": targets,
    }


def query_chain(product_id: str, direction: str = "downstream") -> dict:
    """Recursively traverse the chain (upstream or downstream) and return summary.

    direction: "upstream" or "downstream"
    """
    g = get_domain_graph()
    node = g.get_node(product_id)
    if not node:
        return {"error": f"Unknown product: {product_id}"}

    visited: set[str] = set()
    chains: list[list[str]] = []

    def _walk(current_id: str, path: list[str]):
        if current_id in visited:
            return
        visited.add(current_id)
        path = path + [current_id]

        if direction == "downstream":
            candidates = g.downstream_of(current_id)
        else:
            candidates = g.upstream_of(current_id)

        if not candidates:
            chains.append(path)
            return

        for c in candidates:
            _walk(c.id, path)

    _walk(product_id, [])

    # Build summary
    summary = []
    for chain in chains:
        names = []
        for pid in chain:
            n = g.get_node(pid)
            names.append(f"{n.name_en}({n.name_cn})" if n else pid)
        summary.append(" → ".join(names))

    substitutes = []
    for s in g.substitute_of(product_id):
        substitutes.append({
            "id": s.id,
            "name_en": s.name_en,
            "name_cn": s.name_cn,
        })

    return {
        "product_id": product_id,
        "product_name_en": node.name_en,
        "product_name_cn": node.name_cn,
        f"{direction}_chains": summary,
        "substitutes": substitutes,
        "total_chains": len(chains),
    }


def format_graph_summary() -> str:
    """Return a human-readable summary of the entire product graph."""
    g = get_domain_graph()
    lines = [f"Knowledge Graph — {len(g.nodes)} products, {len(g.edges)} edges\n"]
    lines.append("=" * 60)

    for node in g.nodes:
        up = g.upstream_of(node.id)
        down = g.downstream_of(node.id)
        sub = g.substitute_of(node.id)
        lines.append(f"\n{node.name_en} ({node.name_cn}) [{node.type.value}]")
        if node.description:
            lines.append(f"  {node.description}")
        if up:
            up_names = ", ".join(f"{n.name_en}({n.name_cn})" for n in up)
            lines.append(f"  ↑ upstream: {up_names}")
        if down:
            down_names = ", ".join(f"{n.name_en}({n.name_cn})" for n in down)
            lines.append(f"  ↓ downstream: {down_names}")
        if sub:
            sub_names = ", ".join(f"{n.name_en}({n.name_cn})" for n in sub)
            lines.append(f"  ↔ substitutes: {sub_names}")

    return "\n".join(lines)
