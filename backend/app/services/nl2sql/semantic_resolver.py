"""Semantic resolver — map natural-language questions to database entities.

Uses ``rapidfuzz`` token-set ratio to match user questions against
``MetricDefinition.synonyms`` and ``SemanticMapping.synonyms``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ResolvedIntent:
    metric_name: str | None = None
    metric_id: str | None = None
    table_name: str | None = None
    columns: list[str] = field(default_factory=list)
    filters_spec: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    top_k: int = 3


# ── public API ───────────────────────────────────────────────────────


def resolve(
    question: str,
    metrics: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    *,
    min_confidence: float = 0.5,
    top_k: int = 3,
) -> ResolvedIntent:
    """Find the best-matching metric and semantic mapping for *question*.

    Args:
        question: Natural-language user question (e.g. "How many active users?")
        metrics: List of ``MetricDefinition`` rows serialized as dicts.
        mappings: List of ``SemanticMapping`` rows serialized as dicts.
        min_confidence: Threshold below which matches are discarded.
        top_k: Maximum number of candidates to return.

    Returns:
        A ``ResolvedIntent`` with the best match (may be empty) and
        ``candidates`` populated with up to ``top_k`` alternatives.
    """
    from rapidfuzz import fuzz, process

    intent = ResolvedIntent(top_k=top_k)

    if not metrics and not mappings:
        return intent

    # ── Step 1: find best metric ─────────────────────────────────
    metric_candidates: list[tuple[str, str, list[str]]] = []
    for m in metrics:
        name = m.get("name", "")
        synonyms = _safe_list(m.get("synonyms"))
        for syn in [name] + synonyms:
            metric_candidates.append((m.get("id", ""), name, syn))

    if metric_candidates:
        choices = [c[2] for c in metric_candidates]
        results = process.extract(question, choices, scorer=fuzz.token_set_ratio, limit=top_k)
        for _match, score, idx in results:
            if score >= int(min_confidence * 100):
                c = metric_candidates[idx]
                intent.candidates.append({
                    "type": "metric",
                    "id": c[0],
                    "name": c[1],
                    "label": c[2],
                    "score": score / 100.0,
                })
        if intent.candidates:
            best = intent.candidates[0]
            intent.metric_id = best["id"]
            intent.metric_name = best["name"]
            intent.confidence = best["score"]

    # ── Step 2: find best semantic mapping ───────────────────────
    mapping_candidates: list[tuple[str, list[str], str]] = []
    for mp in mappings:
        book = mp.get("business_term", "")
        synonyms = _safe_list(mp.get("synonyms"))
        for syn in [book] + synonyms:
            mapping_candidates.append((
                mp.get("target_table", ""),
                _safe_list(mp.get("target_columns")),
                syn,
            ))

    if mapping_candidates:
        choices = [c[2] for c in mapping_candidates]
        results = process.extract(question, choices, scorer=fuzz.token_set_ratio, limit=top_k)
        for _match, score, idx in results:
            if score >= int(min_confidence * 100):
                c = mapping_candidates[idx]
                intent.candidates.append({
                    "type": "mapping",
                    "table": c[0],
                    "columns": c[1],
                    "label": c[2],
                    "score": score / 100.0,
                })
        # Use the best mapping for table/columns (if not already set by metric)
        mapping_best = [
            c for c in intent.candidates if c.get("type") == "mapping"
        ]
        if mapping_best:
            best = mapping_best[0]
            intent.table_name = best["table"]
            intent.columns = best["columns"]
            if best["score"] > intent.confidence:
                intent.confidence = best["score"]

    return intent


# ── helpers ──────────────────────────────────────────────────────────


def _safe_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    if val is not None:
        return [str(val)]
    return []
