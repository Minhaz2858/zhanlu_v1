"""Embedding model loader — supports MiniLM (default), bge-m3, and hash fallback.

NEVER import sentence-transformers or torch at module top level — they pull in
~500 MB of RAM. All imports happen inside ``_load_model()`` and ``_load_bge_m3()``,
which are called exactly once on first use and cached on the module global
``_MODEL``. Tests monkeypatch these loaders to avoid loading real models.

Selection via ``RAG_EMBEDDING_MODEL`` env var:
    - unset / "minilm"  → MiniLM-L6-v2 (384-dim, English, default)
    - "bge-m3"          → BAAI/bge-m3 (1024-dim, multilingual, opt-in download)
    - "hash"            → LocalHashEmbeddingFunction (256-dim, offline deterministic)
    - <other>           → Falls back to hash with a warning

Backward compatibility:
    - ``embed_texts(texts)`` → np.ndarray (n, dim) float32 — unchanged
    - ``embed_query(text)``  → np.ndarray (dim,) float32 — unchanged
    - ``embed_dim()``        → int — unchanged
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from typing import Any, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Default model (existing behavior)
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_EMBED_DIM = 384

# bge-m3 model (Chinese-optimized, opt-in)
_BGE_M3_NAME = "BAAI/bge-m3"
_BGE_M3_DIM = 1024

# Local hash fallback (offline, deterministic)
_HASH_DIM = 256

_MODEL: Any = None
_MODEL_LOCK = threading.Lock()

# Track which model is currently loaded so we can swap if env changes
_CURRENT_MODEL_KIND: Optional[str] = None


# ---------------------------------------------------------------------------
# LocalHashEmbeddingFunction — offline deterministic fallback
# ---------------------------------------------------------------------------


class LocalHashEmbeddingFunction:
    """Deterministic, dependency-free embedding function (offline fallback).

    Produces 256-dim SHA-256-hashed token vectors with CJK bigram awareness.
    Quality is lower than neural embeddings, but it is reproducible and works
    without any model download — ideal for offline/test/degraded environments.

    Implements the ChromaDB 1.5+ embedding function protocol:
        __call__(texts) -> List[List[float]]
        embed_documents(texts) -> List[List[float]]
        embed_query(input) -> List[List[float]]
        name() -> str
    """

    DIM = _HASH_DIM

    def __init__(self) -> None:
        pass

    def name(self) -> str:
        return "local-hash-embedding-v1"

    def __call__(self, input: List[str]) -> List[List[float]]:  # noqa: A002
        return self.embed_documents(input)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, input: Any = None, text: Any = None) -> List[List[float]]:
        # ChromaDB 1.5+ calls embed_query(input=[query]) — accept both
        q = input if input is not None else text
        if isinstance(q, list):
            q = q[0] if q else ""
        return [self._embed_one(str(q) if q else "")]

    @staticmethod
    def _embed_one(text: str) -> List[float]:
        vec = [0.0] * LocalHashEmbeddingFunction.DIM
        if not text:
            return vec

        # Tokenize: ASCII tokens + CJK bigrams
        tokens: List[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if "0" <= ch <= "9" or "A" <= ch <= "Z" or "a" <= ch <= "z":
                j = i
                while j < len(text) and (
                    "0" <= text[j] <= "9"
                    or "A" <= text[j] <= "Z"
                    or "a" <= text[j] <= "z"
                ):
                    j += 1
                tokens.append(text[i:j].lower())
                i = j
            elif "\u4e00" <= ch <= "\u9fff":
                # Chinese bigram extraction
                if (
                    i + 1 < len(text)
                    and "\u4e00" <= text[i + 1] <= "\u9fff"
                ):
                    tokens.append(text[i : i + 2])
                i += 1
            else:
                i += 1

        # Hash each token into 4 buckets
        for tok in tokens:
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            for k in range(4):
                bucket = h[k * 2] % LocalHashEmbeddingFunction.DIM
                sign = 1.0 if (h[k * 2 + 1] & 1) else -1.0
                vec[bucket] += sign

        # L2 normalize
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# bge-m3 loader (lazy, optional dependency)
# ---------------------------------------------------------------------------


def _load_bge_m3() -> Any:
    """Load the BAAI/bge-m3 embedding model.

    Lazy imports FlagEmbedding / sentence-transformers to avoid pulling in
    torch (~500 MB) at module load time. Returns a model object with the
    ChromaDB-compatible interface (embed_documents, embed_query, name).

    Raises:
        RuntimeError: if bge-m3 cannot be loaded (missing dep, no network).
    """
    try:
        # Prefer FlagEmbedding if available
        try:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore

            logger.info("Loading BGE-M3 via FlagEmbedding ...")
            model = BGEM3FlagModel(
                _BGE_M3_NAME,
                use_fp16=False,
            )

            class _BgeM3FlagAdapter:
                def name(self) -> str:
                    return "bge-m3-flagembedding"

                def __call__(self, input: List[str]) -> List[List[float]]:
                    return self.embed_documents(input)

                def embed_documents(self, texts: List[str]) -> List[List[float]]:
                    out = model.encode(
                        texts,
                        return_dense=True,
                        return_sparse=False,
                        return_colbert_vecs=False,
                    )
                    return out["dense_vecs"].tolist()

                def embed_query(self, input: Any = None, text: Any = None) -> List[List[float]]:
                    q = input if input is not None else text
                    if isinstance(q, list):
                        q = q[0] if q else ""
                    return self.embed_documents([str(q) if q else ""])

            return _BgeM3FlagAdapter()
        except ImportError:
            # Fall back to sentence-transformers (which supports bge-m3)
            from sentence_transformers import SentenceTransformer  # type: ignore

            logger.info("Loading BGE-M3 via sentence-transformers ...")
            model = SentenceTransformer(_BGE_M3_NAME)

            class _BgeM3StAdapter:
                def name(self) -> str:
                    return "bge-m3-sentence-transformers"

                def __call__(self, input: List[str]) -> List[List[float]]:
                    return self.embed_documents(input)

                def embed_documents(self, texts: List[str]) -> List[List[float]]:
                    vecs = model.encode(
                        texts, convert_to_numpy=True, show_progress_bar=False
                    )
                    return vecs.tolist()

                def embed_query(self, input: Any = None, text: Any = None) -> List[List[float]]:
                    q = input if input is not None else text
                    if isinstance(q, list):
                        q = q[0] if q else ""
                    return self.embed_documents([str(q) if q else ""])

            return _BgeM3StAdapter()
    except Exception as exc:
        raise RuntimeError(
            f"Could not load bge-m3 embedding model: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Existing API (preserved)
# ---------------------------------------------------------------------------


def _load_model() -> Any:
    """Actually import + load the sentence-transformers model.

    Patchable by tests (monkeypatch this to return a fake).
    """
    from sentence_transformers import SentenceTransformer  # lazy import

    logger.info("Loading embedding model %r ...", _MODEL_NAME)
    return SentenceTransformer(_MODEL_NAME)


def get_embedder() -> Any:
    """Return the singleton model instance, loading it on first call."""
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                _MODEL = _load_model()
    return _MODEL


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a batch of texts. Returns shape (n, 384) float32."""
    if not texts:
        return np.zeros((0, _EMBED_DIM), dtype="float32")
    model = get_embedder()
    vecs = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return vecs.astype("float32")


