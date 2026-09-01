"""Pure-Python data structures for the enterprise knowledge graph.

Domain models for the knowledge-graph catalog.
Replaces pydantic BaseModel with standard dataclasses for zero-dependency port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProductType(str, Enum):
    CRUDE = "Crude Oil"
    FEEDSTOCK = "Feedstock"        # Naphtha
    OLEFIN = "Olefin"              # Ethylene, Propylene
    C5_MIX = "Mixed C5"
    C5_DERIVATIVE = "C5 Derivative"
    C9_DERIVATIVE = "C9 Derivative"
    DOWNSTREAM = "Downstream Application"


@dataclass
class ProductNode:
    id: str
    name_en: str
    name_cn: str
    type: ProductType
    description: str | None = None
    # Economics
    typical_price_range: str | None = None   # "800-1200 USD"
    unit: str = "ton"


class RelationshipType(str, Enum):
    PRODUCES = "produces"       # Cracking / Separation
    CONSUMES = "consumes"       # Downstream usage
    SUBSTITUTE = "substitute"   # Competition


@dataclass
class Relationship:
    source_id: str
    target_id: str
    relation: RelationshipType
    yield_rate: float | None = None   # 1 ton Naphtha → 0.15 ton C5
    description: str | None = None


@dataclass
class KnowledgeGraph:
    nodes: list[ProductNode] = field(default_factory=list)
    edges: list[Relationship] = field(default_factory=list)

    def get_node(self, node_id: str) -> ProductNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def find_edges(
        self,
        source_id: str | None = None,
        target_id: str | None = None,
        relation: RelationshipType | None = None,
    ) -> list[Relationship]:
        result: list[Relationship] = []
        for e in self.edges:
            if source_id is not None and e.source_id != source_id:
                continue
            if target_id is not None and e.target_id != target_id:
                continue
            if relation is not None and e.relation != relation:
                continue
            result.append(e)
        return result

    def upstream_of(self, node_id: str) -> list[ProductNode]:
        """Products that FEED into this product (source→this)."""
        nodes: list[ProductNode] = []
        for e in self.edges:
            if e.target_id == node_id:
                n = self.get_node(e.source_id)
                if n and n not in nodes:
                    nodes.append(n)
        return nodes

    def downstream_of(self, node_id: str) -> list[ProductNode]:
        """Products that this product FEEDS into (this→target)."""
        nodes: list[ProductNode] = []
        for e in self.edges:
            if e.source_id == node_id:
                n = self.get_node(e.target_id)
                if n and n not in nodes:
                    nodes.append(n)
        return nodes

    def substitute_of(self, node_id: str) -> list[ProductNode]:
        """Products that this product SUBSTITUTES or is SUBSTITUTED by."""
        nodes: list[ProductNode] = []
        for e in self.edges:
            if e.relation != RelationshipType.SUBSTITUTE:
                continue
            if e.source_id == node_id:
                n = self.get_node(e.target_id)
                if n and n not in nodes:
                    nodes.append(n)
            elif e.target_id == node_id:
                n = self.get_node(e.source_id)
                if n and n not in nodes:
                    nodes.append(n)
        return nodes
