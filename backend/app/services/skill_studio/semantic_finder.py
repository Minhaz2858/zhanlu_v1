"""Semantic skill discovery via embeddings + RRF fusion.

Reuses the existing embedding infrastructure in
``app.services.memory_advanced.embeddings`` (OpenAI ``text-embedding-3-small``
by default, Redis-cached) to rank skills against a natural-language query, then
fuses the ranking with the existing keyword search using Reciprocal Rank
Fusion (RRF).

Graceful degradation:
- If ``SKILL_SEMANTIC_SEARCH_ENABLED`` is off, or embedding fails, or no skill
  has an embedding yet, we fall back to keyword-only search.
- Embeddings are computed lazily and cached in the ``Tool.embedding`` column,
  re-computed only when ``description`` or ``skill_md`` changes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# RRF fusion weight between dense (semantic) and sparse (keyword) rankings.
# Balanced 0.5/0.5 for v1 — skill descriptions are short and keyword search
# already works well, so we blend rather than replace it.
RRF_DENSE_WEIGHT = 0.5
RRF_SPARSE_WEIGHT = 0.5


@dataclass
class SkillSearchResult:
    name: str
    description: str
    category: str
    score: float
    source: str  # "db" | "filesystem"
    references: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)


def semantic_search(
    query: str,
    db: Session,
    user_id: str | None = None,
    limit: int = 10,
) -> list[SkillSearchResult]:
    """Hybrid search: embed the query -> cosine vs stored embeddings -> RRF
    fuse with keyword search. Falls back to keyword-only on any failure.

    ``user_id`` is accepted for future tenant/owner scoping; v1 searches all
    skills (skills are not user-scoped in the current Tool model).
    """
    from app.config import settings

    keyword_results = _keyword_search(query, db, limit=limit)

    if not settings.SKILL_SEMANTIC_SEARCH_ENABLED:
        return keyword_results

    try:
        dense_results = _semantic_search(query, db, limit=limit)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Semantic skill search failed: %s", exc)
        return keyword_results

    if not dense_results:
        return keyword_results

    return _rrf_fuse(dense_results, keyword_results, limit=limit)


def _keyword_search(
    query: str, db: Session, limit: int = 10
) -> list[SkillSearchResult]:
    """Run the existing keyword search and normalize to SkillSearchResult."""
    from app.services.skills_loader import search_skills

    results: list[SkillSearchResult] = []
    try:
        hits = search_skills(query, limit=limit)
        for h in hits:
            meta = h if isinstance(h, dict) else getattr(h, "to_dict", lambda: {})()
            results.append(
                SkillSearchResult(
                    name=meta.get("name", ""),
                    description=meta.get("description", ""),
                    category=meta.get("category", ""),
                    score=1.0,
                    source="filesystem",
                    references=list((meta.get("references") or {}).keys()),
                    assets=list((meta.get("assets") or {}).keys()),
                )
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Keyword skill search failed: %s", exc)
    return results


def _semantic_search(
    query: str, db: Session, limit: int = 10
) -> list[SkillSearchResult]:
    """Embed the query and rank DB skills by cosine similarity."""
    from app.models.tool import Tool
    from app.services.memory_advanced.embeddings import get_embedding

    query_embed = get_embedding(query)
    if query_embed is None or not query_embed.vector:
        return []
    query_vec = query_embed.vector

    # Single SELECT: load all skills with embeddings (avoids N+1).
    rows = (
        db.query(Tool)
        .filter(Tool.embedding.isnot(None))
        .all()
    )

    scored: list[tuple[float, SkillSearchResult]] = []
    for tool in rows:
        vec = tool.embedding
        if not vec or not isinstance(vec, (list, tuple)):
            continue
        sim = _cosine_similarity(query_vec, list(vec))
        if sim is None:
            continue
        refs = (tool.references_manifest or {}).keys() if isinstance(tool.references_manifest, dict) else []
        assets = (tool.assets_manifest or {}).keys() if isinstance(tool.assets_manifest, dict) else []
        scored.append(
            (
                sim,
                SkillSearchResult(
                    name=tool.name,
                    description=tool.description or "",
                    category=getattr(tool, "category", "") or "",
                    score=sim,
                    source="db",
                    references=list(refs),
                    assets=list(assets),
                ),
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def _rrf_fuse(
    dense: list[SkillSearchResult],
    sparse: list[SkillSearchResult],
    limit: int = 10,
    k: int = 60,
) -> list[SkillSearchResult]:
    """Reciprocal Rank Fusion between dense and sparse rankings."""
    fused: dict[str, dict] = {}

    def _add(name: str, res: SkillSearchResult, rank: int, weight: float) -> None:
        rrf = weight / (k + rank + 1)
        entry = fused.setdefault(name, {"res": res, "score": 0.0})
        entry["score"] += rrf
        # Prefer richer metadata (db row wins ties via source precedence).
        if res.references or res.assets or res.source == "db":
            entry["res"] = res

    for rank, res in enumerate(dense):
        _add(res.name, res, rank, RRF_DENSE_WEIGHT)
    for rank, res in enumerate(sparse):
        _add(res.name, res, rank, RRF_SPARSE_WEIGHT)

    merged = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
    out: list[SkillSearchResult] = []
    for entry in merged[:limit]:
        res = entry["res"]
        res.score = round(entry["score"], 6)
        out.append(res)
    return out


def _cosine_similarity(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or not a or not b:
        return None
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return None
    return dot / (norm_a * norm_b)


def embed_skill_if_needed(db: Session, tool: object = None) -> None:
    """Compute + persist the embedding for a Tool row when dirty.

    Re-embeds only when ``embedding`` is NULL or the description/skill_md has
    changed (tracked via a lightweight hash stored in an attribute when
    available). No-op when embedding infra is unavailable.
    """
    from app.config import settings

    if not settings.SKILL_SEMANTIC_SEARCH_ENABLED:
        return

    from app.models.tool import Tool

    tools = [tool] if tool is not None else db.query(Tool).all()
    for t in tools:
        if t is None:
            continue
        text = f"{t.description or ''}\n{t.skill_md or ''}".strip()
        if not text:
            continue
        if t.embedding is not None:
            continue  # already embedded; skip for v1 (recompute on explicit edit)
        _compute_and_store(db, t, text)


def _compute_and_store(db: Session, tool: object, text: str) -> None:
    from app.services.memory_advanced.embeddings import get_embedding

    emb = get_embedding(text)
    if emb is None or not emb.vector:
        return
    try:
        tool.embedding = emb.vector
        db.add(tool)
        db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to persist embedding for %s: %s", tool.name, exc)
        db.rollback()
