"""KG-driven exogenous feature spec derivation + topological ordering."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.knowledge_graph.models import (
    KnowledgeGraph, ProductType, ProductNode, RelationshipType,
)

logger = logging.getLogger(__name__)

_IMPORTED_TYPES = {ProductType.CRUDE, ProductType.FEEDSTOCK}
_DEFAULT_FEEDSTOCK_LAGS = [1, 2, 3, 7]


@dataclass
class FeatureSpec:
    product_key: str
    feedstock_keys: list[str] = field(default_factory=list)
    feedstock_lags: list[int] = field(default_factory=lambda: list(_DEFAULT_FEEDSTOCK_LAGS))
    spread_pairs: list[tuple[str, str]] = field(default_factory=list)
    use_fx: bool = False
    use_event_flags: bool = True
    calendar_features: bool = True


def _walk_upstream_chain(product_id: str, graph: KnowledgeGraph, visited: set[str] | None = None) -> list[ProductNode]:
    """Recursively collect upstream ProductNodes (transitive closure of upstream_of, PRODUCES only)."""
    if visited is None:
        visited = set()
    if product_id in visited:
        return []
    visited.add(product_id)
    # Only consider PRODUCES edges (CONSUMES edges can create reverse-direction cycles)
    direct = [e for e in graph.edges if e.target_id == product_id and e.relation == RelationshipType.PRODUCES]
    nodes_direct: list[ProductNode] = []
    for e in direct:
        n = graph.get_node(e.source_id)
        if n is not None and n not in nodes_direct:
            nodes_direct.append(n)
    chain: list[ProductNode] = []
    for node in nodes_direct:
        if node not in chain:
            chain.append(node)
        deeper = _walk_upstream_chain(node.id, graph, visited)
        for d in deeper:
            if d not in chain:
                chain.append(d)
    return chain


def derive_feature_spec(
    product_key: str,
    graph: KnowledgeGraph,
    feedstock_lags: list[int] | None = None,
    override_exog_features: list[str] | None = None,
) -> FeatureSpec:
    """Derive exogenous feature requirements for a product from the KG.

    Walks upstream_of() transitively. Returns feedstock chain, spread pairs between
    adjacent feedstocks, FX flag (True if chain touches CRUDE/FEEDSTOCK), and calendar
    request. For unknown products returns an empty spec.

    If *override_exog_features* is provided (typically from
    ``target.model_config["exog_features"]``), the KG walk is skipped and
    those features are used directly.  This allows domain experts to
    force-include specific exogenous features per product.
    """
    if override_exog_features:
        logger.info(
            "Using override exog_features for %s: %s (KG walk skipped)",
            product_key, override_exog_features,
        )
        return FeatureSpec(
            product_key=product_key,
            feedstock_keys=override_exog_features,
            feedstock_lags=feedstock_lags or list(_DEFAULT_FEEDSTOCK_LAGS),
            spread_pairs=[],
            use_fx=False,
            use_event_flags=True,
            calendar_features=True,
        )

    node = graph.get_node(product_key)
    if node is None:
        return FeatureSpec(
            product_key=product_key,
            feedstock_keys=[],
            feedstock_lags=feedstock_lags or list(_DEFAULT_FEEDSTOCK_LAGS),
        )
    chain = _walk_upstream_chain(product_key, graph)
    feedstock_keys = [n.id for n in chain]
    spread_pairs: list[tuple[str, str]] = []
    for i in range(len(feedstock_keys) - 1):
        spread_pairs.append((feedstock_keys[i], feedstock_keys[i + 1]))
    use_fx = any(
        graph.get_node(fk) and graph.get_node(fk).type in _IMPORTED_TYPES  # type: ignore[union-attr]
        for fk in feedstock_keys
    )
    return FeatureSpec(
        product_key=product_key,
        feedstock_keys=feedstock_keys,
        feedstock_lags=feedstock_lags or list(_DEFAULT_FEEDSTOCK_LAGS),
        spread_pairs=spread_pairs,
        use_fx=use_fx,
        use_event_flags=True,
        calendar_features=True,
    )


def topological_order(product_keys: list[str], graph: KnowledgeGraph) -> list[str]:
    """Kahn's algorithm on the feedstock dependency graph.

    Returns product_keys sorted so feedstocks appear before derivatives.
    Products not in the graph or with unresolved cycles are placed at the end.
    """
    product_set = set(product_keys)
    deps: dict[str, set[str]] = {pk: set() for pk in product_keys}
    for pk in product_keys:
        if graph.get_node(pk) is None:
            continue
        # Only PRODUCES edges for dependency direction (CONSUMES creates cycles)
        upstream_edges = [e for e in graph.edges if e.target_id == pk and e.relation == RelationshipType.PRODUCES]
        for e in upstream_edges:
            if e.source_id in product_set:
                deps[pk].add(e.source_id)
    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted([pk for pk, d in remaining.items() if not d])
        if not ready:
            logger.warning("Cycle detected — falling back to original order")
            ordered.extend(remaining.keys())
            break
        for pk in ready:
            ordered.append(pk)
            del remaining[pk]
            for other_deps in remaining.values():
                other_deps.discard(pk)
    return ordered
