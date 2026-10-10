"""Build the two contracts from artifacts the pipeline already produced.

No new pipeline. This module reads what the existing stages computed —
``schema_info`` (the selected tables + the catalog meaning merged into them),
the retrieval result, the access policy, ``db/dialect.py``, ``db/capability_probe.py``,
``db/plan_memory.py``, the catalog relations — and writes it into a
:class:`GroundedPlan`; then it turns the executor's result into a
:class:`CanonicalResultSet`.

The one genuinely new judgement is **slot support**: which things the question
asked for can this plan actually produce?

How a slot is decided (deterministic, no LLM, no name lists)
------------------------------------------------------------
1. The question is split into the units a reader would call "the things being
   asked about": CJK runs (``lexical.cjk_runs``) and latin words — the same
   primitives the retrieval layer already uses, so Chinese needs no new code.
2. Every ``(table, column)`` of the source plus every glossary/metric contract
   becomes a *concept document* (identifier + description + semantic type +
   unit). A slot is *requested* only when the question's own vocabulary matches
   a concept — so an unknown concept never becomes a slot, and the disclosure
   can always name where the concept does live.
3. A requested slot is **supported** when at least one matching concept lives in
   a table this plan selected; otherwise it is recorded in
   ``unsupported_slots`` with ``column_not_selected`` (or ``blocked_by_policy``
   when the tables that hold it are denied to this user).
4. After execution the same slots are re-checked against the RESULT: a slot the
   query returned no column for is ``not_in_result``; a slot whose column is
   constant zero/null while the value crosses a join the catalog does not know
   is ``unvalidated_join`` (this is the measured failure — a zero that is an
   artifact of an unresolved join, presented as a figure), and constant zero
   with no coverage evidence for its tables is ``zero_unproven``.

Rule 1 of the brief lives here: an unsupported slot is NEVER converted into a
zero, a null-derived claim, or invented SQL. It is carried into the result set
and disclosed.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from app.services.db import ratio_contract as ratio_metrics
from app.services.db.contracts import (
    CanonicalResultSet,
    GroundedPlan,
    METADATA_OBJECTS,
    SLOT_KINDS,
    UNSUPPORTED_REASONS,
    coverage_state,
    dialect_of,
    freshness_of,
)

#: BM25 floor for "the question's vocabulary matched this concept". Low on
#: purpose: a false slot costs one disclosure line, a missing slot costs a
#: fabricated figure.
CONCEPT_FLOOR = 0.35

#: Only concepts within this fraction of the unit's BEST match count as its
#: reading. A CJK question run is often a whole clause ("月成本金额是多少"), which
#: tokenises to bigrams that several unrelated columns share (金额 appears in
#: 合同金额, 发货金额 and 成本金额). Without a relative cut, the best match is
#: outvoted by weaker ones and a genuine gap looks supported.
RELATIVE_CUT = 0.5

#: How many matched concept columns stay on a slot for carry detection.
CARRY_LOOKUP_CAP = 200

#: How far after an unsupported slot's label a number still counts as a claim
#: about it. Bounded by the end of the SENTENCE as well, because a correct
#: answer naturally continues with a legitimate figure ("…履约率无法得出。合同
#: 签订数量为 177") and a raw character window flags that as a violation.
CLAIM_WINDOW = 24
_SENTENCE_END = "。！？；\n;!?"

_NUMBER = re.compile(r"\d[\d,\.]*")
_GROUP_BY = re.compile(r"\bgroup\s+by\s+(.+?)(?:\s+(?:having|order\s+by|limit)\b|$)", re.I | re.S)
_ORDER_BY = re.compile(r"\border\s+by\s+(.+?)(?:\s+limit\b|$)", re.I | re.S)
_LIMIT = re.compile(r"\blimit\s+(\d+)", re.I)
_TOPN = re.compile(r"(?:\btop\s*|前\s*)(\d{1,3})", re.I)
_FROM_TABLE = re.compile(r"\b(?:from|join)\s+([`\"\[]?[\w\.]+[`\"\]]?)", re.I)


# ── catalog concepts ───────────────────────────────────────────────────────


def catalog_concepts(db, kb_id: str, limit: int = 4000) -> list[dict[str, Any]]:
    """Every (table, column) of a source, with the catalog's own meaning.

    One query, no per-table loop, and cached on the session so a turn that
    builds a plan twice pays once.
    """
    cached = getattr(db, "_grounding_concepts", None)
    if isinstance(cached, dict) and kb_id in cached:
        return cached[kb_id]
    rows: list[dict[str, Any]] = []
    try:
        from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

        tables = {
            t.id: t.table_name
            for t in db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb_id).all()
        }
        if tables:
            for col in (
                db.query(KBColumnMeta)
                .filter(KBColumnMeta.table_meta_id.in_(list(tables)))
                .limit(limit)
                .all()
            ):
                rows.append(
                    {
                        "table": tables.get(col.table_meta_id, ""),
                        "column": col.column_name or "",
                        "description": (col.description_en or col.description_zh or ""),
                        "description_alt": (col.description_zh or col.description_en or ""),
                        "semantic_type": (col.semantic_type or ""),
                        "unit": (col.unit or ""),
                    }
                )
    except Exception:  # pragma: no cover - catalog always present in a real turn
        rows = []
    try:
        db._grounding_concepts = {**(getattr(db, "_grounding_concepts", None) or {}), kb_id: rows}
    except Exception:
        pass
    return rows


def _concept_docs(concepts: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], dict[str, dict]]:
    docs: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for concept in concepts:
        table = str(concept.get("table") or "")
        column = str(concept.get("column") or "")
        if not table or not column:
            continue
        key = f"{table}\x1f{column}"
        docs[key] = " ".join(
            str(part)
            for part in (
                column,
                concept.get("description"),
                concept.get("description_alt"),
                concept.get("semantic_type"),
                concept.get("unit"),
            )
            if part
        )
        meta[key] = {
            "table": table,
            "column": column,
            "description": concept.get("description") or "",
            "semantic_type": concept.get("semantic_type") or "",
            "unit": concept.get("unit") or "",
        }
    return docs, meta


def question_units(question: str) -> list[str]:
    """The things a question asks about, in the reader's own units.

    CJK runs come back whole (``实际发货数量``, not ``发货/货数/数量``) because
    the consumer of a slot is a sentence shown to a user; latin words are
    lowercase and ≥3 chars (``shipped_qty``) so ``a``/``of``/``by`` never become
    slots.
    """
    from app.services.db.lexical import cjk_runs

    units: list[str] = []
    for run in cjk_runs(question or ""):
        run = run.strip()
        if len(run) >= 2:
            units.append(run)
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question or ""):
        units.append(word.lower())
    # a run that is contained in a longer run adds nothing
    out: list[str] = []
    for unit in units:
        if any(unit != other and unit in other and not unit.isascii() for other in units):
            continue
        if unit not in out:
            out.append(unit)
    return out


# ── slot support ───────────────────────────────────────────────────────────

#: Words that ask HOW MANY rows/records. A count request is a supported question
#: with a canonical answer (a count result), not a business metric the source
#: failed to produce. Kept to the shapes that name no measurement at all, so a
#: metric whose name contains 数量/总数 (合同签订数量, 库存总数) stays a metric.
_COUNT_REQUEST_PATTERNS = (
    re.compile(r"(多少|几)\s*(行|条|个|张|份|笔|项|次|种|台|件|列)"),
    re.compile(r"(行数|条数|记录数|数据量|记录条数|总行数|行总数)"),
    re.compile(r"(总数|总量|个数|总条数|总记录数|记录总数)\s*$"),
    re.compile(r"\bhow\s+many\b", re.IGNORECASE),
    re.compile(r"\bnumber\s+of\s+(rows|records|entries|items)\b", re.IGNORECASE),
    re.compile(r"\b(row|record|entry)\s*count\b", re.IGNORECASE),
    re.compile(r"\bcount\s+of\s+(rows|records|entries|items)\b", re.IGNORECASE),
)

#: Vocabulary that asks about STRUCTURE rather than data. A structure/listing
#: question is not a business-data question, so nothing in it can be an
#: unsupported business metric.
_CATALOG_TERM_PATTERNS = (
    re.compile(r"(表名|字段名|列名|字段清单|表清单|列清单|字段列表|表结构|数据结构)"),
    re.compile(r"(字段|列信息|元数据|主键|外键|索引|数据类型|字段类型|表注释|列注释)"),
    re.compile(r"(表有哪些|有哪些表|有哪些字段|有哪些列|有哪些表名)"),
    re.compile(r"\b(table|column|view|schema|index|constraint|primary key|foreign key)s?\b",
               re.IGNORECASE),
    re.compile(r"\b(information_schema|pg_catalog|pg_namespace|pg_class|pg_attribute|"
               r"sqlite_master|performance_schema|metadata|data dictionary)\b",
               re.IGNORECASE),
)

#: Temporal / ordinal / ranking qualifiers: they SCOPE or ORDER a question
#: ("last month", "top 10", "previous quarter") — they never name a
#: measurement. BM25 happily matches them to catalog columns by shared-bigram
#: accident, and the short-word rule below then classified any ≤6-char match
#: as a metric: live 2026-10-10, "last" (from "last month by revenue") became
#: a fake unsupported metric slot even though the SQL had correctly filtered
#: September, forcing complete=False and a false disclosure into the answer.
_TEMPORAL_QUALIFIER_RE = re.compile(
    r"^(last|next|previous|prior|past|latest|recent|current|this|that|"
    r"first|second|third|top|bottom|nth|best|worst|highest|lowest|"
    r"day|week|month|quarter|year|today|yesterday|tomorrow|"
    r"ytd|mtd|qtd|"
    r"年|月|日|周|季度|上月|上个月|本月|下个月|最近|去年|今年|上|下)$",
    re.IGNORECASE,
)

#: Natural-language scaffolding: no measurement is named, so nothing can be
#: missing. Deliberately generic wording — never a business noun.
_SCAFFOLDING_PATTERNS = (
    re.compile(r"(总览|概览|情况|汇总|总结|报告|分析|说明|概述|简介)"),
    re.compile(r"(请|帮我|麻烦|列出|列举|列表|显示|展示|查询|查看|统计一下|看一下|给我)"),
    re.compile(r"(有哪些|是什么|什么样|如何|是否|相关|所有|全部|明细|清单)"),
    re.compile(
        r"\b(please|show|list|give|tell|overview|summary|report|describe|"
        r"what|which|related|all|every|how|many|much|"
        r"is|are|was|were|be|the|this|that|these|those|"
        r"of|in|for|and|or|with|from|to|by|on|at|as|"
        r"row|rows|record|records|entry|entries|item|items|value|values)\b",
        re.IGNORECASE,
    ),
)

#: Column names a count result comes back as, when the model did not alias it.
_COUNT_COLUMN_NAMES = frozenset(
    {
        "count", "cnt", "num", "n", "row_count", "rowcount", "rows", "records",
        "total", "total_count", "total_rows", "total_records",
        "行数", "总行数", "条数", "记录数", "总数",
    }
)


def requests_row_count(question: str) -> bool:
    """Does the question ask how many rows/records (rather than for a metric)?"""
    text = question or ""
    return any(pattern.search(text) for pattern in _COUNT_REQUEST_PATTERNS)


def count_request_phrase(question: str) -> str:
    """The words in the question that ask for a count (``how many rows``)."""
    text = question or ""
    for pattern in _COUNT_REQUEST_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(0).strip()
    return ""


def is_catalog_term(unit: str) -> bool:
    """Is this unit structure vocabulary (表名, 字段清单, information_schema)?"""
    return any(pattern.search(unit or "") for pattern in _CATALOG_TERM_PATTERNS)


def is_scaffolding(unit: str) -> bool:
    """Is this unit filler that names no measurement (总览, 请列出, overview)?"""
    return any(pattern.search(unit or "") for pattern in _SCAFFOLDING_PATTERNS)


def is_metadata_object(name: str) -> bool:
    """Is this relation the engine's own catalog rather than business data?

    Vendor-generic on purpose: the source is bound at runtime, so this decides
    by the shape every engine's catalog objects share (``pg_*``,
    ``information_schema``, ``sqlite_master``, ``all_tables``, ``v$…``), never by
    which vendor is configured.
    """
    token = str(name or "").strip().strip('`"[]').split(".")[-1].lower()
    whole = str(name or "").strip().strip('`"[]').lower()
    if not token:
        return False
    if whole.startswith("v$"):
        return True
    return any(term in token or term in whole for term in METADATA_OBJECTS)


def sql_is_introspection(sql: str | None) -> bool:
    """True when EVERY relation the SQL reads is one of the engine's own objects.

    Such a query answers a structure question. Its rows are not business data, so
    it can neither support nor convict a business metric.
    """
    if not sql:
        return False
    relations = {str(t).strip().strip('`"[]') for t in _FROM_TABLE.findall(sql)}
    for left, right, _on in _sql_join_pairs(sql):
        relations.update({left, right})
    relations = {r for r in relations if r}
    if not relations:
        return False
    return all(is_metadata_object(r) for r in relations)


def classify_unit(
    unit: str,
    *,
    descriptions: Iterable[str] = (),
    tables: Iterable[str] = (),
    columns: Iterable[str] = (),
    table_names: Iterable[str] = (),
) -> str:
    """What a question unit is — one of :data:`SLOT_KINDS`.

    Only ``metric`` can ever be disclosed as unsupported. The order below is the
    decision order, and each step answers a way a unit can look like a metric
    without being one:

    1. structure vocabulary (表名, 字段清单, pg_catalog) is a listing request;
    2. "how many rows" is a count request with a canonical answer;
    3. a table/entity the source holds is not a measurement;
    4. scaffolding (总览, 请列出) names nothing;
    4b. a temporal/ordinal qualifier (last, top, month) scopes or orders the
        question — it never names a measurement;
    5. a catalog phrase CONTAINED IN the unit names a measurement
       (``实际发货数量`` contains ``发货数量``) — that is a metric;
    6. the unit appearing only INSIDE the catalog's own phrases is the entity
       those phrases are about (``订单`` inside 订单金额/订单日期) — a grouping;
    7. a short unit or an identifier-shaped one is a metric name;
    8. otherwise the catalog cannot name it (``unknown``).
    """
    text = (unit or "").strip()
    if not text:
        return "unknown"
    if is_catalog_term(text):
        return "catalog_term"
    if requests_row_count(text):
        return "generic_count"
    if _TEMPORAL_QUALIFIER_RE.match(text):
        return "temporal"
    known_tables = {str(t).lower() for t in table_names if t}
    lowered = text.lower()
    # An ENTITY word the source's own table names are built from ("product"
    # inside report_product_catalog, "customer" inside crm_customer_master)
    # names the entity those tables hold, not a measurement. Live 2026-10-10
    # (second occurrence of the same bug class): "product" from "top 10
    # product for last month" matched a catalog column by BM25 accident,
    # became a fake metric slot, and forced complete=False — AFTER the
    # temporal-qualifier fix had removed "last". The match is derived from
    # the runtime source's table names, never a hardcoded noun list.
    _table_tokens: set[str] = set()
    for _t in known_tables:
        for _part in re.split(r"[^a-z0-9\u4e00-\u9fff]+", _t.split(".")[-1]):
            if len(_part) >= 3:
                _table_tokens.add(_part)
    if known_tables and (
        lowered in known_tables
        or any(lowered == t.split(".")[-1] for t in known_tables)
        or any(lowered == c.split(".")[-1].lower() for c in columns if c)
        or lowered in _table_tokens
    ):
        return "table"
    if is_scaffolding(text):
        return "scaffolding"
    phrases = [str(d) for d in descriptions if d and str(d) in text]
    if phrases:
        return "metric"
    contained = [str(d) for d in descriptions if d and text in str(d)]
    if contained:
        # the user's word is only a COMPONENT of the catalog's phrases: it names
        # the entity those phrases are about, not one measurement
        return "grouping"
    if len(text) <= 6 or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return "metric"
    return "unknown"


def non_metric_units(
    question: str,
    *,
    evidence: Sequence[Mapping[str, Any]] = (),
    table_names: Iterable[str] = (),
    descriptions: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Every question unit that is NOT a business metric, with its kind.

    Recorded on the plan so "we raised no disclosure" is an auditable decision
    rather than an omission — and so the KIND of non-metric is preserved:
    a count request, a table name, structure vocabulary, scaffolding, and a
    concept the catalog has never seen are four different answers.
    """
    kind_of = {
        str(e.get("unit") or e.get("slot") or ""): str(e.get("kind") or "")
        for e in evidence
        if e.get("slot")
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for unit in question_units(question):
        if unit in seen:
            continue
        seen.add(unit)
        kind = kind_of.get(unit) or classify_unit(
            unit, descriptions=descriptions, table_names=table_names
        )
        if kind == "metric":
            continue
        out.append({"unit": unit, "kind": kind})
    return out



def resolve_slots(
    question: str,
    *,
    concepts: Sequence[Mapping[str, Any]],
    selected_tables: Iterable[str],
    denied_tables: Iterable[str] = (),
    glossary: Sequence[Any] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(evidence, unsupported_slots, metrics)``.

    Evidence entries record WHY a slot is believed to be requested (which
    question unit matched which catalog concept, and how strongly) so a reader
    can audit a disclosure instead of trusting it.
    """
    from app.services.db.lexical import Bm25Index, token_list

    selected = {t for t in selected_tables if t}
    denied = {t for t in denied_tables if t}
    docs, meta = _concept_docs(concepts)
    # Every table name this source holds, so a question unit that names a table
    # is classified as a table and never as a missing measurement.
    known_tables = {str(c.get("table") or "") for c in concepts if c.get("table")}
    known_tables |= selected | denied
    index = Bm25Index(docs)
    units = question_units(question)
    unit_terms = {unit: token_list(unit) for unit in units}

    evidence: list[dict[str, Any]] = []
    slots: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []

    for term in glossary or []:
        label = str(getattr(term, "term", "") or "")
        if not label:
            continue
        if label.lower() not in (question or "").lower():
            continue
        metrics.append(
            {
                "slot": label,
                "label": label,
                "definition": getattr(term, "definition", "") or "",
                "sql_expression": getattr(term, "sql_expression", None),
                "unit": getattr(term, "unit", None),
                "tier": getattr(term, "tier", ""),
                "origin": getattr(term, "origin", ""),
                # the metric contract's own declarations, untouched — a ratio
                # contract is read from these (see db/ratio_contract.py)
                "aggregation": getattr(term, "aggregation", None),
                "bindings": getattr(term, "bindings", None),
            }
        )

    for unit in units:
        terms = unit_terms.get(unit) or []
        if not terms:
            continue
        scores = index.score(terms)
        if not scores:
            continue
        scored = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        # `hits` is the RANKED SHORTLIST (what the concept plausibly is), and the
        # carrier set is everything that cleared the floor at all. A top-12 cap
        # here was the second version of the same live bug: the selected view's
        # `out_fqty` ("出库数量", one shared bigram) ranked below twelve delivery
        # columns, so the column the SQL actually aliased was invisible and an
        # answered slot was reported unanswered.
        hits = scored[:12]
        carrier_hits = [(k, s) for k, s in scored if s >= CONCEPT_FLOOR][:CARRY_LOOKUP_CAP]
        top = max((s for _k, s in hits), default=0.0)
        floor = max(CONCEPT_FLOOR, top * RELATIVE_CUT)
        kept = [(k, s) for k, s in hits if s >= floor]
        if not kept:
            continue

        tables: list[str] = []
        columns: list[str] = []
        matched: set[str] = set()
        phrases: list[str] = []
        carriers = [f"{meta[k]['table']}.{meta[k]['column']}" for k, _s in carrier_hits]
        for key, _score in kept:
            info = meta[key]
            if info["table"] not in tables:
                tables.append(info["table"])
            columns.append(f"{info['table']}.{info['column']}")
            matched.update(index.matched_terms(key, terms))
            phrase = str(info.get("description") or "")
            if len(phrase) >= 2 and phrase in unit:
                phrases.append(phrase)
        if not tables:
            continue

        # The reader-facing name: the catalog's OWN phrase for the concept when
        # the user's words contain it (成本金额 out of 月成本金额是多少), so a
        # disclosure reads like the question rather than like the clause.
        phrase = max(phrases, key=len) if phrases else ""
        label = phrase or unit
        # What IS this unit? Only a business metric can be unsupported: a count
        # request, a table name, structure vocabulary or scaffolding asks for no
        # figure, so disclosing one of them as missing accuses the source of
        # failing at something it was never asked. Measured live: a correct
        # COUNT(*) = 26544 came back with "nothing for 一共有多少行数据,
        # _ref_material_mapping".
        kind = classify_unit(
            unit,
            descriptions=[str(meta[k].get("description") or "") for k, _s in kept],
            tables=tables,
            table_names=known_tables,
        )
        # "Is this the NAME OF A METRIC, or a clause that merely shares bigrams
        # with one?" — the classifier answers exactly that, and keeps the answer
        # on the evidence entry so the decision stays auditable.
        metric_like = kind == "metric"

        supported_by = [t for t in tables if t in selected]
        denied_by = [t for t in tables if t in denied]
        evidence.append(
            {
                "slot": label,
                "label": label,
                "unit": unit,
                #: what this unit is (SLOT_KINDS) — see classify_unit
                "kind": kind,
                "matched": sorted(matched),
                "score": round(top, 4),
                "tables": tables[:8],
                # NOT capped for display: this list is how a result column is
                # traced back to the concept that carries it. An 8-column cap
                # measured live on a 143-table warehouse hid the ONE column the
                # query actually aliased (`out_fqty`), so a slot the query DID
                # answer was reported as "the query returned no column for this".
                "columns": columns[:CARRY_LOOKUP_CAP],
                # The relative cut answers "is this the metric the user asked
                # for"; it must NOT decide "did the query return it". A weaker
                # concept (one shared bigram instead of three) can be the exact
                # column the SQL aliases — live: `out_fqty` on the selected view
                # was cut out of the disclosure list and the answered slot was
                # then reported as unanswered.
                "carriers": carriers[:CARRY_LOOKUP_CAP],
                "source": "catalog",
            }
        )
        if supported_by or not metric_like:
            continue
        reason = "blocked_by_policy" if denied_by else "column_not_selected"
        slots.append(
            {
                "slot": label,
                "label": label,
                "unit": unit,
                #: only metrics reach this list — see classify_unit
                "kind": "metric",
                "reason": reason,
                "detail": (
                    "the columns that carry this are not readable by this user"
                    if reason == "blocked_by_policy"
                    else "the columns that carry this live in "
                    + ", ".join(tables[:4])
                    + ", which this query does not use"
                ),
                "tables": tables[:8],
                "columns": columns[:CARRY_LOOKUP_CAP],
                "matched": sorted(matched),
            }
        )

    return evidence, slots, metrics


# ── the plan ───────────────────────────────────────────────────────────────


def build_grounded_plan(  # noqa: PLR0913 - a plan is a wide record by nature
    *,
    db,
    kb,
    question: str,
    window: tuple | None,
    schema_info: Mapping[str, Any] | None,
    catalog: Sequence[Mapping[str, Any]] | None = None,
    retrieval: Any = None,
    authorized_tables: Iterable[str] | None = None,
    denied_tables: Iterable[str] = (),
    intent_signature: str = "",
    clarification_needed: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> GroundedPlan:
    """Assemble the plan. Pure assembly: nothing here queries the data source."""
    from app.services.db import capability_probe as _caps

    # NOTE: `schema_info` entries key the table as "table" (see
    # nl_answer_service._schema_info_from_toc_selection), while the catalog
    # snapshot uses "table_name". Read both — a plan that silently saw ZERO
    # selected tables marked every slot unsupported, i.e. it turned a wrong key
    # into a false disclosure.
    tables = [_table_of(t) for t in ((schema_info or {}).get("tables") or [])]
    tables = [t for t in tables if t]
    columns: dict[str, list[str]] = {}
    for t in (schema_info or {}).get("tables") or []:
        name = _table_of(t)
        if not name:
            continue
        columns[name] = [
            str(c.get("name") or c.get("column_name") or "")
            for c in (t.get("columns") or [])
            if isinstance(c, dict)
        ]

    try:
        from app.services.db.semantic_store import get_semantic_store

        glossary = get_semantic_store(db, [getattr(kb, "id", "")]).glossary(question, limit=8)
    except Exception:
        glossary = []

    concepts = catalog_concepts(db, getattr(kb, "id", ""))
    evidence, unsupported, metrics = resolve_slots(
        question,
        concepts=concepts,
        selected_tables=tables,
        denied_tables=denied_tables,
        glossary=glossary,
    )

    catalog_version = ""
    try:
        from app.services.db.schema_shape import catalog_digest

        catalog_version = catalog_digest(db, getattr(kb, "id", "")) or ""
    except Exception:
        catalog_version = ""

    capability_version = ""
    try:
        from app.models.kb_capability import KBCapabilityProfile

        row = (
            db.query(KBCapabilityProfile)
            .filter(KBCapabilityProfile.kb_id == getattr(kb, "id", ""))
            .first()
        )
        if row is not None:
            capability_version = f"{row.dialect}:{row.catalog_version or row.detected_at or ''}"
    except Exception:
        capability_version = ""

    from app.services.db import readiness as _readiness

    verdict = _readiness.evaluate(kb)
    warn = list(warnings)
    if verdict.limited and verdict.disclosure:
        warn.append(verdict.disclosure)

    plan = GroundedPlan(
        intent=" ".join((question or "").split()),
        question=question or "",
        intent_signature=intent_signature or "",
        temporal_scope=_temporal_scope(resolve_temporal_scope(question, window)),
        source_ids=[getattr(kb, "id", "")] if getattr(kb, "id", "") else [],
        tables=tables,
        columns=columns,
        authorized_tables=sorted({t for t in (authorized_tables or tables) if t}),
        metrics=metrics,
        dialect=dialect_of(kb),
        evidence=evidence,
        unsupported_slots=unsupported,
        ratio_contracts=[
            contract.to_dict()
            for contract in ratio_contracts_from_metrics(
                metrics,
                units={
                    str(c.get("column") or ""): str(c.get("unit") or "")
                    for c in concepts
                    if c.get("column")
                },
            )
        ],
        non_metric_units=non_metric_units(
            question,
            evidence=evidence,
            descriptions=[
                str(c.get("description") or c.get("description_alt") or "")
                for c in concepts
            ],
            table_names=(
                {str(c.get("table") or "") for c in concepts}
                | {str(c.get("table_name") or "") for c in (catalog or [])}
                | {t for t in tables}
                | {str(t) for t in (authorized_tables or ())}
            ),
        ),
        clarification_needed=list(clarification_needed),
        warnings=warn,
        catalog_version=catalog_version,
        capability_version=capability_version,
    )
    if retrieval is not None:
        try:
            as_dict = retrieval.as_dict() if hasattr(retrieval, "as_dict") else dict(retrieval)
            plan.evidence.append(
                {
                    "slot": "",
                    "label": "retrieval",
                    "matched": [],
                    "score": 0.0,
                    "tables": list(as_dict.get("candidates") or [])[:8],
                    "columns": [],
                    "source": "retrieval",
                    "detail": as_dict.get("method") or "",
                }
            )
        except Exception:
            pass
    if catalog:
        plan.evidence.append(
            {
                "slot": "",
                "label": "catalog",
                "matched": [],
                "score": 0.0,
                "tables": [str(c.get("table_name") or "") for c in catalog][:8],
                "columns": [],
                "source": "catalog",
                "detail": f"{len(catalog)} tables visible to this plan",
            }
        )
    return plan


def resolve_temporal_scope(question: str, window: tuple | None = None) -> tuple | None:
    """The period a question names — relative phrases AND absolute dates.

    Reuses the two existing resolvers rather than a third: the pipeline's own
    ``parse_relative_window`` (passed in as ``window``) and
    ``answer_contract.question_period``, which already handles "2026年8月" /
    "2026-08-01 至 2026-08-31". Without the second, a plan for an absolute
    period carried NO temporal scope, so its coverage and freshness were
    ``unknown`` and a stale-window answer could not be told from a fresh one.
    """
    if window:
        return window
    try:
        from app.services.db.answer_contract import question_period

        found = question_period(question)
        if found:
            window, _phrase = found
            return window
    except Exception:
        return None
    return None


def _temporal_scope(window: tuple | None) -> dict[str, Any]:
    if not window:
        return {"start": None, "end": None, "phrase": "", "kind": "none"}
    try:
        start, end = window[0], window[1]
    except Exception:
        return {"start": None, "end": None, "phrase": str(window), "kind": "unknown"}
    return {
        "start": str(start),
        "end": str(end),
        "phrase": str(window[2]) if len(window) > 2 else "",
        "kind": "relative",
    }


def enrich_plan_from_sql(plan: GroundedPlan, sql: str | None, *, db=None) -> GroundedPlan:
    """Complete the plan once the SQL exists: joins, filters, grain, ordering.

    Mutates and returns ``plan``. Every value is read out of the SQL that will
    actually run, so the plan describes the query rather than the intention.
    """
    sql = sql or ""
    plan.filters = _parse_filters(sql, plan)
    group = _GROUP_BY.search(sql)
    plan.groupings = _split_columns(group.group(1)) if group else []
    order = _ORDER_BY.search(sql)
    plan.ordering = [
        {"column": col, "direction": "desc" if re.search(r"\bdesc\b", order.group(1), re.I) else "asc"}
        for col in _split_columns(order.group(1))
    ] if order else []
    limit = _LIMIT.search(sql)
    topn = _TOPN.search(sql)
    if limit:
        plan.top_n = int(limit.group(1))
    elif topn:
        plan.top_n = int(topn.group(1))
    plan.grain = list(plan.groupings)
    plan.join_paths = validate_join_paths(sql, plan, db=db)
    return plan


def _split_columns(text: str) -> list[str]:
    out: list[str] = []
    for part in (text or "").split(","):
        token = part.strip().strip('`"[]').split()  # drop ASC/DESC
        if token and token[0]:
            out.append(token[0])
    return [c for c in out if c]


def _parse_filters(sql: str, plan: GroundedPlan) -> list[dict[str, Any]]:
    """WHERE predicates, as written. Deliberately shallow — a plan, not a parser."""
    where = re.search(r"\bwhere\b(.+?)(?:\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)", sql, re.I | re.S)
    if not where:
        return []
    out: list[dict[str, Any]] = []
    for clause in re.split(r"\band\b|\bor\b", where.group(1), flags=re.I):
        m = re.search(r"([\w\.`\"\[\]]+)\s*(>=|<=|<>|!=|=|>|<|between|in|like)\s*(.+)", clause.strip(), re.I)
        if not m:
            continue
        column = m.group(1).strip('`"[]').split(".")[-1]
        value = m.group(3).strip()
        out.append(
            {
                "column": column,
                "op": m.group(2).upper(),
                "value": value[:80],
                "kind": "temporal" if re.search(r"['\"]?\d{4}[-/]\d{1,2}", value) else "value",
            }
        )
    return out[:12]


def validate_join_paths(sql: str, plan: GroundedPlan, *, db=None) -> list[dict[str, Any]]:
    """Every join in the SQL, marked validated or not against the catalog.

    Validated = the catalog holds a relation (FK or discovered) between the two
    tables. A join the catalog cannot vouch for is recorded, not refused: it may
    still be legitimate (a shared code column), but a constant-zero aggregate
    that crosses it is not evidence of anything.
    """
    if not sql:
        return []
    pairs = _sql_join_pairs(sql)
    if not pairs:
        return []
    known: set[frozenset[str]] = set()
    if db is not None and plan.source_ids:
        try:
            from app.models.knowledge_catalog import KBTableMeta, KBTableRelation

            names = {
                t.id: t.table_name
                for t in db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == plan.source_ids[0])
                .all()
            }
            for rel in db.query(KBTableRelation).filter(KBTableRelation.kb_id == plan.source_ids[0]).all():
                left = names.get(rel.source_table_meta_id)
                right = names.get(rel.target_table_meta_id)
                if left and right:
                    known.add(frozenset((left, right)))
        except Exception:
            known = set()
    out: list[dict[str, Any]] = []
    for left, right, on in pairs:
        # The engine's own catalog objects are not tables of this source: a join
        # between information_schema.columns and pg_namespace is internal
        # plumbing, and recording it as an unvalidated business join produced the
        # false "join path 'columns' is neither selected nor authorized" warning.
        if is_metadata_object(left) or is_metadata_object(right):
            continue
        validated = frozenset((left, right)) in known
        out.append(
            {
                "left": left,
                "right": right,
                "left_column": on[0] if on else "",
                "right_column": on[1] if on else "",
                "validated": validated,
                "evidence": (
                    "catalog relation"
                    if validated
                    else "no catalog relation between these tables"
                ),
            }
        )
    return out


def _sql_join_pairs(sql: str) -> list[tuple[str, str, tuple[str, str] | tuple]]:
    """(left_table, right_table, (left_col, right_col)) for each JOIN.

    sqlglot when it can parse (it understands aliases, which a regex does not),
    with a regex fallback so a parse failure degrades the evidence instead of
    losing the plan.
    """
    try:
        import sqlglot
        from sqlglot import exp

        from app.services.db.schema_graph import _map_dialect

        parse_dialect = _map_dialect(None)
        tree = sqlglot.parse_one(sql, read=parse_dialect)
        if tree is None:
            raise ValueError("no tree")
        alias: dict[str, str] = {}
        base = ""
        for table in tree.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            alias[(table.alias or name).lower()] = name
            if not base:
                base = name
        pairs: list[tuple[str, str, tuple[str, str] | tuple]] = []
        for join in tree.find_all(exp.Join):
            target = join.this
            right = ""
            if isinstance(target, exp.Table):
                right = target.name
            elif hasattr(target, "name"):
                right = getattr(target, "name", "") or ""
            left = base
            on = join.args.get("on")
            cols: tuple[str, str] | tuple = ()
            if on is not None:
                eq = on.find(exp.EQ)
                if eq is not None:
                    lhs = eq.left.name if hasattr(eq.left, "name") else str(eq.left)
                    rhs = eq.right.name if hasattr(eq.right, "name") else str(eq.right)
                    ltab = ""
                    for side in (eq.left, eq.right):
                        if isinstance(side, exp.Column):
                            ltab = alias.get((side.table or "").lower(), "")
                            if ltab:
                                break
                    if ltab:
                        left = ltab
                    cols = (lhs, rhs)
            if right:
                pairs.append((left, right, cols))
        if pairs:
            return pairs
    except Exception:
        pass

    tables = [t.strip('`"[]').split(".")[-1] for t in _FROM_TABLE.findall(sql)]
    out = []
    for i in range(1, len(tables)):
        out.append((tables[0], tables[i], ()))
    return out



# ── ratio metrics ──────────────────────────────────────────────────────────


class _MetricTerm:
    """A plan metric entry read as the metric-contract term it came from."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def __getattr__(self, name: str) -> Any:
        return self._payload.get(name)


def ratio_contracts_from_metrics(
    metrics: Sequence[Mapping[str, Any]], units: Mapping[str, str] | None = None
) -> list[Any]:
    """Ratio contracts DECLARED by the metric vocabulary.

    Never invented here: a contract is read from the metric's own
    ``sql_expression`` / ``definition`` / ``bindings`` (the same vocabulary that
    already supplies ``plan.metrics``), so the ratio system extends the metric
    contract system instead of shadowing it.
    """
    out: list[Any] = []
    for metric in metrics or []:
        contract = ratio_metrics.contract_from_term(_MetricTerm(metric), units=units)
        if contract is None:
            continue
        if ratio_metrics.validate_contract(contract):
            continue
        out.append(contract)
    return out


def ratio_directive(plan: GroundedPlan) -> str:
    """Prompt text handed to the SQL model BEFORE it writes a ratio (pre-SQL).

    The measured defect was arithmetic, so the fix is a stated requirement: both
    sides of a contracted ratio are measured the same way, under one window.
    """
    contracts = [ratio_metrics.RatioContract.from_dict(c) for c in plan.ratio_contracts]
    if not contracts:
        return ""
    lines = [
        "[Ratio metrics] These figures are ratios. Compute both sides on the SAME basis:"
    ]
    for contract in contracts:
        lines.append(f"  - {contract.metric_id} = ({contract.numerator}) / ({contract.denominator})")
        lines.append(
            f"    numerator is {contract.numerator_grain}; "
            f"denominator is {contract.denominator_grain}; "
            f"the division is only valid at grain {contract.required_grain or 'the common grain of both sides'}"
        )
        if contract.allowed_dimensions:
            lines.append("    defined over: " + ", ".join(contract.allowed_dimensions))
        lines.append(f"    a zero denominator means: {contract.zero_denominator}")
    lines.append(
        "Apply ONE date filter to both sides, and NEVER divide a summed quantity by a "
        "count of records (or a count by a quantity) — that is not a percentage."
    )
    return "\n".join(lines)


def _ratio_carriers(entry: Mapping[str, Any]) -> list[str]:
    carriers = [str(c) for c in (entry.get("carriers") or [])]
    carriers += [str(c) for c in (entry.get("columns") or [])]
    return carriers


def _matching_contract(
    candidate: Mapping[str, Any],
    declared: Mapping[str, Any],
    plan: GroundedPlan,
    units: Mapping[str, str] | None = None,
) -> tuple[Any, str]:
    """``(contract, label)`` for a ratio column — a declared one when it exists.

    A declared contract is RECONCILED with the SQL before it is used: the metric
    says how the ratio is defined, the SQL says what was computed, and it is the
    computed arithmetic that gets checked.
    """
    column = str(candidate.get("column") or "")
    lowered = column.lower()
    for metric_id, contract in declared.items():
        if metric_id and metric_id.lower() in lowered:
            return _reconciled_contract(contract, candidate, units), metric_id
    for entry in plan.evidence:
        label = str(entry.get("label") or "")
        if not label:
            continue
        for metric_id, contract in declared.items():
            if metric_id and metric_id in label:
                return _reconciled_contract(contract, candidate, units), label
    # same sides, different spelling (the metric declared the expression verbatim)
    sides = {_norm_sql(candidate.get("numerator")), _norm_sql(candidate.get("denominator"))}
    for metric_id, contract in declared.items():
        if {_norm_sql(contract.numerator), _norm_sql(contract.denominator)} == sides:
            return _reconciled_contract(contract, candidate, units), metric_id
    return (
        ratio_metrics.contract_from_expressions(
            column or "ratio",
            str(candidate.get("numerator") or ""),
            str(candidate.get("denominator") or ""),
            units=units,
            source_ids=plan.source_ids,
        ),
        column or "ratio",
    )


def _reconciled_contract(
    declared: Any, candidate: Mapping[str, Any], units: Mapping[str, str] | None = None
) -> Any:
    """The metric contract with the SQL's ACTUAL sides put in.

    A contract states the basis a ratio is defined on; the SQL states what was
    computed. When the two disagree, the arithmetic is what gets checked — and the
    declared POLICY is carried over (unit, range, dimensions, zero behaviour,
    required grain) so the value is still judged against what the metric says it
    may be. This is the seam the measured ≈17,957% came through: the metric was
    defined as quantity/quantity, the SQL wrote quantity/count.
    """
    computed = ratio_metrics.contract_from_expressions(
        declared.metric_id,
        str(candidate.get("numerator") or ""),
        str(candidate.get("denominator") or ""),
        units=units,
        source_ids=declared.source_ids,
    )
    merged = dict(computed.to_dict())
    merged.update(
        {
            "output_unit": declared.output_unit,
            "valid_range": list(declared.valid_range) if declared.valid_range else None,
            "date_scope": declared.date_scope,
            "allowed_dimensions": list(declared.allowed_dimensions),
            "zero_denominator": declared.zero_denominator,
            "required_grain": (
                declared.required_grain
                if declared.required_grain != ratio_metrics.GRAIN_UNKNOWN
                else computed.required_grain
            ),
            "tier": "declared",
            "evidence": list(computed.evidence)
            + [f"metric contract defines it as ({declared.numerator}) / ({declared.denominator})"],
        }
    )
    return ratio_metrics.RatioContract.from_dict(merged)


def _norm_sql(text: Any) -> str:
    return re.sub(r"[\s`\"\[\]]+", "", str(text or "")).lower()


def _side_value(rows: Sequence[Mapping[str, Any]], expressions: Mapping[str, str], side: str) -> Any:
    """The value of a ratio side when the query returned it as its own column.

    A column whose own expression IS the ratio is skipped: it contains the text of
    both sides, so matching on it would read the ratio as its own denominator and
    report a healthy ratio as a zero-denominator one.
    """
    target = _norm_sql(side)
    if not target:
        return None
    for column, expression in (expressions or {}).items():
        if ratio_metrics.split_ratio(str(expression or "")) is not None:
            continue
        if target in _norm_sql(expression):
            for row in rows:
                if isinstance(row, Mapping) and column in row:
                    return row.get(column)
    return None


def validate_ratio_columns(
    plan: GroundedPlan,
    sql: str | None,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    units: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Every ratio-shaped result column, checked against its contract.

    A ratio is a claim that two measurements are commensurable; this is where the
    claim is tested (grain, units, window, denominator, range, grouping) and the
    verdict recorded with its evidence. Nothing here clamps a value or invents a
    contract: an unprovable ratio is reported as unverified.
    """
    declarations = {
        str(c.get("metric_id") or ""): ratio_metrics.RatioContract.from_dict(c)
        for c in plan.ratio_contracts
    }
    declarations.pop("", None)
    expressions = sql_alias_expressions(sql)
    window = _window_tuple(plan)
    verdicts: list[dict[str, Any]] = []
    for candidate in ratio_metrics.ratios_in_sql(sql):
        column = str(candidate.get("column") or "")
        contract, label = _matching_contract(candidate, declarations, plan, units)
        if not column:
            continue
        verdict = ratio_metrics.validate_ratio(
            contract,
            value=next((r.get(column) for r in rows if isinstance(r, Mapping) and column in r), None),
            numerator_value=_side_value(rows, expressions, contract.numerator),
            denominator_value=_side_value(rows, expressions, contract.denominator),
            sql=sql,
            groupings=list(plan.groupings or plan.grain),
            source_ids=[str(s) for s in plan.source_ids],
            plan_sources=[str(s) for s in plan.source_ids],
            requested_window=window is not None,
        )
        if verdict["contract"].get("tier") != "declared":
            # No metric contract declared this ratio, so its name is the bare
            # column. A slot the plan DID resolve carries the business wording
            # (履约率, "fulfilment rate") — the reader gets told in their language.
            for entry in plan.evidence:
                entry_label = str(entry.get("label") or "")
                if not entry_label or not _RATIO_CUE.search(entry_label):
                    continue
                if column in _carried_columns(entry, rows, expressions):
                    label = entry_label
                    break
        verdict["column"] = column
        verdict["metric"] = label or verdict.get("metric") or column
        verdict["unit"] = contract.output_unit
        verdicts.append(verdict)
    return verdicts


def ratio_disclosures(verdicts: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """``(warnings, unsupported slots)`` for the ratios that could not be verified."""
    warnings: list[str] = []
    slots: list[dict[str, Any]] = []
    for verdict in verdicts:
        if verdict.get("ok"):
            continue
        label = str(verdict.get("metric") or verdict.get("column") or "the ratio")
        text = ratio_metrics.ratio_disclosure(verdict)
        if text:
            warnings.append(text)
        # A value outside the expected band is SUSPECT, not invalid: requirement is
        # to warn and require evidence, never to withhold a figure that may be
        # legitimate (a coverage index, a cumulative factor). Only a defect in the
        # arithmetic itself withholds the ratio.
        if set(verdict.get("reasons") or []) <= ratio_metrics.WARNING_ONLY_REASONS:
            continue
        slots.append(
            {
                "slot": label,
                "label": label,
                "kind": "metric",
                "reason": "ratio_unverified",
                "detail": text or UNSUPPORTED_REASONS["ratio_unverified"],
                "tables": [],
                "columns": [str(verdict.get("column") or "")] if verdict.get("column") else [],
                "checks": dict(verdict.get("checks") or {}),
                "reasons": list(verdict.get("reasons") or []),
            }
        )
    return warnings, slots


# ── the result set ─────────────────────────────────────────────────────────


def _count_column(columns: Sequence[str], sql: str | None) -> str:
    """The result column that holds a row count, if the query returned one."""
    expressions = sql_alias_expressions(sql)
    for name in columns:
        expr = str(expressions.get(str(name).lower()) or name)
        if re.match(r"\s*count\s*\(", expr, re.IGNORECASE):
            return str(name)
    for name in columns:
        if str(name).strip().strip('`"').lower() in _COUNT_COLUMN_NAMES:
            return str(name)
    return ""


def canonical_count(
    plan: GroundedPlan,
    sql: str | None,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The row-count answer, when the question asks for a count and the query is one.

    "How many rows are in this table" is a supported question with a canonical
    answer. Treating it as a business metric is what produced a disclosure on a
    correct ``COUNT(*) = 26544``: the phrase names no measurement, and the count
    the query returned IS the figure asked for.

    Returns ``{}`` — no claim — when the question does not ask for a count, when
    the query returned no count column, or when there are no rows to read.
    """
    asked = requests_row_count(plan.question) or any(
        str(u.get("kind") or "") == "generic_count" for u in plan.non_metric_units
    )
    if not asked:
        return {}
    column = _count_column(columns, sql)
    if not column:
        return {}
    if not rows:
        return {}
    value = next(
        (row.get(column) for row in rows if isinstance(row, Mapping) and column in row),
        None,
    )
    if value is None:
        return {}
    unit = next(
        (
            str(u.get("unit") or "")
            for u in plan.non_metric_units
            if str(u.get("kind") or "") == "generic_count"
        ),
        "",
    ) or count_request_phrase(plan.question)
    return {"column": column, "value": value, "unit": unit, "kind": "row_count"}


def build_canonical_result_set(  # noqa: PLR0913 - a result envelope is wide by nature
    *,
    plan: GroundedPlan,
    exec_result: Mapping[str, Any] | None,
    sql: str | None,
    schema_info: Mapping[str, Any] | None = None,
    catalog: Sequence[Mapping[str, Any]] | None = None,
    contract: Any = None,
    provenance: Mapping[str, Any] | None = None,
    executed_at: str | None = None,
) -> CanonicalResultSet:
    """Wrap the executor's result into the canonical shape, with its verdicts."""
    exec_result = exec_result or {}
    rows = list(exec_result.get("rows") or [])
    columns = list(rows[0].keys()) if rows and isinstance(rows[0], Mapping) else []
    truncated = bool(exec_result.get("truncated")) or bool(
        exec_result.get("row_cap") and len(rows) >= int(exec_result.get("row_cap") or 0)
    )

    tables = list(plan.tables)
    # The window of a table can come from either artifact: the DDL slice the
    # model was given (`schema_info`) or the catalog snapshot the retrieval ran
    # over (`catalog`). Measured live, the selected-view slice carried no
    # min/max, so every coverage state came back `unknown` and freshness with it
    # — the two fields the reader needs to tell a stale window from a current
    # one. Prefer whichever entry actually has a window.
    catalog_windows: dict[str, Mapping[str, Any]] = {}
    for source in (catalog or [], (schema_info or {}).get("tables") or []):
        for entry in source:
            if not isinstance(entry, Mapping):
                continue
            name = _table_of(entry)
            if not name:
                continue
            prior = catalog_windows.get(name)
            if prior is None or (not prior.get("max_date") and entry.get("max_date")):
                catalog_windows[name] = entry
    available: dict[str, Any] = {}
    for table in tables:
        entry = catalog_windows.get(table) or {}
        state, cov = coverage_state(entry, _window_tuple(plan))
        cov["state"] = state
        available[table] = cov

    units: dict[str, str] = {}
    for column, unit in _units_by_column(schema_info).items():
        if column in columns and unit:
            units[column] = unit

    warnings: list[str] = list(plan.warnings)
    if truncated:
        warnings.append("the result hit the row cap, so it covers only part of the data")

    # Ratio metrics: a ratio is a claim that two measurements share a basis, so
    # it is checked here (grain, units, window, denominator, range, grouping) and
    # an unprovable one becomes an unsupported slot rather than a confident
    # percentage. The measured 17,957% fulfilment rate came through this seam.
    ratio_verdicts = validate_ratio_columns(plan, sql, columns, rows, units=units)
    unsupported, zero_warnings = _slots_against_result(
        plan, sql, columns, rows, contract, truncated, available,
        permitted_ratios={str(v.get("column") or "") for v in ratio_verdicts if v.get("ok")},
    )
    warnings.extend(zero_warnings)
    ratio_warnings, ratio_slots = ratio_disclosures(ratio_verdicts)
    warnings.extend(ratio_warnings)
    _claimed = {str(s.get("slot") or "") for s in unsupported}
    unsupported.extend(s for s in ratio_slots if str(s.get("slot") or "") not in _claimed)

    # "How many rows" is a question with a canonical answer, not a business
    # metric the source failed to produce: when the question asks for a count and
    # the query returned one, that is the supported answer to it.
    count = canonical_count(plan, sql, columns, rows)

    crs = CanonicalResultSet(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        complete=not unsupported,
        source_ids=list(plan.source_ids),
        sql_fingerprint=_fingerprint_sql(sql),
        requested_coverage=_requested_coverage(plan),
        available_coverage=available,
        freshness=freshness_of(*available.values(), window=_window_tuple(plan)),
        units=units,
        warnings=warnings,
        provenance=dict(provenance or {}),
        executed_at=executed_at or None or "",
        validation={
            "answer_contract": (
                {
                    "ok": bool(getattr(contract, "ok", True)),
                    "violations": list(getattr(contract, "violations", []) or []),
                    "detail": dict(getattr(contract, "detail", {}) or {}),
                }
                if contract is not None
                else {"ok": True, "violations": []}
            ),
            "plan": plan.validate(),
            "truncated": truncated,
        },
        unsupported_slots=unsupported,
        ratio_validations=ratio_verdicts,
        canonical_count=count,
        disclosure=disclose(unsupported),
        plan_fingerprint=plan.fingerprint(),
    )
    if not crs.executed_at:
        from app.services.db.contracts import _now_iso

        crs.executed_at = _now_iso()
    return crs


def _window_tuple(plan: GroundedPlan) -> tuple | None:
    scope = plan.temporal_scope or {}
    if not scope.get("start") or scope.get("kind") == "none":
        return None
    return (scope.get("start"), scope.get("end"))


def _requested_coverage(plan: GroundedPlan) -> dict[str, Any] | None:
    window = _window_tuple(plan)
    if not window:
        return None
    return {"start": str(window[0]), "end": str(window[1])}


def _fingerprint_sql(sql: str | None) -> str:
    import hashlib

    normalised = " ".join((sql or "").split())
    if not normalised:
        return ""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def _table_of(entry: Mapping[str, Any]) -> str:
    """The table name of a schema_info / catalog entry, under either key."""
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("table_name") or entry.get("table") or "")


def _units_by_column(schema_info: Mapping[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for table in (schema_info or {}).get("tables") or []:
        for col in (table.get("columns") or []):
            if not isinstance(col, Mapping):
                continue
            name = col.get("name") or col.get("column_name")
            unit = col.get("unit")
            if name and unit:
                out[str(name)] = str(unit)
    return out


# ── unsupported slots vs the actual result ─────────────────────────────────


def _slots_against_result(
    plan: GroundedPlan,
    sql: str | None,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    contract: Any,
    truncated: bool,
    coverage: Mapping[str, Mapping[str, Any]] | None = None,
    permitted_ratios: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Re-check every requested slot against what the query returned.

    Returns ``(unsupported_slots, warnings)``. The plan-time list survives (those
    slots were never selectable); this adds the result-time reasons.

    A constant zero is NOT automatically a false figure — it is a figure whose
    support has to be shown. The catalog can show it: when every table that
    carries the slot is ``covered`` for the requested window, the zero is data,
    it is reported, and the fact is attached as a caveat instead. Everything
    else (a value crossing an unknown join, unknown coverage, a window outside
    the data) is disclosed as unsupported rather than emitted as ``0``.

    Missing ≠ empty on the other side too: absent coverage is ``unknown``, which
    convicts nothing on its own — the join evidence is what convicts here.
    """
    coverage = coverage or {}
    from app.services.db.lexical import tokens

    out: list[dict[str, Any]] = [dict(s) for s in plan.unsupported_slots]
    already = {s.get("slot") for s in out}
    warnings: list[str] = list(_ratio_sanity_warnings(plan, rows, sql, permitted=permitted_ratios))

    # A structure/listing query reads the engine's OWN objects. Its rows are not
    # business data, so they can neither support nor convict a business metric:
    # convicting here is what produced "the data source has nothing for
    # 订单/金额相关字段" on a question that asked for tables and columns. The
    # plan-time slots survive (those were never selectable, whatever the SQL was).
    if sql_is_introspection(sql):
        return out, warnings

    # Which tables the SQL actually touched, and did it join them on a key the
    # catalog cannot vouch for?
    sql_tables = {str(t).split(".")[-1] for t in _FROM_TABLE.findall(sql or "")}
    suspect_join = [p for p in plan.join_paths if not p.get("validated")]
    expressions = sql_alias_expressions(sql)

    for entry in plan.evidence:
        slot = entry.get("slot")
        if not slot or slot in already:
            continue
        # An evidence entry with no kind predates the classification (a plan
        # serialized by an older writer, or a hand-built one) and is treated as a
        # metric, which is what every slot used to be.
        if entry.get("kind", "metric") != "metric":
            continue
        carriers = [
            c for c in columns
            if _carries(slot, str(c), _carrier_pool(entry), expressions)
        ]
        if not carriers:
            out.append(
                {
                    "slot": slot,
                    "label": entry.get("label") or slot,
                    "reason": "not_in_result",
                    "detail": "the query returned no column for this",
                    "tables": entry.get("tables") or [],
                    "columns": entry.get("columns") or [],
                }
            )
            already.add(slot)
            continue

        # The column exists. Is its value provably data, or a silent zero?
        zero_like = [
            c for c in carriers
            if not rows or _constant_zero(rows, str(c))
        ]
        if len(zero_like) != len(carriers):
            continue
        slot_tables = {str(t).split(".")[-1] for t in (entry.get("tables") or [])}
        label = entry.get("label") or slot
        if suspect_join and (
            not slot_tables or slot_tables & sql_tables or _touches(sql_tables, suspect_join)
        ):
            out.append(
                {
                    "slot": slot,
                    "label": label,
                    "reason": "unvalidated_join",
                    "detail": _join_detail(suspect_join),
                    "tables": entry.get("tables") or [],
                    "columns": entry.get("columns") or [],
                }
            )
        else:
            entries = [coverage.get(t) or {} for t in slot_tables]
            states = {str(e.get("state") or "unknown") for e in entries}
            # `not_requested` (no period was asked about, and the catalog knows
            # this table's window) means the zero is simply what the query
            # returned. `unknown` — a period WAS asked and the carrier's window
            # is unmeasured — proves nothing, so it does not rescue the zero.
            if states and states <= {"covered", "partially_covered", "not_requested"}:
                # The catalog vouches for the window: the zero is a measurement.
                # Report it — and say that it is a real zero, so a reader does
                # not have to wonder whether it is a missing-value artifact.
                warnings.append(
                    f"{label} is zero across every returned row; the data source "
                    "covers the period asked about, so this is a real figure"
                )
                continue
            out.append(
                {
                    "slot": slot,
                    "label": label,
                    "reason": "zero_unproven",
                    "detail": (
                        "every returned value is zero or null and the catalog does not "
                        "show the tables that carry it hold rows for this period"
                    ),
                    "tables": entry.get("tables") or [],
                    "columns": entry.get("columns") or [],
                }
            )
        already.add(slot)

    if truncated:
        for entry in plan.evidence:
            slot = entry.get("slot")
            if not slot or slot in already:
                continue
            if entry.get("kind", "metric") != "metric":
                continue
            out.append(
                {
                    "slot": slot,
                    "label": entry.get("label") or slot,
                    "reason": "truncated",
                    "detail": UNSUPPORTED_REASONS["truncated"],
                    "tables": entry.get("tables") or [],
                    "columns": entry.get("columns") or [],
                }
            )
            already.add(slot)
    return out, warnings


def _touches(sql_tables: set[str], suspect: Sequence[Mapping[str, Any]]) -> bool:
    for path in suspect:
        if {str(path.get("left")), str(path.get("right"))} <= sql_tables:
            return True
    return False


def _join_detail(suspect: Sequence[Mapping[str, Any]]) -> str:
    names = [f"{p.get('left')} ↔ {p.get('right')}" for p in suspect[:3]]
    return "the value crosses a join the catalog cannot vouch for: " + ", ".join(names)


#: A ratio-shaped question word. Shape only — the platform's convention for
#: patterns that are structural rather than business vocabulary (same class as
#: id/flag/date/type detection), and it never selects a column by itself.
_RATIO_CUE = re.compile(r"率|比|percent|ratio|rate|margin", re.I)


def _ratio_sanity_warnings(
    plan: GroundedPlan,
    rows: Sequence[Mapping[str, Any]],
    sql: str | None = None,
    permitted: Iterable[str] = (),
) -> list[str]:
    """A percentage above 100% is not an unsupported value — it is a suspect one.

    Measured live on Q3: the model wrote ``SUM(out_fqty) / COUNT(DISTINCT FID)``
    as the fulfilment rate — a quantity divided by a COUNT — and the answer came
    back 17,957%. Every guard passed it: the SQL ran, the column existed, the
    value was non-zero. The figure is reported (it is what the data returned) and
    the reader is told it needs checking, which is the honest treatment for a
    number that cannot be right.
    """
    out: list[str] = []
    expressions = sql_alias_expressions(sql)
    # A ratio whose contract DECLARES the range it may occupy (or whose unit is
    # not a percentage) is not a suspect figure — see db/ratio_contract.py. Without
    # this, a legitimately large ratio would be caveated forever.
    allowed = {str(c) for c in permitted if c}
    # Only DIVISION-shaped result columns can be a percentage. Without this,
    # every column fed by the same source column counts too — and SUM(out_fqty)
    # as `shipped_qty` (31,605) turned a sane 89% rate into a false alarm.
    ratio_columns = {c for c, expr in expressions.items() if "/" in expr}
    if not ratio_columns:
        return out
    for entry in plan.evidence:
        label = str(entry.get("label") or "")
        if not label or not _RATIO_CUE.search(label):
            continue
        for column in [
            c
            for c in _carried_columns(entry, rows, expressions)
            if c in ratio_columns and c not in allowed
        ]:
            values = [r.get(column) for r in rows if r.get(column) is not None]
            if not values:
                continue
            try:
                worst = max(float(v) for v in values)
            except (TypeError, ValueError):
                continue
            if worst > 1.0:
                out.append(
                    f"{label} is reported as {worst:.4g} — over 100%, so the "
                    "numerator and denominator may not be in the same unit; "
                    "treat the figure as unverified"
                )
                break
    return out[:3]


def _carried_columns(
    entry: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    expressions: Mapping[str, str] | None = None,
) -> list[str]:
    """The result columns that carry this slot (same rule the zero check uses)."""
    if not rows or not isinstance(rows[0], Mapping):
        return []
    return [
        str(c) for c in rows[0].keys()
        if _carries(
            str(entry.get("slot") or ""), str(c), _carrier_pool(entry), expressions
        )
    ]


def _carrier_pool(entry: Mapping[str, Any]) -> list[str]:
    """Every catalog column that could carry this slot: the cut list AND the
    wider set that only had to clear the match floor."""
    pool = list(entry.get("columns") or [])
    for extra in entry.get("carriers") or []:
        if extra not in pool:
            pool.append(extra)
    return pool


def _constant_zero(rows: Sequence[Mapping[str, Any]], column: str) -> bool:
    seen = False
    for row in rows:
        value = row.get(column)
        if value is None:
            continue
        try:
            if float(value) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
        seen = True
    return True


#: ``<expression> AS <alias>``. The expression class is deliberately broad (no
#: ``;``) so a CASE/CAST/arithmetic expression is captured whole — a narrow
#: character class silently truncated ``CASE WHEN … END AS fulfillment_rate`` to
#: the text after the ``=``, which broke the one link this map exists for.
_ALIASED = re.compile(r"([^;]+?)\s+AS\s+([\w\u4e00-\u9fff]+)", re.I)


def sql_alias_expressions(sql: str | None) -> dict[str, str]:
    """``{result_column: source_expression}`` from the SELECT list.

    The link between a catalog column and a result column is usually the SQL
    itself: ``SUM(out_fqty) AS shipped_qty``. Without this map, a slot named in
    Chinese and returned under an English alias look unrelated, and the
    constant-zero check never fires on the case it exists for.
    """
    out: dict[str, str] = {}
    body = re.split(r"\bfrom\b", sql or "", maxsplit=1, flags=re.I)[0]
    body = re.sub(r"^\s*select\s+(?:distinct\s+)?", "", body, flags=re.I)
    for expr, alias in _ALIASED.findall(body):
        expr = expr.strip().strip(",").strip()
        if expr and alias:
            out[alias.lower()] = expr
    return out


def _carries(
    slot: str, column: str, concepts: Sequence[str], expressions: Mapping[str, str] | None = None
) -> bool:
    """Does ``column`` carry ``slot``?

    Three independent signals, any one of which is enough:

    1. the column's identifier shares vocabulary with the slot;
    2. the column is one of the catalog columns that matched the slot
       (``table.column``), by name;
    3. the column's SELECT expression references one of those catalog columns
       (``SUM(out_fqty) AS shipped_qty``) — the alias case.
    """
    from app.services.db.lexical import identifier_terms, tokens

    slot_tokens = set(tokens(slot)) | set(identifier_terms(slot))
    col_tokens = set(tokens(column)) | set(identifier_terms(column))
    if slot_tokens & col_tokens:
        return True
    expr = (expressions or {}).get(str(column).lower(), "")
    for concept in concepts:
        name = str(concept).split(".")[-1]
        if not name:
            continue
        if name.lower() == str(column).lower():
            return True
        if expr and re.search(rf"\b{re.escape(name)}\b", expr, re.I):
            return True
    return False


# ── disclosure + narration ─────────────────────────────────────────────────


def disclose(slots: Sequence[Mapping[str, Any]]) -> str:
    """One business-language sentence for the unsupported slots ("" when none)."""
    if not slots:
        return ""
    labels = [str(s.get("label") or s.get("slot") or "") for s in slots]
    labels = [l for l in labels if l]
    if not labels:
        return ""
    reasons = {str(s.get("reason") or "") for s in slots}
    if reasons == {"truncated"}:
        return (
            "The result was cut off at the row limit, so the figures for "
            + ", ".join(labels)
            + " cover only part of the data."
        )
    if "ratio_unverified" in reasons:
        # The verdict already carries the reader-facing sentence (which names the
        # actual defect: a quantity divided by a count, a unit clash, a window
        # mismatch). Only fall back to generic wording if it is absent.
        ratio_slots = [s for s in slots if str(s.get("reason") or "") == "ratio_unverified"]
        details = [str(s.get("detail") or "") for s in ratio_slots if s.get("detail")]
        if details and len(details) == len(ratio_slots):
            return " ".join(details)
        return (
            "The ratio "
            + ", ".join(labels)
            + " could not be verified against this data, so no figure is reported for it. "
            "It is not zero — it is unknown."
        )
    if reasons & {"unvalidated_join", "zero_unproven"}:
        return (
            "For this period the data behind "
            + ", ".join(labels)
            + " could not be resolved, so no figure is reported for it. "
            "It is not zero — it is unknown."
        )
    if reasons == {"blocked_by_policy"}:
        return (
            "Your access does not cover " + ", ".join(labels) + ", so no figure is reported for it."
        )
    return (
        "The data source used here has nothing for "
        + ", ".join(labels)
        + ", so no figure is reported for it. It is not zero — it is unknown."
    )


def unsupported_directive(plan: GroundedPlan) -> str:
    """Prompt text handed to the SQL model when something cannot be supported."""
    if not plan.unsupported_slots:
        return ""
    lines = [
        "[Unsupported] These parts of the question have no supporting column in the "
        "tables selected for this query:"
    ]
    for slot in plan.unsupported_slots:
        lines.append(f"  - {slot.get('label') or slot.get('slot')}: {slot.get('detail') or ''}")
    lines.append(
        "Do NOT invent a column, table or expression for them and do NOT return a "
        "literal 0 for them. Omit them from the query; the answer will say they are "
        "unavailable."
    )
    return "\n".join(lines)


def narrate_directive(
    crs: CanonicalResultSet, supplementary: list | None = None
) -> str:
    """The rules the narrator must obey for THIS result set.

    ``supplementary`` (2026-09-22): verified snapshots from entity-sibling
    tables the primary SQL never touched (see services/db/coverage.py).
    When present, the narrator MUST cite them too — excluding them is how
    the answer reported a partial entity set as complete (25 vs 215
    contracts)."""
    lines = [
        "[Grounding] Report only the figures present in the rows below"
        + (" and in the RELATED-TABLES block." if supplementary else "."),
        "- Never restate, adjust, average, extrapolate or infer a number that is not in them.",
    ]
    if supplementary:
        labels = ", ".join(
            str(s.get("table")) for s in supplementary[:3]
        )
        lines.append(
            f"- The RELATED-TABLES block holds VERIFIED results from sibling "
            f"tables ({labels}) that the primary query did not cover: cite "
            f"their figures in the answer, labeled by table — the primary "
            f"rows alone are a PARTIAL entity set."
        )
    if crs.unsupported_slots:
        labels = ", ".join(crs.unsupported_labels())
        lines.append(
            f"- The following were asked for but are NOT available: {labels}. "
            "Say plainly that they cannot be determined from this data, and never "
            "write 0, 0%, none or a null-derived value for them."
        )
    if not crs.complete:
        lines.append("- The result is incomplete: name what is missing.")
    if crs.freshness.get("state") == "stale":
        lines.append(
            f"- The data covers up to {crs.freshness.get('as_of')}; say so if the "
            "question asks for a more recent period."
        )
    if crs.warnings:
        lines.append("- Include this caveat: " + "; ".join(crs.warnings[:3]))
    return "\n".join(lines)


def check_narrative_against_result(narrative: str, crs: CanonicalResultSet) -> list[str]:
    """Deterministic: does the prose claim a figure for an unsupported slot?

    Conservative on purpose — a number only counts as a claim when it sits next
    to the slot's own label, so a legitimate figure elsewhere in the prose is
    never mistaken for one.
    """
    text = narrative or ""
    if not text or not crs.unsupported_slots:
        return []
    violations: list[str] = []
    for slot in crs.unsupported_slots:
        # A slot names its business wording AND the columns that would have
        # carried it, so prose that uses the raw alias ("fulfilment_rate: 17")
        # is a claim about the slot just as much as prose using the label.
        names = {str(slot.get("label") or ""), str(slot.get("slot") or "")}
        names |= {str(c).split(".")[-1] for c in (slot.get("columns") or [])}
        for label in names:
            if not label:
                continue
            for match in re.finditer(re.escape(label), text):
                tail = text[match.end(): match.end() + CLAIM_WINDOW]
                cut = min(
                    (tail.find(ch) for ch in _SENTENCE_END if tail.find(ch) != -1),
                    default=len(tail),
                )
                window = tail[:cut]
                if _NUMBER.search(window):
                    violations.append(f"{label}: {(label + window).strip()[:80]}")
                    break
            if violations and violations[-1].startswith(label):
                break
    return violations


def rewrite_instruction(violations: Sequence[str], crs: CanonicalResultSet) -> str:
    """What to tell the narrator when it emitted an unsupported figure."""
    return (
        "[Correction] The previous answer stated a value for data that is not available: "
        + "; ".join(violations)
        + ". Rewrite the answer using ONLY the rows provided and say plainly that "
        + ", ".join(crs.unsupported_labels())
        + " cannot be determined from this data. Do not use 0 for them."
    )
