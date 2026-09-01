"""Hybrid dense+sparse retrieval with Reciprocal Rank Fusion (RRF).

Hybrid retrieval combining vector + keyword search.

Pure Python module — no DB, no I/O, no LLM dependency. The functions
here can be unit-tested in isolation and reused by any caller that has
a duck-typed ChromaDB collection object.

Pipeline (hybrid_query_collection):
  1. dense_hits_from_query() — vector search with .query(query_texts=..., n_results=...)
  2. sparse_hits_from_collection() — full-collection lexical scan with .get() + score()
  3. reciprocal_rank_fusion() — merge two ranked lists via RRF(k=60)
  4. return top_k (doc_id, rrf_score) tuples
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Standard RRF k parameter (Cormack et al., 2009).
DEFAULT_RRF_K: int = 60

#: Default weighting between dense and sparse signals (must sum to 1.0).
DEFAULT_DENSE_WEIGHT: float = 0.65
DEFAULT_SPARSE_WEIGHT: float = 0.35

#: Prefetch cap for sparse scan to avoid O(N) over huge collections.
DEFAULT_PREFETCH_LIMIT: int = 500

#: Big-CJK regex — matches consecutive Han characters for bigram extraction.
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

#: English token regex — letters/digits (with internal punctuation).
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-\.]*")

#: Pure numeric tokens.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

#: Single CJK chars (used as stop-list — never index alone).
_CJK_SINGLETON_STOP = set()


# ---------------------------------------------------------------------------
# Term extraction
# ---------------------------------------------------------------------------


def query_terms(text: str) -> Set[str]:
    """Extract query terms: CJK bigrams + English tokens + numeric tokens.

    Singleton CJK characters are excluded (low semantic value, high noise).
    CJK runs of length >= 2 yield sliding bigrams. English tokens are
    lowercased. Numeric tokens are preserved as-is.

    Args:
        text: arbitrary query string (may contain mixed Chinese/English/numbers).

    Returns:
        Deduped set of query terms (bigrams + tokens + numbers).
    """
    if not text or not text.strip():
        return set()

    terms: Set[str] = set()

    # 1. CJK runs → sliding bigrams
    for run in _CJK_RUN.findall(text):
        if len(run) < 2:
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram not in _CJK_SINGLETON_STOP:
                terms.add(bigram)

    # 2. English tokens (lowercased)
    for tok in _TOKEN_RE.findall(text):
        # Skip pure-numeric tokens here (handled below)
        if tok.isdigit() or _NUMBER_RE.fullmatch(tok):
            continue
        terms.add(tok.lower())

    # 3. Numeric tokens
    for num in _NUMBER_RE.findall(text):
        terms.add(num)

    return terms


# ---------------------------------------------------------------------------
# Lexical scoring
# ---------------------------------------------------------------------------


def lexical_score(text: str, terms: Set[str]) -> float:
    """Weighted lexical matching score with length normalization.

    Counts occurrences of each query term in text, weights rare/long terms
    higher, then divides by sqrt(length) to avoid long-doc bias.

    Args:
        text: candidate document text.
        terms: query terms (output of query_terms()).

    Returns:
        Non-negative float score. Zero if no matches or empty inputs.
    """
    if not text or not terms:
        return 0.0

    # Count term occurrences (overlapping allowed, longer matches weighted more)
    score = 0.0
    lower_text = text.lower()
    for term in terms:
        if not term:
            continue
        # Numeric tokens: case-sensitive match (preserve "123.45")
        if _NUMBER_RE.fullmatch(term):
            count = text.count(term)
        else:
            # English/bigram: case-insensitive
            count = lower_text.count(term.lower())
        if count > 0:
            # Longer / rarer terms get higher weight
            score += count * math.log1p(len(term))

    if score == 0:
        return 0.0

    # Length normalization: divide by sqrt(text_length + alpha)
    alpha = 50.0  # smoothing to prevent very-short-doc dominance
    return score / math.sqrt(len(text) + alpha)


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]],
    k: int = DEFAULT_RRF_K,
) -> List[Tuple[str, float]]:
    """Merge multiple ranked lists via Reciprocal Rank Fusion.

    RRF score for a doc that appears in rank `r` of any list is
    `1 / (k + r)`. The combined score across lists is the sum.
    Documents not appearing in a list contribute 0 from that list.

    Args:
        ranked_lists: each inner sequence is an ordered list of doc_ids
                      (best first).
        k: RRF damping constant (higher = less aggressive rank weighting).

    Returns:
        List of (doc_id, rrf_score) sorted by descending score.
    """
    scores: Dict[str, float] = {}

    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst):
            # 1-indexed rank
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)

    # Stable sort by score desc
    sorted_items = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [(doc_id, float(score)) for doc_id, score in sorted_items]


# ---------------------------------------------------------------------------
# Sparse retrieval (lexical scan)
# ---------------------------------------------------------------------------


def sparse_hits_from_collection(
    collection: Any,
    query: str,
    top_k: int = 50,
    prefetch_limit: int = DEFAULT_PREFETCH_LIMIT,
) -> List[Tuple[str, float]]:
    """Retrieve documents from a collection by lexical matching.

    Pulls up to ``prefetch_limit`` documents via ``collection.get()``,
    scores each via ``lexical_score``, returns top_k by score desc.

    Args:
        collection: duck-typed ChromaDB collection with .get() returning
                    dict with keys "ids" and "documents".
        query: raw query string.
        top_k: maximum number of hits to return.
        prefetch_limit: cap on documents scanned from the collection.

    Returns:
        List of (doc_id, score) tuples, sorted descending by score.
    """
    terms = query_terms(query)
    if not terms:
        return []

    try:
        payload = collection.get()
    except Exception:
        return []

    ids: List[str] = payload.get("ids", []) or []
    documents: List[str] = payload.get("documents", []) or []

    if not ids:
        return []

    # Apply prefetch cap
    if prefetch_limit > 0 and len(ids) > prefetch_limit:
        ids = ids[:prefetch_limit]
        documents = documents[:prefetch_limit]

    scored: List[Tuple[str, float]] = []
    for doc_id, doc_text in zip(ids, documents):
        if not doc_text:
            continue
        s = lexical_score(doc_text, terms)
        # Normalize by query-term count to penalize long queries
        # (so 2-term match doesn't look 10x better than 1-term match)
        s = s / (len(terms) + 5)
        if s > 0:
            scored.append((doc_id, s))

    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Dense retrieval (vector query)
# ---------------------------------------------------------------------------


def dense_hits_from_query(
    collection: Any,
    query: str,
    top_k: int = 50,
) -> List[Tuple[str, float]]:
    """Retrieve documents from a collection by vector similarity.

    Wraps ``collection.query(query_texts=[query], n_results=top_k)`` and
    converts distance → score via ``exp(-distance)``.

    Args:
        collection: duck-typed ChromaDB collection with .query().
        query: raw query string (will be embedded by the collection).
        top_k: number of nearest neighbors to return.

    Returns:
        List of (doc_id, score) tuples, sorted descending by score.
    """
    try:
        result = collection.query(query_texts=[query], n_results=top_k)
    except Exception:
        return []

    ids_list = result.get("ids", [[]])[0] if result.get("ids") else []
    distances_list = result.get("distances", [[]])[0] if result.get("distances") else []

    # Defensive: enforce top_k cap (in case mock collection ignores n_results)
    if top_k > 0:
        ids_list = list(ids_list)[:top_k]
        distances_list = list(distances_list)[:top_k]

    scored: List[Tuple[str, float]] = []
    for doc_id, distance in zip(ids_list, distances_list):
        if doc_id is None:
            continue
        # Distance → score via exponential decay
        try:
            d = float(distance)
        except (TypeError, ValueError):
            continue
        score = math.exp(-d)
        scored.append((doc_id, score))

    # ChromaDB returns ascending by distance — sort descending by score
    scored.sort(key=lambda x: -x[1])
    return scored


# ---------------------------------------------------------------------------
# Hybrid query (dense + sparse via RRF)
# ---------------------------------------------------------------------------


def hybrid_query_collection(
    collection: Any,
    query: str,
    top_k: int = 10,
    dense_weight: float = DEFAULT_DENSE_WEIGHT,
    sparse_weight: float = DEFAULT_SPARSE_WEIGHT,
    prefetch_limit: int = DEFAULT_PREFETCH_LIMIT,
    rrf_k: int = DEFAULT_RRF_K,
) -> List[Tuple[str, float]]:
    """Run hybrid (dense + sparse) query against a single collection.

    Combines dense vector search and lexical scan via Reciprocal Rank Fusion.
    Returns top_k (doc_id, rrf_score) tuples.

    Args:
        collection: duck-typed ChromaDB collection (.get + .query).
        query: raw query string.
        top_k: final number of hits returned.
        dense_weight: pre-fusion dense signal weight (used in score rescaling).
        sparse_weight: pre-fusion sparse signal weight.
        prefetch_limit: cap on documents scanned for sparse path.
        rrf_k: RRF damping constant.

    Returns:
        List of (doc_id, rrf_score) tuples, sorted by score desc.
    """
    dense_hits = dense_hits_from_query(collection, query, top_k=top_k)
    sparse_hits = sparse_hits_from_collection(
        collection, query, top_k=top_k, prefetch_limit=prefetch_limit
    )

    # Apply weights before RRF (multiply scores by signal weight)
    dense_ranked = [doc_id for doc_id, _ in dense_hits]
    sparse_ranked = [doc_id for doc_id, _ in sparse_hits]

    # RRF over both ranked lists; re-score by weighting the dense contribution
    fused = reciprocal_rank_fusion([dense_ranked, sparse_ranked], k=rrf_k)

    # Re-weight: each list's contribution is multiplied by its weight
    # dense_ranked contributes dense_weight, sparse_ranked contributes sparse_weight
    weighted_scores: Dict[str, float] = {}
    for doc_id, rrf_score in fused:
        # Determine which lists contained this doc
        in_dense = doc_id in set(dense_ranked)
        in_sparse = doc_id in set(sparse_ranked)
        weight = 0.0
        if in_dense:
            weight += dense_weight
        if in_sparse:
            weight += sparse_weight
        weighted_scores[doc_id] = rrf_score * weight

    sorted_items = sorted(weighted_scores.items(), key=lambda x: -x[1])
    return [(doc_id, float(score)) for doc_id, score in sorted_items[:top_k]]
