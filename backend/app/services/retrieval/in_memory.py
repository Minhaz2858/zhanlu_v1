"""In-memory cosine-similarity retriever.

Stores documents as (text, vector) pairs and computes cosine similarity
on query. No external dependencies beyond the standard library.

For unit tests, pass a deterministic ``embed_fn``. In production, use
``app.services.llm_service``'s embedding endpoint.
"""

from __future__ import annotations

import logging

from app.services.retrieval.base import Retriever

logger = logging.getLogger(__name__)


def _default_embed_fn(text: str, dim: int = 128) -> list[float]:
    """Simple hash-based pseudo-embedding (used when no embed_fn is provided)."""
    import hashlib
    import math

    h = hashlib.sha256(text.encode()).digest()
    vec = [h[i % len(h)] / 255.0 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / (norm or 1.0) for v in vec]


class InMemoryRetriever:
    """Cosine-similarity retriever backed by a plain Python list.

    Suitable for corpora up to ~10k documents. For larger datasets swap
    to a pgvector-based implementation that conforms to the same
    ``Retriever`` protocol.
    """

    def __init__(
        self,
        embed_fn: callable | None = None,
        dim: int = 128,
    ) -> None:
        self._dim = dim
        self._embed = embed_fn or (lambda t: _default_embed_fn(t, dim=dim))
        self._texts: list[tuple[str, list[float]]] = []

    # ── Retriever protocol ──────────────────────────────────────────────

    def index(self, text: str, description: str = "") -> None:
        """Add a document to the index.

        ``description`` is appended to the embedding text so it contributes
        to the semantic representation.
        """
        combined = f"{text} {description}".strip()
        vec = self._embed(combined)
        self._texts.append((text, vec))

    def query(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Return top-k most similar documents by cosine distance."""
        if not self._texts:
            return []

        import math

        q_vec = self._embed(query)

        scored: list[tuple[str, float]] = []
        for text, t_vec in self._texts:
            dot = sum(a * b for a, b in zip(q_vec, t_vec))
            a_norm = math.sqrt(sum(v * v for v in q_vec))
            b_norm = math.sqrt(sum(v * v for v in t_vec))
            sim = dot / (a_norm * b_norm) if a_norm and b_norm else 0.0
            scored.append((text, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
