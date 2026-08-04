"""Static C5/C9 petrochemical value-chain knowledge graph.

Port of EDIA 5.1.2 backend/src/domain/knowledge_graph.py.
Builds the 12-product graph with PRODUCES/CONSUMES/SUBSTITUTE edges.
All 12 products are included by default (no per-user scoping).
"""

from __future__ import annotations

from .models import (
    ProductNode,
    ProductType,
    Relationship,
    RelationshipType,
    KnowledgeGraph,
)

# ── C5/C9 Graph Definition ────────────────────────────────────────────────

_C5_C9_NODES: list[ProductNode] = [
    # Raw materials
    ProductNode(
        id="crude_oil", name_en="Crude Oil", name_cn="原油",
        type=ProductType.CRUDE,
        description="Global crude oil (Brent/Dubai) — feedstock for naphtha cracking.",
        typical_price_range="70-120 USD/bbl", unit="barrel",
    ),
    ProductNode(
        id="naphtha", name_en="Naphtha", name_cn="石脑油",
        type=ProductType.FEEDSTOCK,
        description="Light naphtha from crude distillation — cracker feedstock.",
        typical_price_range="600-1200 USD/ton", unit="ton",
    ),

    # C5 chain ─────────────────────────────────────────────
    ProductNode(
        id="cracked_c5", name_en="Cracked C5", name_cn="裂解C5",
        type=ProductType.C5_MIX,
        description="C5 fraction from steam cracking — separation into isoprene/piperylene/DCPD.",
        typical_price_range="700-1500 USD/ton", unit="ton",
    ),
    ProductNode(
        id="mixed_c5", name_en="Mixed C5", name_cn="混合碳五",
        type=ProductType.C5_MIX,
        description="Blended C5 stream including C5 raffinate.",
        typical_price_range="500-1000 USD/ton", unit="ton",
    ),
    ProductNode(
        id="isoprene", name_en="Isoprene", name_cn="异戊二烯",
        type=ProductType.C5_DERIVATIVE,
        description="Isoprene — monomer for polyisoprene rubber (IR) and SIS.",
        typical_price_range="1500-3000 USD/ton", unit="ton",
    ),
    ProductNode(
        id="piperylene", name_en="Piperylene", name_cn="间戊二烯",
        type=ProductType.C5_DERIVATIVE,
        description="Piperylene — co-monomer for C5 petroleum resin.",
        typical_price_range="800-1800 USD/ton", unit="ton",
    ),
    ProductNode(
        id="dcpd", name_en="DCPD", name_cn="双环戊二烯",
        type=ProductType.C5_DERIVATIVE,
        description="DCPD — monomer for high-grade hydrocarbon resin and ENB.",
        typical_price_range="1000-2500 USD/ton", unit="ton",
    ),
    ProductNode(
        id="sis", name_en="SIS", name_cn="苯乙烯-异戊二烯-苯乙烯",
        type=ProductType.C5_DERIVATIVE,
        description="Styrene-Isoprene-Styrene block copolymer — thermoplastic elastomer.",
        typical_price_range="2000-4000 USD/ton", unit="ton",
    ),

    # C9 chain ─────────────────────────────────────────────
    ProductNode(
        id="cracked_c9", name_en="Cracked C9", name_cn="裂解C9",
        type=ProductType.C9_DERIVATIVE,
        description="C9 fraction from steam cracking — resin feedstock.",
        typical_price_range="500-1200 USD/ton", unit="ton",
    ),
    ProductNode(
        id="styrene", name_en="Styrene", name_cn="苯乙烯",
        type=ProductType.DOWNSTREAM,
        description="Styrene monomer — for C5/C9 resin and downstream SIS.",
        typical_price_range="800-1500 USD/ton", unit="ton",
    ),

    # Downstream / other ───────────────────────────────────
    ProductNode(
        id="c5_resin", name_en="C5 Petroleum Resin", name_cn="碳五石油树脂",
        type=ProductType.DOWNSTREAM,
        description="C5 petroleum resin — tackifier for adhesives, rubber, paint.",
        typical_price_range="800-2000 USD/ton", unit="ton",
    ),
    ProductNode(
        id="raffinate_c5", name_en="Raffinate C5", name_cn="抽余碳五",
        type=ProductType.C5_DERIVATIVE,
        description="C5 raffinate after extraction — remaining C5 stream.",
        typical_price_range="400-800 USD/ton", unit="ton",
    ),
    ProductNode(
        id="blowing_agent", name_en="Pentane Blowing Agent", name_cn="戊烷发泡剂",
        type=ProductType.DOWNSTREAM,
        description="Pentane-based blowing agent for foam products.",
        typical_price_range="500-1000 USD/ton", unit="ton",
    ),
]

