"""Fused, explainable evidence for the table-selection stage.

The semantic store ranks by *name and alias* evidence, which is exactly right
for ``erp_t_crm_contract`` but blind to everything else the catalog already
knows: the Chinese description of every table, its column meanings, its
business-domain vocabulary, and the definitions a person wrote for the metrics
the question mentions. Measured on the live 143-table warehouse, a pure-CJK
question (``这个月的合同执行情况``) produced **zero** store evidence — the whole
question was one token — so the ordering was decided by a domain guess and the
top eight candidates were ``auth_*`` tables.

This module fuses the remaining signals into one ordering:

    component        source                              tier
    ----------------------------------------------------------------------
    text (BM25)      table name, description_zh/en,      DISCOVERED
                     domain vocabulary, column names
                     and column descriptions
    columns          semantic_type + unit of the         DISCOVERED
                     columns the question names
    metric           definitions/aliases of the          VERIFIED
                     terminology/metric the question
                     mentions
    embedding        cosine(question, table document)    RUNTIME_INFERRED

Design rules this module obeys, each one the answer to a measured failure:

1. **Ordering only.** Nothing here drops a table. Every hit is a score plus the
   reason for it; the caller decides, and the full catalog stays behind the
   ranked block.
2. **No business vocabulary.** No table name, Chinese word, dialect or customer
   term appears in this file. Chinese is handled as bigrams, and the weight of a
   bigram comes from its document frequency IN THIS CATALOG, so ``情况`` and
   ``数据`` are worth almost nothing while ``合同`` and ``发货`` are worth a lot
   — measured from the data, not from a stop-word list.
3. **Tier order is preserved.** A VERIFIED metric definition can beat an
   inferred embedding similarity, and no inferred signal can beat a verified
   plan (the plan weight lives in ``table_retrieval``).
4. **Weights are bounded and declared.** The text component is normalised to
   ``_W_TEXT`` max; the embedding component is capped at ``_W_EMBED`` — measured
   on this deployment the configured embedder (minilm, monolingual) puts the
   right table first for one question and 32nd for another, so it is fused as a
   bounded tie-breaker, never as the primary signal.

Everything is deterministic: same catalog + same question ⇒ same order.
"""
from __future__ import annotations

import hashlib
import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.db.lexical import Bm25Index, cosine, has_cjk, identifier_terms, token_list, tokens

logger = logging.getLogger(__name__)

# ── weights (declared, so the ordering can be argued about) ────────────────
_W_TEXT = 3.0          # full BM25 match against the table document
_W_METRIC = 1.5        # a VERIFIED metric/terminology definition names this table
_W_COLUMN = {          # a question term names one of the table's columns
    "measure": 1.0,
    "temporal": 0.75,
    "dimension": 0.5,
    "flag": 0.25,
    "identifier": 0.25,
    "unknown": 0.25,
}
_W_COLUMN_CAP = 2.25  # three measure columns are enough of a case
_W_EMBED = 0.75        # bounded tie-breaker — see the module docstring
_METRIC_CAP = 2        # at most two definitions contribute per table

# A BM25 hit worth less than this fraction of the best one contributes nothing:
# the question matched something generic (a shared prefix, a filler bigram) and
# the ordering should not be built on it.
_TEXT_MIN_RATIO = 0.15

# Embedding gate. Below this spread between the best and the median similarity
# the distribution is flat, i.e. the model has nothing to say about this
# question, and a normalised value would manufacture a difference that is not
# there. Measured spreads on the live catalog: 0.145 … 0.302.
_EMB_MIN_SPREAD = 0.03
# Only the upper half of the similarity range earns anything.
_EMB_QUANTILE = 0.5

_MAX_COLUMNS_PER_TABLE = 60
_MAX_DOC_CHARS = 400   # bounding the text that goes into the embedder


@dataclass
class EvidenceHit:
    """One table's fused evidence, with the contribution of each component."""

    table_name: str
    score: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def add(self, component: str, amount: float, why: str = "") -> None:
        if amount <= 0:
            return
        self.score += amount
        self.breakdown[component] = round(self.breakdown.get(component, 0.0) + amount, 3)
        if why and why not in self.evidence:
            self.evidence.append(why)


@dataclass
class EvidenceOutcome:
    """The fused hits plus an honest record of what could not be measured."""

    hits: dict[str, EvidenceHit] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    method: str = "none"

    @property
    def max_score(self) -> float:
        return max((h.score for h in self.hits.values()), default=0.0)


# ── catalog documents ──────────────────────────────────────────────────────

