"""Pluggable retriever protocol — backend-agnostic semantic search interface.

Implementations can swap between in-memory (numpy+cosine) and pgvector without
changing call sites.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Retriever(Protocol):
    """Semantic search interface.

    Concrete implementations must implement ``index`` and ``query``.
    """

    def index(self, text: str, description: str = "") -> None:
        """Add a document to the index.

        Args:
            text: The document text (used for embedding).
            description: Optional metadata / description.
        """
        ...

    def query(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Return top-k most similar documents.

        Args:
            query: Search string.
            top_k: Maximum number of results.

        Returns:
            List of ``(text, score)`` tuples ordered by descending similarity.
        """
        ...
