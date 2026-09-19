"""Shape-only lexical primitives — tokenising, and BM25 with catalog-derived IDF.

Why this module exists
----------------------
The retrieval stage could not see Chinese questions. ``_tokens`` split on
``[^0-9a-zA-Z\\u4e00-\\u9fff]+`` — everything that is not alphanumeric is a
separator — so a CJK run has no separators inside it and the ENTIRE question came
back as one token::

    tokenize("这个月的合同执行情况")  ->  {"这个月的合同执行情况"}

Nothing in a catalog matches that string, so ``rank_relations`` returned no hits
with real evidence (measured live: ``method=none``) and the ordering fell to
whatever weak signal remained — on the live 143-table warehouse, a Chinese
contract question ranked eight ``auth_*`` tables first.

There is no whitespace to split Chinese on, so the unit has to be the bigram
(ChnSentiCorp / standard Chinese IR practice: overlapping 2-character windows).
Bigrams are SHAPE, not vocabulary — no Chinese word list, no table name, no
ERP term, no customer alias appears in this file, so a newly connected database
in any language gets the same treatment with no new code.

IDF comes from the catalog itself: a bigram that occurs in most business tables
(情况, 数据, 信息 …) is worth almost nothing, while a discriminating one (合同,
价目, 发货) is worth a lot. That is why there is no stop-word list here either —
document frequency measures it from the data in hand, per database.

Also here: BM25, so a multi-gram match is scored by rarity and length-normalised
rather than by counting hits. Deterministic, no model, no network.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Mapping

# CJK ideographs (incl. extension A and the compatibility block), plus kana and
# Hangul so the same path covers Japanese/Korean identifiers. Ranges, not words,
# and written as regex escapes — this file contains no CJK character at all, so
# "the ranker carries no vocabulary" is a checkable property (see
# tests/test_retrieval_ranking.py::test_the_ranker_has_no_business_vocabulary).
_CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"
_CJK_CHAR_RE = re.compile(f"[{_CJK}]")
# A CJK run is a maximal stretch of CJK characters; a latin run is the same for
# alphanumerics. Splitting on "not alphanumeric and not CJK" alone is not enough:
# a part may still MIX the two, and treating such a part as one CJK run shredded
# its digits into bigrams while losing the word.
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_SPLIT_RE = re.compile(f"[^0-9a-zA-Z{_CJK}]+")
# The two run kinds, in one pass over a part.
_RUN_RE = re.compile(f"[0-9a-zA-Z]+|[{_CJK}]+")
# A bounded CJK run also enters as a PHRASE, which is precise but rare — a long
# run would only ever match itself, so only short ones are worth carrying.
_PHRASE_MAX = 6
# The unit a Chinese run is cut into: overlapping 2-character windows.
_CJK_NGRAM = 2

# BM25 constants. Standard values; not tuned to any dataset.
_K1 = 1.2
_B = 0.75


def cjk_runs(text: str | None) -> list[str]:
    """Maximal CJK stretches — the units that must be cut into n-grams."""
    if not text:
        return []
    return _CJK_RUN_RE.findall(text)


def has_cjk(text: str | None) -> bool:
    return bool(text) and _CJK_CHAR_RE.search(text or "") is not None


def tokens(text: str | None, *, min_len: int = 2) -> set[str]:
    """Lowercased latin words (longer than ``min_len``-1) + CJK bigrams.

    A one-character CJK run yields itself, so a single-character question word
    is not silently dropped.
    """
    return set(token_list(text, min_len=min_len))


def token_list(text: str | None, *, min_len: int = 2) -> list[str]:
    """Same as :func:`tokens` but as a BAG — term frequency needs the repeats."""
    if not text:
        return []
    out: list[str] = []
    for part in _SPLIT_RE.split(text.lower()):
        if not part:
            continue
        # A part can still mix scripts ("2026年8月"), so work run by run: latin
        # words stay words, CJK runs become n-grams. Every character of a part is
        # alphanumeric or CJK by construction, so the runs cover it completely.
        for match in _RUN_RE.finditer(part):
            chunk = match.group(0)
            if _CJK_CHAR_RE.match(chunk):
                out.extend(_cjk_terms(chunk))
            elif len(chunk) >= min_len:
                out.append(chunk)
    return out


def _cjk_terms(run: str) -> list[str]:
    """Overlapping bigrams, plus the whole run when it is short enough to be a
    phrase rather than a sentence fragment."""
    terms = _cjk_ngrams(run)
    if _CJK_NGRAM < len(run) <= _PHRASE_MAX:
        terms.append(run)
    return terms


def _cjk_ngrams(run: str, n: int = _CJK_NGRAM) -> list[str]:
    if len(run) < n:
        return [run]
    return [run[i : i + n] for i in range(len(run) - n + 1)]


def identifier_terms(identifier: str | None) -> list[str]:
    """Sub-words of a table/column identifier: ``erp_t_crm_contract`` →
    ``[erp, crm, contract]``. Shape-only, so any naming convention works."""
    if not identifier:
        return []
    return token_list(identifier.replace(".", "_"))


class Bm25Index:
    """A tiny in-memory BM25 over the catalog documents.

    Built per question from documents that already exist (the table-of-contents
    the caller had to build anyway), so a ranking costs no extra query and there
    is no persistent index to invalidate when a database is re-indexed.

    ``idf`` uses the ``ln(1 + (N - df + 0.5)/(df + 0.5))`` form, which is
    non-negative for every term, so a term present in every document adds
    nothing instead of subtracting.
    """

    def __init__(self, docs: Mapping[str, str] | Mapping[str, list[str]]) -> None:
        self._tf: dict[str, Counter[str]] = {}
        self._len: dict[str, int] = {}
        self._df: Counter[str] = Counter()
        for key, raw in (docs or {}).items():
            terms = raw if isinstance(raw, list) else token_list(raw)
            counter = Counter(terms)
            self._tf[key] = counter
            self._len[key] = sum(counter.values()) or 1
            for term in counter:
                self._df[term] += 1
        self._n = len(self._tf)
        self._avg_len = (sum(self._len.values()) / self._n) if self._n else 1.0

    @property
    def size(self) -> int:
        return self._n

    def document_frequency(self, term: str) -> int:
        return self._df.get(term, 0)

    def idf(self, term: str) -> float:
        if not self._n:
            return 0.0
        df = self._df.get(term, 0)
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    def score(
        self,
        query_terms: Iterable[str] | Mapping[str, float],
        *,
        weights: Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        """BM25 score per document for a query.

        ``query_terms`` may be a bag (repeats ignored: BM25 assumes query term
        frequency is not informative) or a ``{term: weight}`` mapping, so
        expanded terms from a metric/glossary definition can be discounted
        relative to the words the user actually typed.
        """
        if isinstance(query_terms, Mapping):
            q_weights = dict(query_terms)
        else:
            q_weights = {t: 1.0 for t in query_terms}
        # A mapping's values are WEIGHTS. Coerce defensively: a caller that hands
        # over a provenance mapping (term → label) must not take the ranking down
        # with a TypeError deep inside a scoring loop.
        q_weights = {
            t: float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) else 1.0
            for t, w in q_weights.items()
        }
        if weights:
            q_weights = {
                t: w * float(weights.get(t, 1.0)) for t, w in q_weights.items()
            }
        out: dict[str, float] = {}
        for key, counter in self._tf.items():
            dl = self._len[key]
            total = 0.0
            for term, weight in q_weights.items():
                tf = counter.get(term, 0)
                if not tf:
                    continue
                idf = self.idf(term)
                if idf <= 0:
                    continue
                denom = tf + _K1 * (1 - _B + _B * dl / (self._avg_len or 1.0))
                total += weight * idf * (tf * (_K1 + 1)) / denom
            if total:
                out[key] = total
        return out

    def matched_terms(self, key: str, query_terms: Iterable[str], limit: int = 4) -> list[str]:
        """Which query terms actually hit ``key`` — the explainable part.

        Ordered by discriminative power (idf) so the evidence string names the
        rare, meaningful grams rather than the filler ones.
        """
        counter = self._tf.get(key) or {}
        hits = [t for t in set(query_terms) if counter.get(t)]
        hits.sort(key=lambda t: (-self.idf(t), t))
        return hits[:limit]


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity, 0.0 when either side is empty or degenerate."""
    va, vb = list(a or []), list(b or [])
    if not va or not vb or len(va) != len(vb):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(va, vb):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)