_C5_C9_EDGES: list[Relationship] = [
    # Crude → Naphtha
    Relationship("crude_oil", "naphtha", RelationshipType.PRODUCES,
                 yield_rate=0.15,
                 description="Crude distillation → light naphtha fraction"),

    # Naphtha → Cracked C5 / Cracked C9
    Relationship("naphtha", "cracked_c5", RelationshipType.PRODUCES,
                 yield_rate=0.12,
                 description="Naphtha steam cracking → C5 fraction"),
    Relationship("naphtha", "cracked_c9", RelationshipType.PRODUCES,
                 yield_rate=0.08,
                 description="Naphtha steam cracking → C9 fraction"),

    # Cracked C5 → derivatives
    Relationship("cracked_c5", "isoprene", RelationshipType.PRODUCES,
                 yield_rate=0.20,
                 description="C5 separation → isoprene"),
    Relationship("cracked_c5", "piperylene", RelationshipType.PRODUCES,
                 yield_rate=0.18,
                 description="C5 separation → piperylene"),
    Relationship("cracked_c5", "dcpd", RelationshipType.PRODUCES,
                 yield_rate=0.22,
                 description="C5 separation → DCPD"),
    Relationship("cracked_c5", "mixed_c5", RelationshipType.PRODUCES,
                 yield_rate=None,
                 description="C5 blending → mixed C5"),
    Relationship("cracked_c5", "raffinate_c5", RelationshipType.PRODUCES,
                 yield_rate=0.25,
                 description="Post-extraction C5 raffinate"),

    # Cracked C9 → downstream
    Relationship("cracked_c9", "c5_resin", RelationshipType.CONSUMES,
                 yield_rate=None,
                 description="C9 feeds into C5 resin as co-monomer"),

    # Mixed C5 → downstream
    Relationship("mixed_c5", "c5_resin", RelationshipType.CONSUMES,
                 yield_rate=None,
                 description="Mixed C5 → C5 petroleum resin"),
    Relationship("mixed_c5", "blowing_agent", RelationshipType.CONSUMES,
                 yield_rate=None,
                 description="Mixed C5 → pentane blowing agent"),

    # Isoprene → SIS
    Relationship("isoprene", "sis", RelationshipType.CONSUMES,
                 yield_rate=0.70,
                 description="Isoprene + styrene → SIS block copolymer"),

    # Styrene → SIS / C5 Resin
    Relationship("styrene", "sis", RelationshipType.CONSUMES,
                 yield_rate=0.30,
                 description="Styrene co-monomer for SIS"),
    Relationship("styrene", "c5_resin", RelationshipType.CONSUMES,
                 yield_rate=None,
                 description="Styrene co-monomer for C5/C9 resin"),

    # Substitution edges
    Relationship("piperylene", "dcpd", RelationshipType.SUBSTITUTE,
                 yield_rate=None,
                 description="Piperylene and DCPD compete for C5 separation capacity"),
    Relationship("isoprene", "piperylene", RelationshipType.SUBSTITUTE,
                 yield_rate=None,
                 description="Isoprene and piperylene compete for cracker C5 allocation"),
    Relationship("c5_resin", "sis", RelationshipType.SUBSTITUTE,
                 yield_rate=None,
                 description="C5 resin vs SIS — competing downstream applications"),
    Relationship("naphtha", "crude_oil", RelationshipType.CONSUMES,
                 yield_rate=None,
                 description="Naphtha is refined from crude oil"),
]


def build_c5_c9_graph() -> KnowledgeGraph:
    """Return a fresh instance of the static C5/C9 product graph."""
    return KnowledgeGraph(
        nodes=list(_C5_C9_NODES),
        edges=list(_C5_C9_EDGES),
    )


# Lazy singleton — built on first call
_domain_graph: KnowledgeGraph | None = None


def get_domain_graph() -> KnowledgeGraph:
    """Return the shared (lazy-singleton) C5/C9 domain graph."""
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
    """Return a human-readable summary of the entire C5/C9 graph."""
    g = get_domain_graph()
    lines = [f"C5/C9 Knowledge Graph — {len(g.nodes)} products, {len(g.edges)} edges\n"]
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
