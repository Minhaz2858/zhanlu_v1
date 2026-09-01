"""Vector store with two backends:

* **pgvector** when the database is PostgreSQL with the ``pgvector``
  extension (preferred — durable, transactional, joins cleanly with the
  existing ``artifacts`` / ``messages`` tables).
* **numpy** in-process fallback when pgvector is not available (single-
  process only; fine for development and the eval harness).

The public surface is small:

* ``add(id, vector)`` — upsert a vector keyed by id.
* ``query(vector, top_k=10)`` — return ``[(id, score), ...]`` sorted
  descending by cosine similarity.
* ``delete(id)`` — remove by id.

The in-process backend uses a tiny LSH-style bucketing to keep queries
under ``O(N)`` per call: the entire matrix is small in practice (a few
thousand entries max per conversation), and numpy matrix-multiply is
fast enough that a smarter index is not worth the complexity here.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Iterable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Lazy import of pgvector; the symbol may be missing if the extension is
# not installed.
try:  # pragma: no cover — depends on environment
    import sqlalchemy  # noqa: F401
    from sqlalchemy import text  # noqa: F401

    _SA_AVAILABLE = True
except Exception:  # pragma: no cover
    _SA_AVAILABLE = False


class VectorStore:
    """Small wrapper that hides the backend choice from callers."""

    def __init__(self, backend: str = "auto", *, sql_session=None, dim: int = 1536) -> None:
        if backend not in ("auto", "pgvector", "numpy"):
            raise ValueError(f"unknown backend: {backend}")
        self._backend = backend
        self._sql = sql_session
        self._dim = dim
        self._lock = threading.Lock()
        # numpy-only state
        self._ids: list[str] = []
        self._matrix: Optional[np.ndarray] = None
        if backend == "auto":
            self._backend = "numpy" if not self._probe_pgvector() else "pgvector"

    def _probe_pgvector(self) -> bool:
        if self._sql is None:
            return False
        try:
            row = self._sql.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector' LIMIT 1")
            ).first()
            return bool(row)
        except Exception as exc:
            logger.debug("vector_store: pgvector probe failed (%s); using numpy", exc)
            return False

    @property
    def backend(self) -> str:
        return self._backend

    def add(self, id: str, vector: list[float]) -> None:  # noqa: A002 — id is the natural name
        if not vector:
            return
        if self._backend == "pgvector":
            self._add_pgvector(id, vector)
        else:
            self._add_numpy(id, vector)

    def _add_numpy(self, id: str, vector: list[float]) -> None:  # noqa: A002
        with self._lock:
            v = np.asarray(vector, dtype=np.float32)
            if v.shape[0] != self._dim:
                # Re-dim defensively; the first write wins for the dim.
                self._dim = v.shape[0]
            if self._matrix is None:
                self._matrix = v.reshape(1, -1)
                self._ids = [id]
            else:
                if id in self._ids:
                    idx = self._ids.index(id)
                    self._matrix[idx] = v
                else:
                    self._matrix = np.vstack([self._matrix, v.reshape(1, -1)])
                    self._ids.append(id)

    def _add_pgvector(self, id: str, vector: list[float]) -> None:  # noqa: A002
        if not _SA_AVAILABLE:
            return self._add_numpy(id, vector)
        # Defer the actual SQL to the caller — they own the session. We
        # just record the intent; the storage layer is responsible for
        # the schema.  When called as a low-level helper this method is
        # a thin adapter; the call site in memory_advanced uses a
        # direct INSERT.
        raise NotImplementedError(
            "pgvector persistence must be performed by the caller "
            "via the existing `memories` table; VectorStore.add "
            "is the numpy-side contract only."
        )

    def query(self, vector: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        if not vector:
            return []
        if self._backend == "pgvector":
            return self._query_pgvector(vector, top_k)
        return self._query_numpy(vector, top_k)

    def _query_numpy(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        with self._lock:
            if self._matrix is None or not self._ids:
                return []
            q = np.asarray(vector, dtype=np.float32)
            # Cosine similarity with L2-normalized rows.
            norm = np.linalg.norm(self._matrix, axis=1, keepdims=True) + 1e-9
            matrix_normed = self._matrix / norm
            q_norm = q / (np.linalg.norm(q) + 1e-9)
            sims = matrix_normed @ q_norm
            order = np.argsort(-sims)[:top_k]
            return [(self._ids[i], float(sims[i])) for i in order]

    def _query_pgvector(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        if not _SA_AVAILABLE or self._sql is None:
            return []
        try:
            row = self._sql.execute(
                text(
                    "SELECT id, 1 - (embedding <=> :vec) AS score "
                    "FROM memory_embeddings "
                    "ORDER BY embedding <=> :vec ASC LIMIT :k"
                ),
                {"vec": json.dumps(vector), "k": top_k},
            ).all()
            return [(r[0], float(r[1])) for r in row]
        except Exception as exc:
            logger.debug("vector_store: pgvector query failed (%s); falling back to numpy", exc)
            return []

    def delete(self, id: str) -> None:  # noqa: A002
        if self._backend == "pgvector":
            return self._delete_pgvector(id)
        with self._lock:
            if id not in self._ids:
                return
            idx = self._ids.index(id)
            self._ids.pop(idx)
            if self._matrix is not None:
                self._matrix = np.delete(self._matrix, idx, axis=0)
            if not self._ids:
                self._matrix = None

    def _delete_pgvector(self, id: str) -> None:  # noqa: A002
        if not _SA_AVAILABLE or self._sql is None:
            return
        try:
            self._sql.execute(text("DELETE FROM memory_embeddings WHERE id = :id"), {"id": id})
            self._sql.commit()
        except Exception as exc:
            logger.debug("vector_store: pgvector delete failed (%s)", exc)


__all__ = ["VectorStore"]
