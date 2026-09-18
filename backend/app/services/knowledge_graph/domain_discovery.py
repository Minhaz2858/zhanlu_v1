"""Business Domain Discovery — the grouping stage of the Business Semantic Layer.

Turns a flat catalog into named business domains so the answering path can scope
itself before it picks tables (context isolation), exactly like the Schema
Discovery pattern: structural communities + semantic clusters + a merge rule +
LLM naming.

Stages (all dialect-agnostic — they read only catalog rows and embeddings):

1. **Structural** — ``kb_table_relation`` rows (FK + inferred join edges) become a
   weighted graph; Louvain community detection finds the physical groupings.
2. **Semantic** — the table vectors already stored in Chroma ``catalog_{kb_id}``
   are clustered with K-Means; ``k`` is chosen by silhouette score.
3. **Merge** — the semantic clusters are the primary grouping (business meaning
   is what a domain *is*), the structural graph places the tables that carry no
   vector but are FK-connected to a group, and whole clusters are united when
   their centroids clear ``settings.KG_DOMAIN_MERGE_THRESHOLD`` (default 0.75),
   each cluster merging at most once. Similarity is measured on *centred* vectors
   (see :func:`center_vectors`); without centring, every pair of document vectors
   looks alike and the whole catalog merges into one domain.
4. **Naming** — one batched LLM call turns each cluster's *actual* member tables,
   descriptions and sample values into a 2–4 word business name (en + zh) plus a
   one-line description. Names are data-derived; no domain vocabulary lives in
   code or prompts.

Nothing is written back into the customer's database — the domain map is stored
in ``kb_domain`` / ``kb_domain_member`` and surfaced through the catalog API.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_catalog import (
    KBDomain,
    KBDomainMember,
    KBTableMeta,
)

logger = logging.getLogger(__name__)
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

_RANDOM_SEED = 42
_NAME_BATCH = 10          # clusters per naming LLM call
_MIN_TABLES_TO_CLUSTER = 4  # below this a single domain is the honest answer
_NO_RELATION_LABEL = -1

# Tokens that carry no business meaning in a physical table name.
_GENERIC_TOKENS = {
    "t", "tb", "tbl", "table", "v", "vw", "view", "f", "d", "s", "stg", "tmp",
    "temp", "data", "info", "master", "mst", "main", "detail", "details", "p",
}

_NAME_SCHEMA = {
    "type": "object",
    "properties": {
        "domains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "name_en": {"type": "string"},
                    "name_zh": {"type": "string"},
                    "description_en": {"type": "string"},
                    "description_zh": {"type": "string"},
                },
                "required": ["index", "name_en"],
            },
        }
    },
    "required": ["domains"],
}

_NAMING_SYSTEM = (
    "You are a data architect naming business domains inside an enterprise "
    "database catalog. Each domain is a group of related tables discovered from "
    "foreign-key structure and semantic similarity. For every group, read its "
    "table names, table descriptions and column sample values, then return:\n"
    "- name_en: a 2-4 word business domain name in Title Case that a business "
    "user would recognise. Use the domain's OWN vocabulary taken from the data "
    "(table names, descriptions, sample values). Never use generic labels such "
    "as 'Other', 'Misc', 'Data', 'Table Group' or 'Domain N'.\n"
    "- name_zh: the same name in Simplified Chinese (2-8 characters).\n"
    "- description_en: one sentence (max 25 words) describing what this domain "
    "covers, naming the key entities found in the data.\n"
    "- description_zh: the same sentence in Simplified Chinese.\n"
    "Return one object per group, echoing its index. Never invent tables or "
    "concepts that are not present in the group."
)


# ── catalog loading ────────────────────────────────────────────────────────


def _load_tables(db: Session, kb_id: str) -> list[KBTableMeta]:
    return (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.is_deleted.is_(False))
        .order_by(KBTableMeta.table_name.asc())
        .all()
    )


def _load_relations(db: Session, kb_id: str) -> list[tuple[str, str, float]]:
    """Return ``(source_table_meta_id, target_table_meta_id, confidence)``."""
    from app.models.knowledge_catalog import KBTableRelation

    rows = (
        db.query(KBTableRelation)
        .filter(KBTableRelation.kb_id == kb_id, KBTableRelation.is_deleted.is_(False))
        .all()
    )
    return [
        (r.source_table_meta_id, r.target_table_meta_id, float(r.confidence or 1.0))
        for r in rows
        if r.source_table_meta_id and r.target_table_meta_id
    ]


def _load_table_vectors(kb_id: str) -> dict[str, list[float]]:
    """Table vectors from Chroma ``catalog_{kb_id}``; ``{}`` when unavailable."""
    out: dict[str, list[float]] = {}
    try:
        from app.services.document_ingestion.store import _get_client

        coll = _get_client().get_collection(f"catalog_{kb_id}")
        got = coll.get(where={"kind": "table"}, include=["embeddings", "metadatas"])
    except Exception:
        logger.info("domain_discovery: no table vectors for kb=%s", kb_id)
        return {}

    metas = got.get("metadatas") or []
    vecs = got.get("embeddings")
    if vecs is None:
        return {}
    try:
        import numpy as np

        vecs = np.asarray(vecs, dtype=float)
    except Exception:
        return {}
    for i, meta in enumerate(metas):
        if not meta:
            continue
        mid = meta.get("table_meta_id")
        if not mid or i >= len(vecs):
            continue
        vec = vecs[i]
        if vec is None or len(vec) == 0:
            continue
        out[str(mid)] = [float(x) for x in vec]
    return out


# ── vector helpers ─────────────────────────────────────────────────────────


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na, nb = _normalize(a), _normalize(b)
    return float(sum(x * y for x, y in zip(na, nb)))


def _centroid(members: list[str], vectors: dict[str, list[float]]) -> list[float] | None:
    vecs = [vectors[m] for m in members if m in vectors]
    if not vecs:
        return None
    width = len(vecs[0])
    acc = [0.0] * width
    for v in vecs:
        if len(v) != width:
            continue
        for i, x in enumerate(v):
            acc[i] += x
    return [x / len(vecs) for x in acc]


def center_vectors(vectors: dict[str, list[float]]) -> dict[str, list[float]]:
    """Remove the shared component from a set of document vectors.

    Table descriptions are embedded from a common template ("[TABLE] name … Type:
    TABLE  Rows: N"), which pushes every pair of table vectors to a high cosine
    similarity regardless of meaning — so an absolute merge threshold like 0.75
    would union the entire catalog into one domain. Subtracting the mean vector
    (the standard anisotropy fix for sentence embeddings) makes the similarity
    measure *relative*: related tables stay close, unrelated ones fall away, and
    a fixed threshold becomes meaningful again.

    Domain- and dialect-agnostic: it uses only the vectors the catalog already
    has. Falls back to the input when there are too few vectors to estimate a
    mean.
    """
    if len(vectors) < 3:
        return dict(vectors)
    width = len(next(iter(vectors.values())))
    usable = [v for v in vectors.values() if len(v) == width]
    if not usable:
        return dict(vectors)
    mean = [0.0] * width
    for vec in usable:
        for i, x in enumerate(vec):
            mean[i] += x
    n = len(usable)
    mean = [x / n for x in mean]
    return {
        key: ([x - mean[i] for i, x in enumerate(vec)] if len(vec) == width else vec)
        for key, vec in vectors.items()
    }


# ── stage 1: structural communities ────────────────────────────────────────


def structural_partition(
    table_ids: list[str], relations: list[tuple[str, str, float]]
) -> dict[str, int]:
    """Louvain communities over the FK / join-edge graph.

    Returns ``{table_meta_id: community_index}``. An empty graph — a schema with
    no declared relationships — yields ``-1`` for every table, which the merge
    stage treats as "unconstrained".
    """
    if not table_ids:
        return {}
    edges = [
        (s, t, max(0.05, float(c)))
        for (s, t, c) in relations
        if s in set(table_ids) and t in set(table_ids) and s != t
    ]
    if not edges:
        return {t: _NO_RELATION_LABEL for t in table_ids}

    try:
        import networkx as nx

        graph = nx.Graph()
        graph.add_nodes_from(table_ids)
        for s, t, c in edges:
            if graph.has_edge(s, t):
                graph[s][t]["weight"] = max(graph[s][t]["weight"], c)
            else:
                graph.add_edge(s, t, weight=c)

        resolution = float(getattr(settings, "KG_DOMAIN_LOUVAIN_RESOLUTION", 1.0) or 1.0)
        try:
            communities = nx.community.louvain_communities(
                graph, weight="weight", resolution=resolution, seed=_RANDOM_SEED
            )
        except TypeError:  # networkx < 3.0 has no seed kwarg
            communities = nx.community.louvain_communities(
                graph, weight="weight", resolution=resolution
            )
        labels: dict[str, int] = {}
        for idx, members in enumerate(communities):
            for node in members:
                labels[str(node)] = idx
        for t in table_ids:
            labels.setdefault(t, _NO_RELATION_LABEL)
        return labels
    except Exception:
        logger.exception("domain_discovery: Louvain failed — structural stage skipped")
        return {t: _NO_RELATION_LABEL for t in table_ids}


# ── stage 2: semantic clusters ─────────────────────────────────────────────


def semantic_partition(
    table_ids: list[str], vectors: dict[str, list[float]]
) -> dict[str, int]:
    """K-Means over table vectors; ``k`` picked by silhouette score.

    Tables without a vector get ``-1``. Fewer than 4 vectors cannot be scored
    meaningfully — everything lands in one cluster.
    """
    ids = [t for t in table_ids if t in vectors and vectors[t]]
    if len(ids) < _MIN_TABLES_TO_CLUSTER:
        return {t: (_NO_RELATION_LABEL if t not in ids else 0) for t in table_ids}

    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score

        matrix = np.asarray([vectors[i] for i in ids], dtype=float)
        max_k = int(getattr(settings, "KG_DOMAIN_MAX_CLUSTERS", 20) or 20)
        upper = max(2, min(max_k, len(ids) - 1))

        best: tuple[float, Any] | None = None
        for k in range(2, upper + 1):
            km = KMeans(n_clusters=k, n_init=10, random_state=_RANDOM_SEED).fit(matrix)
            if len(set(km.labels_)) < 2:
                continue
            try:
                score = silhouette_score(matrix, km.labels_)
            except Exception:
                continue
            if not math.isfinite(score):
                continue
            if best is None or score > best[0]:
                best = (score, km)

        if best is None:
            return {t: 0 for t in ids} | {
                t: _NO_RELATION_LABEL for t in table_ids if t not in ids
            }

        labels = {str(t): int(lbl) for t, lbl in zip(ids, best[1].labels_)}
        for t in table_ids:
            labels.setdefault(t, _NO_RELATION_LABEL)
        logger.info(
            "domain_discovery: semantic stage — %d tables, k=%d, silhouette=%.3f",
            len(ids),
            best[1].n_clusters,
            best[0],
        )
        return labels
    except Exception:
        logger.exception("domain_discovery: K-Means failed — semantic stage skipped")
        return {t: (_NO_RELATION_LABEL if t not in ids else 0) for t in table_ids}


# ── stage 3: merge engine ──────────────────────────────────────────────────


def build_adjacency(relations: list[tuple[str, str, float]]) -> dict[str, set[str]]:
    """Undirected FK/join adjacency for orphan placement."""
    adj: dict[str, set[str]] = {}
    for s, t, _ in relations:
        if s == t:
            continue
        adj.setdefault(s, set()).add(t)
        adj.setdefault(t, set()).add(s)
    return adj


def merge_partitions(
    table_ids: list[str],
    structural: dict[str, int],
    semantic: dict[str, int],
    vectors: dict[str, list[float]],
    neighbours: dict[str, set[str]] | None = None,
    threshold: float | None = None,
) -> list[list[str]]:
    """Combine the structural and semantic views into table clusters.

    Design note (learned the hard way on a 143-table warehouse): a table must
    belong to exactly ONE domain, so the two partitions cannot both be unioned
    through their tables — the join of two partitions of the same set is almost
    always a single block, which collapses the whole catalog into one domain.

    So the semantic partition (business meaning) is the primary grouping; the
    structural graph places the tables that carry no vector yet are FK-connected
    to a group; and whole clusters are then merged only when their centroids are
    business-similar (cosine at or above ``threshold``, default
    ``settings.KG_DOMAIN_MERGE_THRESHOLD``), each cluster merging at most once.
    """
    if threshold is None:
        threshold = float(getattr(settings, "KG_DOMAIN_MERGE_THRESHOLD", 0.75) or 0.75)

    def _labelled(labels: dict[str, int]) -> list[str]:
        return [
            t for t in table_ids if labels.get(t, _NO_RELATION_LABEL) != _NO_RELATION_LABEL
        ]

    labelled = _labelled(semantic)
    if len(labelled) < _MIN_TABLES_TO_CLUSTER:
        labelled = _labelled(structural)
        source = structural
    else:
        source = semantic

    clusters: list[list[str]] = []
    if labelled:
        clusters = _clusters_from_labels(
            {t: source[t] for t in labelled}, labelled
        )

    placed = {t for c in clusters for t in c}
    by_table = {t: i for i, c in enumerate(clusters) for t in c}

    for t in table_ids:
        if t in placed:
            continue
        votes: dict[int, int] = {}
        for nb in (neighbours or {}).get(t, ()):
            idx = by_table.get(nb)
            if idx is not None:
                votes[idx] = votes.get(idx, 0) + 1
        if votes:
            target = max(votes.items(), key=lambda kv: (kv[1], -kv[0]))[0]
            clusters[target].append(t)
            by_table[t] = target
        else:
            clusters.append([t])
            by_table[t] = len(clusters) - 1

    # Cluster-level merge, best-first, each cluster at most once (no chains).
    with_centroid = [
        (i, cen) for i, cen in enumerate(_centroid(c, vectors) for c in clusters) if cen
    ]
    pairs = [
        (round(_cosine(ca, cb), 6), i, j)
        for pos, (i, ca) in enumerate(with_centroid)
        for j, cb in with_centroid[pos + 1 :]
    ]
    if pairs:
        sims = sorted(p[0] for p in pairs)
        logger.info(
            "domain_discovery: merge candidates=%d, above %.2f=%d "
            "(max %.3f, median %.3f)",
            len(sims),
            threshold,
            sum(1 for s in sims if s >= threshold),
            sims[-1],
            sims[len(sims) // 2],
        )

    absorbed: set[int] = set()
    for sim, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        if sim < threshold:
            break
        if i in absorbed or j in absorbed:
            continue
        clusters[i].extend(clusters[j])
        clusters[i] = sorted(set(clusters[i]))
        absorbed.add(j)

    return [c for k, c in enumerate(clusters) if k not in absorbed and c]


def _clusters_from_labels(
    labels: dict[str, int], table_ids: list[str]
) -> list[list[str]]:
    """Group table ids by label; unlabelled (-1) tables stay their own cluster."""
    buckets: dict[int, list[str]] = {}
    for t in table_ids:
        buckets.setdefault(labels.get(t, _NO_RELATION_LABEL), []).append(t)
    return [sorted(v) for v in buckets.values() if v]


def guard_collapse(
    clusters: list[list[str]],
    table_ids: list[str],
    structural: dict[str, int],
    semantic: dict[str, int],
    vectors: dict[str, list[float]],
    min_tables: int = 8,
) -> tuple[list[list[str]], str | None]:
    """Refuse a single-domain answer for a catalog that clearly has structure.

    When the merge stage unites everything, the domain layer stops being an
    isolation tool. Rather than shipping one bucket, fall back to the semantic
    partition (business meaning first), then the structural one, and say so in
    the log. Returns ``(clusters, fallback_used)``.
    """
    if len(clusters) > 1 or len(table_ids) < min_tables:
        return clusters, None

    semantic_groups = {v for v in semantic.values() if v != _NO_RELATION_LABEL}
    structural_groups = {v for v in structural.values() if v != _NO_RELATION_LABEL}
    for name, labels in (("semantic", semantic), ("structural", structural)):
        groups = semantic_groups if name == "semantic" else structural_groups
        if len(groups) < 2:
            continue
        alt = _clusters_from_labels(labels, table_ids)
        alt = absorb_clusters(alt, vectors)
        if len(alt) >= 2:
            logger.warning(
                "domain_discovery: merge collapsed %d tables into one domain — "
                "falling back to the %s partition (%d groups)",
                len(table_ids),
                name,
                len(alt),
            )
            return alt, name
    return clusters, None


def absorb_clusters(
    clusters: list[list[str]],
    vectors: dict[str, list[float]],
    threshold: float | None = None,
    min_tables: int | None = None,
) -> list[list[str]]:
    """Fold clusters too small to be a business domain into their nearest peer.

    A cluster is absorbed when it holds fewer than ``min_tables`` tables
    (default ``settings.KG_DOMAIN_MIN_TABLES``) and a neighbour centroid clears
    ``threshold`` — otherwise it survives, because dropping tables silently is
    worse than an imperfect domain.
    """
    if threshold is None:
        threshold = float(getattr(settings, "KG_DOMAIN_MERGE_THRESHOLD", 0.75) or 0.75)
    if min_tables is None:
        min_tables = int(getattr(settings, "KG_DOMAIN_MIN_TABLES", 2) or 2)
    if len(clusters) < 2:
        return clusters

    weights = [_centroid(c, vectors) for c in clusters]
    # Largest first so small clusters fold into a substantial domain.
    order = sorted(range(len(clusters)), key=lambda i: (-len(clusters[i]), i))

    keep: list[int] = []
    for idx in order:
        if len(clusters[idx]) >= min_tables:
            keep.append(idx)
            continue
        best, best_sim = None, 0.0
        own = weights[idx]
        if own:
            for cand in keep:
                peer = weights[cand]
                if not peer:
                    continue
                sim = _cosine(own, peer)
                if sim > best_sim:
                    best, best_sim = cand, sim
        if best is not None and best_sim >= threshold:
            clusters[best].extend(clusters[idx])
            clusters[best] = sorted(set(clusters[best]))
            weights[best] = _centroid(clusters[best], vectors)
        else:
            keep.append(idx)

    return [clusters[i] for i in keep]


# ── stage 4: naming ────────────────────────────────────────────────────────


def _cluster_digest(members: list[KBTableMeta], limit_desc: int = 160) -> str:
    lines: list[str] = []
    for row in members:
        desc = (row.description_en or row.description_zh or "").strip().replace("\n", " ")
        cols = ""
        sample = row.sample_values if isinstance(row.sample_values, dict) else {}
        bits: list[str] = []
        for col, values in list(sample.items())[:4]:
            if isinstance(values, list) and values:
                bits.append(f"{col}: {', '.join(str(v) for v in values[:4])}")
        if bits:
            cols = " | sample — " + "; ".join(bits)
        role = f" [{row.table_role}]" if row.table_role and row.table_role != "unknown" else ""
        lines.append(f"  - {row.table_name}{role}: {desc[:limit_desc]}{cols}")
    return "\n".join(lines)


def fallback_domain_name(members: list[KBTableMeta]) -> tuple[str, str]:
    """Deterministic, data-derived name for when the naming LLM is unavailable.

    Built from the most frequent meaningful token in the member table names — no
    vocabulary list, nothing hardcoded per customer.
    """
    counts: dict[str, int] = {}
    for row in members:
        for token in str(row.table_name).replace("-", "_").split("_"):
            token = token.strip().lower()
            if not token or token.isdigit() or token in _GENERIC_TOKENS or len(token) < 3:
                continue
            counts[token] = counts.get(token, 0) + 1
    top = [tok for tok, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
    if not top:
        base = str(members[0].table_name) if members else "unclassified"
        return base, base
    label = " ".join(tok.capitalize() for tok in top)
    return label, label


def _build_naming_prompt(
    chunk: list[list[KBTableMeta]], offset: int
) -> str:
    parts: list[str] = []
    for i, members in enumerate(chunk):
        idx = offset + i
        parts.append(
            f"Group {idx} ({len(members)} tables):\n{_cluster_digest(members)}"
        )
    return (
        "Name each group below as a business domain.\n\n" + "\n\n".join(parts)
    )


async def name_domains(
    clusters: list[list[KBTableMeta]],
) -> list[dict[str, str]]:
    """LLM-name the clusters, batched; falls back to token-derived names.

    Returns one dict per cluster: ``{name_en, name_zh, description_en,
    description_zh, source}`` where ``source`` is ``llm`` or ``derived``.
    """
    results: list[dict[str, str]] = [
        {
            "name_en": fallback_domain_name(c)[0],
            "name_zh": fallback_domain_name(c)[1],
            "description_en": "",
            "description_zh": "",
            "source": "derived",
        }
        for c in clusters
    ]
    if not clusters:
        return results

    try:
        from app.services.llm_service import call_llm
    except Exception:
        return results

    for offset in range(0, len(clusters), _NAME_BATCH):
        chunk = clusters[offset : offset + _NAME_BATCH]
        try:
            result = await call_llm(
                messages=[
                    {"role": "system", "content": _NAMING_SYSTEM},
                    {"role": "user", "content": _build_naming_prompt(chunk, offset)},
                ],
                temperature=0.2,
                response_json_schema=_NAME_SCHEMA,
                task_type="domain_naming",
            )
        except Exception:
            logger.exception("domain_discovery: naming call failed (batch %d)", offset)
            continue

        data = result.get("data") if isinstance(result, dict) else {}
        if isinstance(data, list):
            named = data
        else:
            named = (data or {}).get("domains", [])
        for item in named:
            if not isinstance(item, dict):
                continue
            raw_index = item.get("index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, (int, str)):
                continue
            try:
                idx = int(raw_index)
            except (TypeError, ValueError):
                continue
            local = idx - offset
            if local < 0 or local >= len(chunk):
                continue
            name_en = (item.get("name_en") or "").strip()
            if not name_en:
                continue
            results[offset + local] = {
                "name_en": name_en,
                "name_zh": (item.get("name_zh") or "").strip(),
                "description_en": (item.get("description_en") or "").strip(),
                "description_zh": (item.get("description_zh") or "").strip(),
                "source": "llm",
            }
    return results


# ── orchestration ──────────────────────────────────────────────────────────


def _slug(name: str) -> str:
    return " ".join(name.strip().split())[:120]


def persist_domains(
    db: Session,
    kb_id: str,
    clusters: list[list[KBTableMeta]],
    names: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Replace the KB's domain map with ``clusters`` (hard delete, no orphans)."""
    existing = (
        db.query(KBDomain).filter(KBDomain.kb_id == kb_id).all()
    )
    if existing:
        db.query(KBDomainMember).filter(
            KBDomainMember.domain_id.in_([d.id for d in existing])
        ).delete(synchronize_session=False)
        db.query(KBDomain).filter(KBDomain.kb_id == kb_id).delete(
            synchronize_session=False
        )
        db.flush()

    created: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for i, members in enumerate(clusters):
        if not members:
            continue
        meta = names[i] if i < len(names) else {}
        name_en = (meta.get("name_en") or fallback_domain_name(members)[0]).strip()
        if not name_en:
            name_en = fallback_domain_name(members)[0]
        # Two clusters CAN be named identically by the naming model (observed
        # live: two of twenty domains both came back "Sales Order Management"),
        # and kb_domain is unique on (kb_id, name_en) — an un-disambiguated
        # insert aborts the whole catalog index. Disambiguate with the
        # cluster's own biggest table, which keeps the name data-derived.
        if name_en in used_names:
            anchor = max(
                members, key=lambda r: (r.row_count or 0, r.table_name or "")
            ).table_name
            candidate = f"{name_en} ({anchor})"
            seq = 2
            while candidate in used_names:
                candidate = f"{name_en} ({anchor} {seq})"
                seq += 1
            logger.info(
                "domain_discovery: duplicate domain name %r disambiguated to %r",
                name_en,
                candidate,
            )
            name_en = candidate
        used_names.add(name_en)

        domain = KBDomain(
            id=str(uuid.uuid4()),
            kb_id=kb_id,
            name_en=name_en,
            name_zh=(meta.get("name_zh") or "").strip() or None,
            slug=_slug(name_en),
            description_en=(meta.get("description_en") or "").strip() or None,
            description_zh=(meta.get("description_zh") or "").strip() or None,
            member_count=len(members),
            method=str(meta.get("source") or "derived"),
            confidence=None,
        )
        db.add(domain)
        for rank, row in enumerate(members):
            db.add(
                KBDomainMember(
                    id=str(uuid.uuid4()),
                    domain_id=domain.id,
                    kb_id=kb_id,
                    table_meta_id=row.id,
                    table_name=row.table_name,
                    rank=rank,
                    weight=None,
                )
            )
        created.append(
            {
                "id": domain.id,
                "name_en": domain.name_en,
                "name_zh": domain.name_zh,
                "member_count": domain.member_count,
                "method": domain.method,
            }
        )

    # Denormalised pointer so TOC rendering and prompts do not need a join.
    domain_of: dict[str, str] = {}
    for i, members in enumerate(clusters):
        if i >= len(created):
            continue
        for row in members:
            domain_of[row.id] = created[i]["name_en"]
    touched = 0
    for row in {r.id: r for c in clusters for r in c}.values():
        label = domain_of.get(row.id)
        if row.domain_name != label:
            row.domain_name = label
            touched += 1

    try:
        db.commit()
    except Exception:
        # Never leave a poisoned session behind: the catalog index continues
        # (status still reaches "ready") and the previous domain map survives.
        db.rollback()
        logger.exception(
            "domain_discovery: kb=%s domain map NOT persisted (rolled back)", kb_id
        )
        return []
    logger.info(
        "domain_discovery: kb=%s persisted %d domains (%d table pointers updated)",
        kb_id,
        len(created),
        touched,
    )
    return created


async def discover_domains(
    db: Session, kb_id: str, tables: list[KBTableMeta] | None = None
) -> dict[str, Any]:
    """Run the four-stage discovery and persist ``kb_domain`` / ``kb_domain_member``.

    Never raises: failure leaves the previous domain map untouched and returns
    ``{"ok": False, ...}`` so catalog indexing can continue.
    """
    try:
        rows = tables if tables is not None else _load_tables(db, kb_id)
        if not rows:
            return {"ok": False, "reason": "no_tables", "domains": 0}

        by_id = {r.id: r for r in rows}
        table_ids = [r.id for r in rows]
        structural_groups = 0
        vectors: dict[str, list[float]] = {}
        partitioning = "merged"

        if len(table_ids) < _MIN_TABLES_TO_CLUSTER:
            clusters = [table_ids] if table_ids else []
        else:
            raw_vectors = _load_table_vectors(kb_id)
            # Center before measuring similarity between tables: raw document
            # vectors share a large common component, which would make every
            # cluster look alike and merge the whole catalog into one domain.
            vectors = center_vectors(raw_vectors)
            relations = _load_relations(db, kb_id)
            structural = structural_partition(table_ids, relations)
            structural_groups = len(
                {v for v in structural.values() if v != _NO_RELATION_LABEL}
            )
            semantic = semantic_partition(table_ids, vectors)
            clusters = merge_partitions(
                table_ids,
                structural,
                semantic,
                vectors,
                neighbours=build_adjacency(relations),
            )
            clusters = absorb_clusters(clusters, vectors)
            clusters, fallback = guard_collapse(
                clusters, table_ids, structural, semantic, vectors
            )
            if fallback:
                partitioning = f"fallback:{fallback}"
            clusters = [c for c in clusters if c]
            clusters.sort(key=lambda c: (-len(c), str(c[0])))

        cluster_rows = [[by_id[t] for t in c if t in by_id] for c in clusters]
        names = await name_domains(cluster_rows)

        domains = persist_domains(db, kb_id, cluster_rows, names)

        logger.info(
            "domain_discovery: kb=%s → %d domains over %d tables "
            "(structural groups=%d, llm-named=%d)",
            kb_id,
            len(domains),
            len(table_ids),
            structural_groups,
            sum(1 for n in names if n.get("source") == "llm"),
        )
        return {
            "ok": True,
            "domains": len(domains),
            "tables": len(table_ids),
            "structural_groups": structural_groups,
            "partitioning": partitioning,
            "named_by_llm": sum(1 for n in names if n.get("source") == "llm"),
        }
    except Exception:
        logger.exception("domain_discovery: failed for kb=%s", kb_id)
        return {"ok": False, "reason": "exception", "domains": 0}


def _domain_document(domain: KBDomain, member_names: list[str]) -> str:
    return "\n".join(
        [
            f"[DOMAIN] {domain.name_en} / {domain.name_zh or ''}",
            (domain.description_en or domain.description_zh or "").strip(),
            "Tables: " + ", ".join(member_names[:60]),
        ]
    )


def _tokens(text: str) -> set[str]:
    import re

    return {
        tok
        for tok in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", (text or "").lower())
        if len(tok) > 1
    }


def rank_domains_for_question(
    db: Session, kb_ids: list[str], question: str, top_n: int = 3
) -> list[dict[str, Any]]:
    """Rank a question's KB domains by embedding similarity, then lexical overlap.

    Returns ``[{kb_id, domain_id, name_en, name_zh, description, score, tables}]``
    ordered best-first. Empty when the KBs have no domain map yet — callers must
    treat that as "no scoping information", never as "no tables".
    """
    from app.models.knowledge_catalog import KBDomainMember

    if not question or not kb_ids:
        return []

    domains = (
        db.query(KBDomain)
        .filter(KBDomain.kb_id.in_(kb_ids), KBDomain.is_deleted.is_(False))
        .all()
    )
    if not domains:
        return []

    members = (
        db.query(KBDomainMember)
        .filter(KBDomainMember.domain_id.in_([d.id for d in domains]))
        .all()
    )
    members_by_domain: dict[str, list[str]] = {}
    for m in members:
        members_by_domain.setdefault(m.domain_id, []).append(m.table_name)

    docs = [_domain_document(d, members_by_domain.get(d.id, [])) for d in domains]

    vecs: list[list[float]] = []
    qvec: list[float] = []
    try:
        from app.services.document_ingestion.embedder import get_embedding_function

        ef = get_embedding_function()
        embedded = ef(docs)
        vecs = [list(map(float, v)) for v in embedded]
        qvec = list(map(float, ef([question])[0]))
    except Exception:
        logger.info("domain_discovery: domain ranking fell back to lexical overlap")
        vecs, qvec = [], []

    q_tokens = _tokens(question)
    ranked: list[dict[str, Any]] = []
    for i, domain in enumerate(domains):
        table_names = members_by_domain.get(domain.id, [])
        score = 0.0
        if qvec and i < len(vecs):
            score = _cosine(qvec, vecs[i])
        overlap = len(q_tokens & _tokens(f"{domain.name_en} {domain.name_zh} {docs[i]}"))
        ranked.append(
            {
                "kb_id": domain.kb_id,
                "domain_id": domain.id,
                "name_en": domain.name_en,
                "name_zh": domain.name_zh,
                "description": domain.description_en or domain.description_zh,
                "score": round(score, 4),
                "overlap": overlap,
                "table_count": len(table_names),
                "tables": table_names,
            }
        )

    ranked.sort(key=lambda d: (-d["score"], -d["overlap"], -d["table_count"], d["name_en"]))
    return ranked[: max(1, top_n)]


def scoped_tables_for_question(
    db: Session, kb_ids: list[str], question: str
) -> tuple[set[str], list[dict[str, Any]]]:
    """Table names to restrict retrieval to, plus the domains that produced them.

    Returns ``(set(), [])`` when scoping does not apply (no domain map, small
    catalog, or the ranking produced nothing usable) — the caller then keeps the
    full catalog, so a bad domain map can never shrink what the agent can see.
    """
    if not getattr(settings, "KG_DOMAIN_SCOPING_ENABLED", True):
        return set(), []

    min_tables = int(getattr(settings, "KG_DOMAIN_SCOPING_MIN_TABLES", 40) or 40)
    total = (
        db.query(KBTableMeta)
        .filter(
            KBTableMeta.kb_id.in_(kb_ids or []),
            KBTableMeta.is_deleted.is_(False),
        )
        .count()
    )
    if total and total < min_tables:
        return set(), []

    ranked = rank_domains_for_question(db, kb_ids, question, top_n=3)
    if not ranked:
        return set(), []

    # Keep only domains that are plausible: embedding-close, or lexically
    # anchored, or the single best domain of a KB when nothing scored.
    keep: list[dict[str, Any]] = []
    floor = float(getattr(settings, "KG_DOMAIN_SCOPING_MIN_SCORE", 0.25) or 0.25)
    for i, d in enumerate(ranked):
        if d["score"] >= floor or d["overlap"] > 0 or i == 0:
            keep.append(d)

    tables: set[str] = set()
    for d in keep:
        tables.update(d["tables"])

    max_tables = int(getattr(settings, "KG_DOMAIN_SCOPING_MAX_TABLES", 120) or 120)
    if not tables or len(tables) > max_tables:
        return set(), []
    return tables, keep