def embed_query(text: str) -> np.ndarray:
    """Embed a single query string. Returns shape (384,)."""
    return embed_texts([text])[0]


def embed_dim() -> int:
    return _EMBED_DIM


# ---------------------------------------------------------------------------
# New: get_embedding_function — env-based ChromaDB-compatible function
# ---------------------------------------------------------------------------


def _resolve_model_kind() -> str:
    """Read RAG_EMBEDDING_MODEL env var and normalize to a model kind."""
    env_val = os.environ.get("RAG_EMBEDDING_MODEL", "").strip().lower()
    if env_val in ("", "minilm", "mini-lm", "all-minilm-l6-v2"):
        return "minilm"
    if env_val in ("bge-m3", "bge_m3", "bge", "baai/bge-m3"):
        return "bge-m3"
    if env_val in ("hash", "local-hash", "local_hash"):
        return "hash"
    # Unknown value → warn + fallback
    logger.warning(
        "Unknown RAG_EMBEDDING_MODEL=%r — falling back to LocalHashEmbeddingFunction",
        env_val,
    )
    return "hash"


def get_embedding_function(
    model_kind: Optional[str] = None,
    force_reload: bool = False,
) -> Any:
    """Get a ChromaDB-compatible embedding function based on env or override.

    Args:
        model_kind: optional explicit override ("minilm" | "bge-m3" | "hash").
                    If None, reads ``RAG_EMBEDDING_MODEL`` env var.
        force_reload: if True, discard cached singleton and reload.

    Returns:
        An object implementing the ChromaDB embedding function protocol:
        ``__call__(input)``, ``embed_documents(texts)``, ``embed_query(input)``,
        ``name()``.
    """
    global _MODEL, _CURRENT_MODEL_KIND

    kind = (model_kind or _resolve_model_kind()).lower()

    # Hash mode → always return a fresh instance (cheap, no caching needed)
    if kind == "hash":
        return LocalHashEmbeddingFunction()

    # bge-m3 mode → try load, fall back to hash on failure
    if kind == "bge-m3":
        try:
            fn = _load_bge_m3()
            return fn
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bge-m3 load failed (%s) — falling back to LocalHash", exc
            )
            return LocalHashEmbeddingFunction()

    # Default MiniLM mode — return as a ChromaDB-compatible adapter
    if force_reload:
        _MODEL = None

    class _MiniLMAdapter:
        def name(self) -> str:
            return "minilm-l6-v2"

        def __call__(self, input: List[str]) -> List[List[float]]:  # noqa: A002
            return self.embed_documents(input)

        def embed_documents(self, texts: List[str]) -> List[List[float]]:
            arr = embed_texts(texts)
            if arr.shape[0] == 0:
                return []
            return arr.tolist()

        def embed_query(self, input: Any = None, text: Any = None) -> List[List[float]]:
            q = input if input is not None else text
            if isinstance(q, list):
                q = q[0] if q else ""
            arr = embed_query(str(q) if q else "")
            return [arr.tolist()]

    return _MiniLMAdapter()


def reset_for_tests() -> None:
    """Test-only: clear the cached singleton."""
    global _MODEL, _CURRENT_MODEL_KIND
    _MODEL = None
    _CURRENT_MODEL_KIND = None
