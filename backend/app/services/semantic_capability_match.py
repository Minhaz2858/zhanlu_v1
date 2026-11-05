"""Semantic capability discovery & specialist selection (Phase 3.2).

ROOT CAUSE ADDRESSED
--------------------
Specialist selection was purely lexical (Phase 2 ``capability_fallback``) —
a task phrased as "who builds our vehicles" never matched the production
agent. This module ranks PUBLISHED agents by EMBEDDING cosine similarity,
reusing the EXISTING local embedding infra
(``memory_advanced.embeddings.get_embeddings_batch`` — all-MiniLM / bge,
LOCAL by default, batched, Redis-cached). No new provider, no second
registry.

Guarantees (mission §C):
- PUBLISHED candidates only (authority), exclusions honored (the failed
  agent never matches itself).
- Department filter honored when supplied (routing, same semantics as
  ``_list_available_agents``).
- Relevance THRESHOLD: below it the list is EMPTY — "our own tools suffice"
  is the honest answer; no unnecessary delegation is ever forced.
- Rationale recorded per selection (matcher kind + score) — auditable.
- Embedding infra unavailable/failing → graceful degrade to the Phase-2
  lexical matcher (labeled in the rationale), never to guessing.

Pure selection core: the embedder is injectable (tests pass a fake; prod
uses the local embedder lazily).
"""
from __future__ import annotations

import math
from typing import Callable, Iterable

from app.services.capability_fallback import select_alternatives

# Below this cosine a candidate is NOT a genuine capability match.
DEFAULT_THRESHOLD = 0.35


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    va, vb = list(a), list(b)
    if not va or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _candidate_text(c: dict) -> str:
    caps = c.get("capabilities")
    caps_text = " ".join(str(x) for x in caps) if isinstance(caps, list) else ""
    return " ".join(filter(None, [
        str(c.get("name") or ""), str(c.get("description") or ""), caps_text,
    ]))[:600]


def _local_embedder() -> Callable[[list[str]], list[list[float]]] | None:
    """Lazy local embedder (no API dependency; LRU+Redis cached upstream).
    None when the infra is unavailable — callers degrade to lexical."""
    try:
        from app.services.memory_advanced.embeddings import get_embeddings_batch

        return get_embeddings_batch
    except Exception:  # noqa: BLE001 — degrade, never crash selection
        return None


def semantic_select_specialists(
    task: str,
    candidates: list[dict],
    *,
    embedder: Callable[[list[str]], list[list[float]]] | str | None = "auto",
    top_k: int = 3,
    threshold: float = DEFAULT_THRESHOLD,
    exclude: set[str] | None = None,
    department: str | None = None,
) -> list[dict]:
    """Select genuinely-capable published specialists for a task.

    ``embedder``: a callable, ``"auto"`` (default — lazily load the LOCAL
    embedder) or ``None`` (force the lexical fallback — testable degrade).

    Returns up to ``top_k`` dicts: {name, kind, rationale, score, source,
    department, data_domains}. EMPTY = no genuine match (own tools suffice).
    """
    if not task or not candidates:
        return []
    excluded = {e.lower() for e in (exclude or set())}

    pool: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name or name.lower() in excluded:
            continue
        if c.get("published") is not True:
            continue  # authority: unpublished is NEVER a candidate
        if department and str(c.get("department") or "").lower() != department.lower():
            continue  # routing: honor the department filter when supplied
        pool.append(c)
    if not pool:
        return []

    embed_fn = None
    if embedder == "auto":
        embed_fn = _local_embedder()
    else:
        embed_fn = embedder  # explicit callable, or None → lexical degrade
    if embed_fn is None:
        # Infra unavailable (or explicitly disabled) — degrade to Phase-2 lexical.
        return _lexical_fallback(task, pool, exclude=excluded, top_k=top_k)

    try:
        texts = [task] + [_candidate_text(c) for c in pool]
        vectors = embed_fn(texts)
        if not vectors or len(vectors) != len(texts):
            return _lexical_fallback(task, pool, exclude=excluded, top_k=top_k)
    except Exception:  # noqa: BLE001 — any embedder failure degrades safely
        return _lexical_fallback(task, pool, exclude=excluded, top_k=top_k)

    query_vec = vectors[0]
    scored: list[tuple[float, dict]] = []
    for c, vec in zip(pool, vectors[1:]):
        score = cosine_similarity(query_vec, vec)
        if score >= threshold:
            scored.append((score, c))
    scored.sort(key=lambda pair: -pair[0])

    return [
        {
            "kind": str(c.get("kind") or "specialist_agent"),
            "name": str(c.get("name")),
            "rationale": (
                f"Published agent; embedding match score {score:.2f} "
                f"(matcher=embedding, threshold={threshold:.2f})"
            ),
            "score": round(score, 4),
            "source": str(c.get("source") or "agent_directory"),
            "department": c.get("department"),
            "data_domains": c.get("data_domains") or [],
        }
        for score, c in scored[: max(1, int(top_k))]
    ]


def _lexical_fallback(
    task: str, pool: list[dict], *, exclude: set[str], top_k: int,
) -> list[dict]:
    """Phase-2 lexical selection over the prefiltered pool (graceful
    degradation; rationale labels the matcher honestly)."""
    lexical = select_alternatives(
        pool,
        required_keywords=tuple(task.split())[:12],
        exclude=exclude,
        limit=top_k,
    )
    for item in lexical:
        item["rationale"] = (
            f"{item.get('rationale', '')} (matcher=lexical, embedding "
            "infra unavailable)".strip()
        )
        item["score"] = None
    return lexical