def _domain_by_table(toc: Iterable[dict], domains: list[dict] | None) -> dict[str, str]:
    """Business-domain vocabulary per table, both languages.

    ``build_full_toc`` denormalises ``domain_name`` in English only; the Chinese
    domain name is where a Chinese question (销售, 定价) actually lands, so it is
    read from the domain map when one is available.
    """
    zh_by_en = {
        (d.get("name_en") or "").lower(): (d.get("name_zh") or "")
        for d in (domains or [])
    }
    out: dict[str, str] = {}
    for t in toc:
        name = t.get("table_name") or ""
        dom = t.get("domain_name") or ""
        out[name] = " ".join(x for x in (dom, zh_by_en.get(dom.lower(), "")) if x)
    return out


def _column_terms(entry: dict, cap: int = _MAX_COLUMNS_PER_TABLE) -> tuple[list[str], list[dict]]:
    """Tokens for every column's name + meaning, and the columns themselves.

    A column is how a question's vocabulary ("单价", "数量", "金额") reaches the
    table that stores it even when the table's own description is silent.
    """
    terms: list[str] = []
    cols: list[dict] = []
    for c in (entry.get("columns") or [])[:cap]:
        cname = c.get("name") or ""
        desc = c.get("description") or ""
        stype = (c.get("semantic_type") or "unknown").lower()
        unit = c.get("unit") or ""
        terms.extend(identifier_terms(cname))
        terms.extend(token_list(desc))
        if unit:
            terms.extend(token_list(unit))
        cols.append({"name": cname, "description": desc, "semantic_type": stype, "unit": unit})
    return terms, cols


def _document(entry: dict, domain_text: str, aliases: Iterable[str]) -> tuple[str, list[str]]:
    """The searchable text of one table, and that text as a term bag."""
    name = entry.get("table_name") or ""
    desc_zh = entry.get("description_zh") or ""
    desc_en = entry.get("description_en") or entry.get("description") or ""
    role = entry.get("table_role") or ""
    alias_list = [a for a in (aliases or []) if a]
    col_terms, _ = _column_terms(entry)

    bag: list[str] = []
    bag.extend(identifier_terms(name))
    bag.extend(identifier_terms(name))  # a name match is the strongest text signal
    bag.extend(token_list(desc_zh))
    bag.extend(token_list(desc_en))
    bag.extend(token_list(domain_text))
    bag.extend(token_list(role))
    for a in alias_list:
        bag.extend(token_list(a))
        bag.extend(token_list(a))
    bag.extend(col_terms)

    readable = " ".join(
        x for x in (name, domain_text, desc_zh, desc_en, " ".join(alias_list)) if x
    )
    return readable[:_MAX_DOC_CHARS], bag


def _expand_query(store: Any, question: str) -> tuple[dict[str, str], list[str]]:
    """Terms from the definitions of the metrics/terms this question mentions.

    Returns ``({term: source_label}, sources)``. A question says 履约率; the
    metric's own definition says 发货 / 合同, and those are the words the table
    descriptions use. This is the one component that carries VERIFIED knowledge
    (a person wrote it) into retrieval.
    """
    expansion: dict[str, str] = {}
    sources: list[str] = []
    if store is None:
        return expansion, sources
    primary = tokens(question)
    try:
        terms = store.glossary(question, limit=8) or []
    except Exception as e:  # pragma: no cover - vocabulary is optional
        logger.debug("retrieval_evidence: glossary unavailable: %s", e)
        return expansion, sources

    for t in terms:
        label = (getattr(t, "term", "") or "").strip()
        if not label:
            continue
        text = " ".join(
            x for x in (getattr(t, "definition", ""), " ".join(getattr(t, "aliases", ()) or ())) if x
        )
        # The question's own terms are already the primary query; only the
        # definition's additional words are expansion.
        added = [w for w in token_list(text) if w not in primary]
        if not added:
            continue
        sources.append(label)
        for w in added:
            expansion.setdefault(w, label)
    return expansion, sources


