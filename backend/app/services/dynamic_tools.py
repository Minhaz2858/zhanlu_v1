"""Dynamic tool loading — per-turn intent → tool-subset selection.

The agent registry exposes 60+ tools; sending every full JSON schema on
every turn costs ~12–24k tokens of prompt (money + latency) and dilutes
tool-choice accuracy. This module keeps an always-on core (memory, files,
code, grounding, delegation, dashboards) and adds only the periphery tools
whose description best matches THIS turn's user message.

Selection strategy (model-agnostic, no extra LLM call):

1. Embed the user message (local MiniLM via document_ingestion.embedder).
2. Embed every periphery tool's description once per process (cached).
3. Cosine similarity → top-k periphery tools.
4. Fallback to lexical token-overlap when embeddings are unavailable.
5. Any error → return the FULL original list (fail-open: dynamic loading
   must never cripple the agent).

Ordering is preserved (core first, then selected periphery, both in their
original relative order) so provider tool-ordering heuristics keep working.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_DESC_EMBED_CACHE: dict[str, object] = {}


def _mode() -> str:
    return (getattr(settings, "TOOL_LOADING_MODE", "all") or "all").lower()


def _core_names() -> set[str]:
    return {n for n in (getattr(settings, "TOOL_LOADING_CORE", None) or []) if n}


def _schema_name(schema: dict) -> str:
    return str((schema.get("function") or {}).get("name") or "")


def _schema_description(schema: dict) -> str:
    return str((schema.get("function") or {}).get("description") or "")


def _embed_or_none(texts: list[str]):
    """Embed ``texts`` with the shared local embedder; None on failure."""
    try:
        from app.services.document_ingestion import embedder

        out = embedder.embed_texts(texts)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("dynamic_tools: embed failed (lexical fallback): %s", exc)
        return None


def _embed_one_or_none(text: str):
    try:
        from app.services.document_ingestion import embedder

        return embedder.embed_query(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("dynamic_tools: query embed failed (lexical fallback): %s", exc)
        return None


def _lexical_scores(user_message: str, periphery: list[dict]) -> list[float]:
    """Token-overlap scores (0..1) between the message and each tool."""
    try:
        from app.services.document_ingestion.store import tokenize_lexical
    except Exception:
        return [0.0] * len(periphery)
    q = set(tokenize_lexical(user_message))
    if not q:
        return [0.0] * len(periphery)
    out = []
    for schema in periphery:
        doc = set(tokenize_lexical(_schema_description(schema)))
        doc.update(tokenize_lexical(_schema_name(schema)))
        if not doc:
            out.append(0.0)
            continue
        out.append(len(q & doc) / len(q))
    return out


def _select_periphery(
    user_message: str,
    periphery: list[dict],
    top_k: int,
) -> list[dict]:
    """Return the top-k periphery schemas for this message (original order)."""
    if not periphery or not user_message or top_k <= 0:
        return list(periphery)

    qvec = _embed_one_or_none(user_message)
    if qvec is not None:
        try:
            import numpy as np

            texts = [_schema_description(s) or _schema_name(s) for s in periphery]
            mat = _embed_or_none(texts)
            if mat is not None and len(mat) == len(periphery):
                mat = np.asarray(mat, dtype=np.float32)
                q = np.asarray(qvec, dtype=np.float32).reshape(-1)
                if mat.shape[1] == q.shape[0] and np.linalg.norm(q) > 0:
                    norms = np.linalg.norm(mat, axis=1)
                    scores = (mat @ q) / (norms * np.linalg.norm(q) + 1e-9)
                    order = np.argsort(-scores)[:top_k]
                    picked = {int(i) for i in order if scores[int(i)] > 0}
                    if picked:
                        return [periphery[i] for i in range(len(periphery)) if i in picked]
        except Exception as exc:  # noqa: BLE001
            logger.debug("dynamic_tools: cosine selection failed: %s", exc)

    # Lexical fallback.
    scores = _lexical_scores(user_message, periphery)
    ranked = sorted(
        ((s, i) for i, s in enumerate(scores)),
        key=lambda t: -t[0],
    )
    picked = {i for s, i in ranked[:top_k] if s > 0}
    if not picked:
        # No periphery tool shared any token — don't gamble; caller treats
        # an empty pick as "return the full list".
        return list(periphery)
    return [periphery[i] for i in range(len(periphery)) if i in picked]


def select_tools_for_turn(
    schemas: list[dict],
    user_message: Optional[str] = None,
) -> list[dict]:
    """Filter ``schemas`` to the always-on core + intent-relevant periphery.

    - ``TOOL_LOADING_MODE=all`` → returns ``schemas`` unchanged.
    - ``TOOL_LOADING_MODE=dynamic`` → core + top-k periphery.
    - Any failure → returns ``schemas`` unchanged (fail-open).

    Schemas missing a ``function.name`` are always kept (defensive).
    """
    if not schemas:
        return schemas
    if _mode() != "dynamic":
        return list(schemas)
    try:
        core = _core_names()
        core_list: list[dict] = []
        periphery: list[dict] = []
        for schema in schemas:
            name = _schema_name(schema)
            if not name or name in core:
                core_list.append(schema)
            else:
                periphery.append(schema)
        if not periphery:
            return list(schemas)
        top_k = int(getattr(settings, "TOOL_LOADING_PERIPHERY_TOP_K", 12) or 12)
        selected = _select_periphery(user_message or "", periphery, top_k)
        if not selected or len(selected) == len(periphery):
            return list(schemas)
        return core_list + selected
    except Exception as exc:  # noqa: BLE001
        logger.warning("dynamic_tools: selection failed, using full list: %s", exc)
        return list(schemas)
