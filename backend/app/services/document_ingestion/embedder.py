"""Lazy singleton wrapper around sentence-transformers all-MiniLM-L6-v2.

NEVER import sentence-transformers at module top level — it pulls in
torch and ~500 MB of RAM. All imports happen inside ``_load_model()``,
which is called exactly once on first use and cached on the module
global ``_MODEL``. Tests monkeypatch ``_load_model`` to avoid loading
the real model.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_EMBED_DIM = 384

_MODEL: Any = None
_LOCK = threading.Lock()


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
        with _LOCK:
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


def reset_for_tests() -> None:
    """Test-only: clear the cached singleton."""
    global _MODEL
    _MODEL = None