def _embedding_scores(
    question: str, docs: dict[str, str], embedder: Any | None
) -> tuple[dict[str, float], list[str], dict[str, float]]:
    """Normalised embedding evidence per table, the notes, and the raw cosines.

    Gated on the SHAPE of the distribution (top − median) rather than on the
    model's name: a model can be monolingual and still discriminate on this
    catalog because the documents carry latin identifiers — measured — and a
    multilingual model can be uninformative for one question and useful for the
    next. What matters is whether this question produced relative differences.
    """
    notes: list[str] = []
    if embedder is None or not docs:
        return {}, notes, {}
    try:
        names = list(docs.keys())
        texts = [docs[n] for n in names]
        vecs = embedder(texts)
        qvec = embedder([question])[0]
    except Exception as e:
        notes.append(f"embedding similarity unavailable: {type(e).__name__}")
        logger.info("retrieval_evidence: embeddings unavailable: %s", e)
        return {}, notes, {}

    if not isinstance(vecs, list) or len(vecs) != len(names):
        notes.append("embedding similarity skipped: embedder returned an unexpected shape")
        return {}, notes, {}
    sims = {n: cosine(qvec, v) for n, v in zip(names, vecs)}
    sims = {n: s for n, s in sims.items() if s > 0}
    if not sims:
        return {}, notes, {}
    values = list(sims.values())
    top, med = max(values), statistics.median(values)
    spread = top - med
    if spread < _EMB_MIN_SPREAD:
        notes.append(
            f"embedding similarity skipped: flat distribution "
            f"(max {top:.3f} − median {med:.3f} < {_EMB_MIN_SPREAD})"
        )
        return {}, notes, sims
    floor = med + _EMB_QUANTILE * spread
    model = ""
    try:
        model = embedder.name() if hasattr(embedder, "name") else ""
    except Exception:
        model = ""
    notes.append(
        f"embedding similarity used as a bounded tie-breaker "
        f"(max {top:.3f}, median {med:.3f}{', ' + model if model else ''})"
    )
    # {table: normalised value} while the RAW cosine is what gets reported as
    # evidence, so a line in the prompt reads as a measurement, not as a weight.
    return (
        {n: max(0.0, (s - floor) / spread) for n, s in sims.items() if s > floor},
        notes,
        sims,
    )


# ── the fusion ─────────────────────────────────────────────────────────────

def build_documents(
    toc: list[dict],
    *,
    aliases: dict[str, list[str]] | None = None,
    domains: list[dict] | None = None,
) -> dict[str, str]:
    """The readable text of every catalog table — the embedding document.

    Exposed separately so the caller can cache the encoded vectors keyed by a
    digest of exactly these strings, instead of re-encoding 143 documents per
    question.
    """
    alias_map = aliases or {}
    domain_text = _domain_by_table(toc, domains)
    out: dict[str, str] = {}
    for entry in toc:
        name = entry.get("table_name") or ""
        if not name:
            continue
        readable, _bag = _document(entry, domain_text.get(name, ""), alias_map.get(name.lower(), []))
        out[name] = readable
    return out


def fuse_evidence(
    question: str,
    toc: list[dict],
    *,
    store: Any | None = None,
    embedder: Any | None = None,
    aliases: dict[str, list[str]] | None = None,
    domains: list[dict] | None = None,
    enabled: bool = True,
) -> EvidenceOutcome:
    """Score every catalog table for this question. Deterministic; read-only."""
    out = EvidenceOutcome()
    if not enabled or not question or not toc:
        return out

    alias_map = aliases or {}
    domain_text = _domain_by_table(toc, domains)
    docs: dict[str, str] = {}
    bags: dict[str, list[str]] = {}
    for entry in toc:
        name = entry.get("table_name") or ""
        if not name:
            continue
        readable, bag = _document(entry, domain_text.get(name, ""), alias_map.get(name.lower(), []))
        docs[name] = readable
        bags[name] = bag

    if not bags:
        return out

    index = Bm25Index(bags)

    # ── 1. text: the question's own words, rarity-weighted ─────────────────
    primary = token_list(question)
    if not primary:
        return out
    raw = index.score(primary)
    if raw:
        best = max(raw.values())
        for name, value in raw.items():
            ratio = value / best if best else 0.0
            if ratio < _TEXT_MIN_RATIO:
                continue
            hit = out.hits.setdefault(name, EvidenceHit(table_name=name))
            matched = index.matched_terms(name, primary, limit=3)
            hit.add(
                "text",
                _W_TEXT * ratio,
                f"text[{'+'.join(matched)}]" if matched else "text",
            )
        out.method = "fused-text"

    # ── 2. columns: the question names a column the table stores ───────────
    primary_set = set(primary)
    for entry in toc:
        name = entry.get("table_name") or ""
        if not name:
            continue
        _terms, cols = _column_terms(entry)
        if not cols:
            continue
        col_score = 0.0
        matched_cols: list[str] = []
        for col in cols:
            text = f"{col['name']} {col['description']} {col['unit']}"
            if not tokens(text) & primary_set:
                continue
            weight = _W_COLUMN.get(col["semantic_type"], _W_COLUMN["unknown"])
            if col_score + weight > _W_COLUMN_CAP:
                break
            col_score += weight
            matched_cols.append(f"{col['name']}({col['semantic_type']})")
            if len(matched_cols) >= 3:
                break
        if col_score:
            hit = out.hits.get(name) or out.hits.setdefault(name, EvidenceHit(table_name=name))
            hit.add("column", col_score, "column[" + ", ".join(matched_cols) + "]")

    # ── 3. metrics / terminology: VERIFIED definitions ────────────────────
    expansion, sources = _expand_query(store, question)
    if expansion:
        # ``expansion`` maps term → the metric it came from (provenance for the
        # evidence line). BM25 wants WEIGHTS, so the query bag is built from the
        # weights and the labels are used only to label the hit. Passing the
        # provenance dict straight through multiplied a STRING by a float, the
        # exception was swallowed by the caller's guard, and the whole fused
        # component — text, columns, similarity — silently vanished whenever a
        # verified metric matched the question.
        exp_scores = index.score({term: 0.5 for term in expansion})
        if exp_scores and sources:
            out.notes.append(
                "verified vocabulary applied to the search: " + ", ".join(sources[:3])
            )
        for name in exp_scores:
            bag = set(bags.get(name) or ())
            labels = sorted({expansion[t] for t in expansion if t in bag})
            if not labels:
                continue
            hit = out.hits.get(name)
            if hit is None:
                continue
            for label in labels[:_METRIC_CAP]:
                hit.add("metric", _W_METRIC, f"metric:{label}")

    # ── 4. embedding similarity: bounded tie-breaker ──────────────────────
    sims, emb_notes, raw_sims = _embedding_scores(question, docs, embedder)
    out.notes.extend(emb_notes)
    for name, value in (sims or {}).items():
        hit = out.hits.get(name)
        if hit is None:
            continue
        hit.add("embedding", _W_EMBED * value, f"similarity {raw_sims.get(name, 0.0):.2f}")

    for hit in out.hits.values():
        hit.score = round(hit.score, 3)
    if out.hits and out.method == "none":
        out.method = "fused"
    if has_cjk(question):
        out.notes.append("question is Chinese: matched as character bigrams, weighted by catalog frequency")
    return out


# ── embedding cache ────────────────────────────────────────────────────────
# Encoding every catalog description takes seconds on CPU. The documents are a
# pure function of the catalog rows, so the vectors are cached keyed by a digest
# of the texts that produced them: a re-index changes the digest, so the cache
# misses by itself and there is nothing to invalidate. Bounded, in-process,
# never persisted.
_EMBED_CACHE: dict[str, list] = {}
_EMBED_CACHE_MAX = 2


def _embedder_fingerprint(embedder: Any) -> str:
    """Identify the model that produced the vectors.

    The cache must not outlive a model change: swapping ``RAG_EMBEDDING_MODEL``
    at runtime would otherwise keep serving vectors from the previous model.
    ``name()`` is the Chroma-protocol capability the embedders here implement;
    the class identity covers stubs and plain callables that lack it.
    """
    try:
        named = embedder.name() or ""
    except Exception:
        named = ""
    cls = type(embedder)
    return f"{named}|{cls.__module__}.{cls.__qualname__}"


def cache_documents(
    embedder: Any, docs: dict[str, str], cache_key: str = ""
) -> Any:
    """Wrap ``embedder`` so the catalog documents are encoded at most once.

    Returns a callable with the same contract. Only the DOCUMENT call is cached
    — it is the expensive one and it repeats across questions of the same
    catalog; the question itself is always embedded fresh.
    """
    if embedder is None or not docs:
        return embedder

    names = list(docs.keys())
    texts = [docs[n] for n in names]
    digest = hashlib.sha1("|".join(names + texts).encode("utf-8")).hexdigest()
    key = f"{cache_key}|{_embedder_fingerprint(embedder)}|{digest}"

    vectors = _EMBED_CACHE.get(key)
    if vectors is None:
        try:
            vectors = embedder(texts)
        except Exception as e:  # pragma: no cover - degraded embedder
            logger.info("retrieval_evidence: document embedding failed: %s", e)
            return embedder
        if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
            _EMBED_CACHE.pop(next(iter(_EMBED_CACHE)))
        _EMBED_CACHE[key] = vectors

    def _cached(texts_in: list[str]) -> Any:
        if list(texts_in) == texts:
            return vectors
        return embedder(texts_in)

    return _cached
