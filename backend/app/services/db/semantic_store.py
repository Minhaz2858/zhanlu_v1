"""SemanticStore — one read-only semantic foundation for data questions.

Architecture (adapted from Oracle's "enterprise NL2SQL with semantic
enrichments" — the IDEA, not the product): a single persistent semantic
foundation fed by four inputs, consumed by retrieval / schema linking /
grounding / validation.

    input                       where it already lives in Zhanlu
    ------------------------------------------------------------------
    1 schema info               kb_table_meta / kb_column_meta / kb_table_relation
                                (catalog_indexer)
    2 safe sample data          kb_table_meta.sample_values,
                                kb_column_meta.enum_values / unit,
                                entity_master_hints.sample_categories
    3 query workload            nl2sql_query_logs (table usage, deduced from the
                                SQL that actually ran) + query_plan_memory
    4 domain knowledge          terminologies, metric_definitions,
                                project_catalog_overlay (aliases), project_metric
                                (approved only)

This module is a FACADE. It creates no tables, writes nothing, and duplicates
no existing intelligence. Everything it returns is already persisted somewhere;
the value is that callers ask ONE interface instead of five, and that every
item carries its provenance tier.

Provenance tiers (spec: RUNTIME_INFERENCE_AUTO_PROMOTED_TO_TRUTH = NO)
---------------------------------------------------------------------
    VERIFIED                an human curated this: approved project_metric,
                            terminology/glossary rows, catalog overlay aliases
    DISCOVERED              the indexer read it off the datasource: catalog
                            rows, semantic_type/enum_values/unit, domain names
    LEARNED_FROM_WORKLOAD   observed from real executions: plan memory rows,
                            table usage counts parsed out of logged SQL
    RUNTIME_INFERRED        computed for THIS question (ranking, scores).
                            Never persisted, never promoted — read-only by
                            construction, and no writer exists in this module.

Scope (spec: CROSS_ORG_SEMANTIC_LEAK = 0)
----------------------------------------
Every read is filtered by the ``kb_ids`` handed to the constructor. The store
cannot see a datasource it was not given: retrieval is strictly an
EffectiveSourceScope subset. Workload rows that carry no kb_id (the query log)
are counted ONLY for tables that exist in the in-scope catalog, so a log row
from another warehouse can never promote a table here.

Authorisation is NOT a semantic decision (spec: QUERY_HISTORY_GRANTS_AUTH = NO).
Workload signals may reorder candidates. They never grant access — that stays
with ``access_policy_service``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from sqlalchemy.orm import Session

from app.services.db.lexical import tokens as lexical_tokens

logger = logging.getLogger(__name__)

# ── provenance tiers ───────────────────────────────────────────────────────
TIER_VERIFIED = "VERIFIED"
TIER_DISCOVERED = "DISCOVERED"
TIER_LEARNED = "LEARNED_FROM_WORKLOAD"
TIER_RUNTIME = "RUNTIME_INFERRED"

# Relation names inside a logged SQL statement. Deliberately generic SQL shape,
# no dialect or domain knowledge: FROM/JOIN followed by an identifier.
_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+([`\"\[]?)([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")

# How many log rows a workload scan reads. Bounded: the hot path must never
# scan an unbounded telemetry table.
_LOG_SCAN_DEFAULT = 2000

# A description hit is worth 1.0, but no more than this in total — a description
# is prose, and prose length must not buy rank (see ``rank_relations``).
_W_DESC_CAP = 3.0


def _tokens(text: str | None) -> set[str]:
    """Lowercased alphanumeric/CJK tokens longer than one char.

    Shape-only (no business vocabulary), so a new database needs no new rules.
    CJK text is cut into overlapping bigrams by ``lexical.tokens`` — there is no
    whitespace inside a Chinese run, so without that a whole Chinese question
    came back as ONE token and matched nothing (measured: pure-CJK questions
    retrieved ``method=none``, and the ordering fell to the domain guess).
    """
    return lexical_tokens(text)


def _token_hits(q_tokens: set[str], text: str | None) -> int:
    """Count question tokens a text answers to, tolerating inflection.

    Exact token equality first; then a prefix relation for long tokens, so
    "contracts" in a question still reaches a table called ``contract_header``
    and "orders" reaches ``order_line``. Deliberately crude and symmetric —
    a stemmer would need a language pack, which a multi-lingual warehouse
    cannot assume.
    """
    if not q_tokens or not text:
        return 0
    hits = 0
    for tok in q_tokens:
        for cand in _tokens(text):
            if tok == cand or _prefix_same(tok, cand):
                hits += 1
                break
    return hits


def _prefix_same(a: str, b: str) -> bool:
    if len(a) < 4 or len(b) < 4:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n] and abs(len(a) - len(b)) <= 2


def _matched_question_tokens(q_tokens: set[str], text: str | None) -> list[str]:
    """The question-side tokens that matched, for evidence strings."""
    if not q_tokens or not text:
        return []
    cands = _tokens(text)
    return sorted(
        t for t in q_tokens
        if any(t == c or _prefix_same(t, c) for c in cands)
    )


def _json_list(raw: Any) -> list[str]:
    """Parse a JSON-list column that may also be a comma-separated string."""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x]
        return [p.strip() for p in raw.split(",") if p.strip()]
    return []


def _overlap(q_tokens: set[str], *haystacks: str | None) -> int:
    if not q_tokens:
        return 0
    text = " ".join(h for h in haystacks if h)
    return len(q_tokens & _tokens(text))


# ── value objects ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SemanticRelation:
    """One retrievable relation (table/view) plus the meaning known about it."""

    kb_id: str
    table_name: str
    description: str = ""
    description_alt: str = ""  # the other language variant — matching only
    tier: str = TIER_DISCOVERED
    role: str = "unknown"
    domain: str | None = None
    row_count: int | None = None
    column_count: int = 0
    aliases: tuple[str, ...] = ()
    schema_name: str = ""


@dataclass(frozen=True)
class SemanticColumn:
    table_name: str
    column_name: str
    data_type: str = ""
    description: str = ""
    tier: str = TIER_DISCOVERED
    semantic_type: str = "unknown"
    unit: str | None = None
    enum_values: tuple[Any, ...] = ()
    is_primary_key: bool = False


@dataclass(frozen=True)
class SemanticJoin:
    source_table: str
    target_table: str
    source_columns: tuple[str, ...] = ()
    target_columns: tuple[str, ...] = ()
    relation_type: str = "FK"
    confidence: float = 1.0
    tier: str = TIER_DISCOVERED


@dataclass(frozen=True)
class GlossaryTerm:
    """A business word with a meaning — the schema-linking vocabulary."""

    term: str
    definition: str = ""
    tier: str = TIER_VERIFIED
    origin: str = "terminology"  # terminology | metric | alias
    unit: str | None = None
    sql_expression: str | None = None
    query_pattern: str | None = None
    aliases: tuple[str, ...] = ()
    #: the metric's declared aggregation (sum/avg/count/…) — the grain evidence
    aggregation: str | None = None
    #: the metric's own binding block (numerator/denominator/grain/range/…),
    #: passed through untouched so a ratio contract can be read from it
    bindings: object | None = None


@dataclass(frozen=True)
class WorkloadSignal:
    """How often a relation actually got used — ranking evidence, never truth."""

    table_name: str
    uses: int = 0
    successes: int = 0
    failures: int = 0
    last_used: datetime | None = None
    tier: str = TIER_LEARNED


@dataclass(frozen=True)
class RankedRelation:
    table_name: str
    kb_id: str
    score: float
    evidence: tuple[str, ...] = ()
    tier: str = TIER_RUNTIME


@dataclass(frozen=True)
class SourceCandidate:
    """A bound datasource ranked for one question (cross-source retrieval)."""

    kb_id: str
    name: str
    database_name: str = ""
    db_type: str = ""
    score: float = 0.0
    evidence: tuple[str, ...] = ()
    tier: str = TIER_RUNTIME


# ── the store ──────────────────────────────────────────────────────────────
class SemanticStore:
    """Read-only facade over the persisted semantic foundation.

    Construct with the KB ids the caller is already authorised to read; the
    store is then structurally incapable of returning anything else.
    """

    def __init__(
        self,
        db: Session,
        kb_ids: Sequence[str] | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.db = db
        self.kb_ids: tuple[str, ...] = tuple(k for k in (kb_ids or []) if k)
        self.project_id = project_id
        self.agent_id = agent_id
        self._relations: list[SemanticRelation] | None = None
        self._workload: dict[str, WorkloadSignal] | None = None
        # P0-1 (OCI offline-enrichment parity): mined table_pair (frequent
        # joins) + table_filter (common predicates) from the workload miner —
        # collected since its introduction but never consumed until now.
        self._mined: dict[str, dict] | None = None

    # -- schema info -------------------------------------------------------
    def relations(self, force: bool = False) -> list[SemanticRelation]:
        """Every in-scope relation with its known meaning, alias-resolved."""
        if self._relations is not None and not force:
            return self._relations
        if not self.kb_ids:
            self._relations = []
            return self._relations

        from app.models.knowledge_catalog import (
            KBColumnMeta,
            KBTableMeta,
            ProjectCatalogOverlay,
        )

        rows = (
            self.db.query(KBTableMeta)
            .filter(
                KBTableMeta.kb_id.in_(list(self.kb_ids)),
                KBTableMeta.is_deleted.is_(False),
            )
            .order_by(KBTableMeta.table_name)
            .all()
        )
        if not rows:
            self._relations = []
            return self._relations

        counts: dict[str, int] = {}
        col_rows = (
            self.db.query(KBColumnMeta.table_meta_id)
            .filter(KBColumnMeta.table_meta_id.in_([r.id for r in rows]))
            .all()
        )
        for (tmid,) in col_rows:
            counts[tmid] = counts.get(tmid, 0) + 1

        aliases = self._aliases()

        out: list[SemanticRelation] = []
        for r in rows:
            name = r.table_name
            key = name.lower()
            overlay = aliases.get(key) or {}
            alias_list = tuple(
                a for a in (overlay.get("aliases") or ()) if a
            )
            description = (
                overlay.get("description") or r.description_en or r.description_zh or ""
            )
            # The language variant NOT chosen for display. Matching must span
            # both: the store used to see only ``description_en``, so on a
            # catalog whose Chinese descriptions exist for every table (all 143
            # here) a Chinese question could not match a single one of them.
            description_alt = ""
            if description:
                if description == (r.description_zh or ""):
                    description_alt = r.description_en or ""
                else:
                    description_alt = r.description_zh or ""
            out.append(
                SemanticRelation(
                    kb_id=r.kb_id,
                    table_name=name,
                    description=description,
                    description_alt=description_alt,
                    # A human-authored alias/description is the only thing here
                    # that upgrades a row above DISCOVERED.
                    tier=TIER_VERIFIED if alias_list else TIER_DISCOVERED,
                    role=r.table_role or "unknown",
                    domain=getattr(r, "domain_name", None),
                    row_count=r.row_count,
                    column_count=counts.get(r.id, 0),
                    aliases=alias_list,
                    schema_name=r.schema_name or "",
                )
            )
        self._relations = out
        return out

    def columns(self, table_name: str) -> list[SemanticColumn]:
        """Column-level meaning for one relation (semantic_type/enum/unit)."""
        from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

        if not self.kb_ids or not table_name:
            return []
        tmeta = (
            self.db.query(KBTableMeta.id)
            .filter(
                KBTableMeta.kb_id.in_(list(self.kb_ids)),
                KBTableMeta.table_name == table_name,
                KBTableMeta.is_deleted.is_(False),
            )
            .first()
        )
        if tmeta is None:
            return []
        rows = (
            self.db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id == tmeta[0])
            .order_by(KBColumnMeta.ordinal)
            .all()
        )
        return [
            SemanticColumn(
                table_name=table_name,
                column_name=c.column_name,
                data_type=c.data_type or "",
                description=c.description_en or c.description_zh or "",
                tier=TIER_DISCOVERED,
                semantic_type=getattr(c, "semantic_type", None) or "unknown",
                unit=getattr(c, "unit", None),
                enum_values=tuple(_json_list(getattr(c, "enum_values", None))),
                is_primary_key=bool(c.is_primary_key),
            )
            for c in rows
        ]

    def sample_values(self, table_name: str, limit: int = 20) -> dict[str, Any]:
        """Bounded safe samples already persisted by the indexer.

        Table-level only, and the payload is whatever the indexer stored — this
        method never queries the datasource, so it cannot widen what is stored.
        """
        from app.models.knowledge_catalog import KBTableMeta

        if not self.kb_ids or not table_name:
            return {}
        row = (
            self.db.query(KBTableMeta.sample_values, KBTableMeta.entity_master_hints)
            .filter(
                KBTableMeta.kb_id.in_(list(self.kb_ids)),
                KBTableMeta.table_name == table_name,
                KBTableMeta.is_deleted.is_(False),
            )
            .first()
        )
        if row is None:
            return {}
        samples = row[0] or {}
        hints = row[1] or {}
        out: dict[str, Any] = dict(samples) if isinstance(samples, dict) else {}
        cats = hints.get("sample_categories") if isinstance(hints, dict) else None
        if cats:
            out.setdefault("categories", cats[:limit])
        return out

    def joins(self, table_name: str | None = None) -> list[SemanticJoin]:
        """FK/relationship edges (join-path reachability for retrieval)."""
        from app.models.knowledge_catalog import KBTableMeta, KBTableRelation

        if not self.kb_ids:
            return []
        q = self.db.query(KBTableMeta.id, KBTableMeta.table_name).filter(
            KBTableMeta.kb_id.in_(list(self.kb_ids)),
            KBTableMeta.is_deleted.is_(False),
        )
        name_by_id = {tid: tname for tid, tname in q.all()}
        if not name_by_id:
            return []
        rows = (
            self.db.query(KBTableRelation)
            .filter(
                KBTableRelation.source_table_meta_id.in_(list(name_by_id)),
                KBTableRelation.is_deleted.is_(False),
            )
            .all()
        )
        out: list[SemanticJoin] = []
        for r in rows:
            src = name_by_id.get(r.source_table_meta_id)
            tgt = name_by_id.get(r.target_table_meta_id)
            if not src or not tgt:
                continue
            if table_name and src != table_name and tgt != table_name:
                continue
            out.append(
                SemanticJoin(
                    source_table=src,
                    target_table=tgt,
                    source_columns=tuple(_json_list(r.source_columns)),
                    target_columns=tuple(_json_list(r.target_columns)),
                    relation_type=r.relation_type or "FK",
                    confidence=float(r.confidence or 1.0),
                )
            )
        return out

    def domains(self) -> list[dict[str, Any]]:
        """The named business domains of the in-scope catalog."""
        from app.models.knowledge_catalog import KBDomain

        if not self.kb_ids:
            return []
        rows = (
            self.db.query(KBDomain)
            .filter(
                KBDomain.kb_id.in_(list(self.kb_ids)),
                KBDomain.is_deleted.is_(False),
            )
            .all()
        )
        return [
            {
                "kb_id": d.kb_id,
                "name_en": d.name_en,
                "name_zh": d.name_zh,
                "description": d.description_en or d.description_zh,
                "member_count": d.member_count,
                "tier": TIER_DISCOVERED,
            }
            for d in rows
        ]

    # -- domain knowledge --------------------------------------------------
    def _aliases(self) -> dict[str, dict[str, Any]]:
        """Human-authored table aliases/definitions, keyed by lower table name.

        Overlay rows are project-scoped by design; a row with no kb_id is
        treated as a project-wide alias and only matched inside a project.
        """
        from app.models.knowledge_catalog import ProjectCatalogOverlay

        if not self.kb_ids:
            return {}
        # An overlay row is project-scoped by construction. With no project
        # selected there is no legitimate scope to read, so expose nothing
        # rather than every project's aliases.
        if not self.project_id:
            return {}
        try:
            rows = (
                self.db.query(ProjectCatalogOverlay)
                .filter(
                    ProjectCatalogOverlay.project_id == self.project_id,
                    ProjectCatalogOverlay.is_deleted.is_(False),
                )
                .all()
            )
        except Exception as e:  # pragma: no cover - defensive, catalog-optional
            logger.debug("semantic_store: alias overlay unavailable: %s", e)
            return {}

        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r.kb_id and r.kb_id not in self.kb_ids:
                continue
            if not r.table_name:
                continue
            key = r.table_name.lower()
            slot = out.setdefault(key, {"aliases": [], "description": None})
            if r.alias:
                slot["aliases"].append(r.alias)
            if r.description and not slot.get("description"):
                slot["description"] = r.description
        return out

    def glossary(
        self, question: str | None = None, limit: int = 12
    ) -> list[GlossaryTerm]:
        """Human-authored business vocabulary, optionally matched to a question.

        Sources, all VERIFIED by definition (a person wrote them):
        approved ``project_metric``, ``terminologies``, catalog overlay aliases.
        With a question, only terms the question actually mentions come back —
        progressive disclosure, never a dictionary dump.
        """
        from app.models.knowledge_catalog import ProjectCatalogOverlay, ProjectMetric
        from app.models.terminology import Terminology

        q_tokens = _tokens(question)
        terms: list[GlossaryTerm] = []

        # 1. terminologies (glossary entries)
        try:
            q = self.db.query(Terminology).filter(Terminology.enabled.is_(True))
            # An agent-scoped term belongs to that agent only; a caller that
            # does not know its agent gets the global vocabulary, never
            # another agent's.
            if self.agent_id:
                q = q.filter(
                    (Terminology.agent_id.is_(None))
                    | (Terminology.agent_id == self.agent_id)
                )
            else:
                q = q.filter(Terminology.agent_id.is_(None))
            for r in q.all():
                scope = _json_list(getattr(r, "datasource_ids", None))
                if scope and self.kb_ids and not (set(scope) & set(self.kb_ids)):
                    continue
                terms.append(
                    GlossaryTerm(
                        term=r.word,
                        definition=r.description or "",
                        tier=TIER_VERIFIED,
                        origin="terminology",
                    )
                )
        except Exception as e:  # pragma: no cover - table optional in some DBs
            logger.debug("semantic_store: terminology unavailable: %s", e)

        # 2. approved metrics (project-scoped; kb-scoped rows win)
        try:
            q = self.db.query(ProjectMetric).filter(ProjectMetric.status == "approved")
            if self.project_id:
                q = q.filter(ProjectMetric.project_id == self.project_id)
            else:
                q = q.filter(ProjectMetric.project_id.is_(None))
            for r in q.all():
                if r.kb_id and self.kb_ids and r.kb_id not in self.kb_ids:
                    continue
                terms.append(
                    GlossaryTerm(
                        term=r.name,
                        definition=r.definition or "",
                        tier=TIER_VERIFIED,
                        origin="metric",
                        unit=r.unit,
                        sql_expression=r.sql_expression,
                        query_pattern=r.query_pattern,
                        aliases=tuple(_json_list(r.aliases)),
                        aggregation=getattr(r, "default_aggregation", None),
                        bindings=getattr(r, "bindings", None),
                    )
                )
        except Exception as e:  # pragma: no cover
            logger.debug("semantic_store: project_metric unavailable: %s", e)

        # 3. metric definitions (name/definition/synonyms authored in the
        #    Metrics UI). Scoped by datasource_id, so a row only applies when
        #    it is bound to one of the in-scope datasources — otherwise it is
        #    silently skipped rather than leaking another source's vocabulary.
        try:
            from app.models.metric_definition import MetricDefinition

            q = self.db.query(MetricDefinition).filter(
                MetricDefinition.is_enabled.is_(True)
            )
            if self.kb_ids:
                q = q.filter(MetricDefinition.datasource_id.in_(list(self.kb_ids)))
            else:
                q = q.filter(MetricDefinition.id.is_(None))
            for r in q.all():
                terms.append(
                    GlossaryTerm(
                        term=r.display_name or r.name,
                        definition=r.description or "",
                        tier=TIER_VERIFIED,
                        origin="metric",
                        unit=r.unit,
                        aliases=tuple(_json_list(getattr(r, "synonyms", None))),
                    )
                )
        except Exception as e:  # pragma: no cover
            logger.debug("semantic_store: metric definitions unavailable: %s", e)

        # 4. catalog overlay aliases (a user's own name for a table/column)
        for key, slot in self._aliases().items():
            for alias in slot.get("aliases") or ():
                terms.append(
                    GlossaryTerm(
                        term=alias,
                        definition=slot.get("description") or "",
                        tier=TIER_VERIFIED,
                        origin="alias",
                    )
                )

        if q_tokens:
            matched: list[tuple[int, GlossaryTerm]] = []
            for t in terms:
                hits = _overlap(q_tokens, t.term, t.definition, " ".join(t.aliases))
                if hits:
                    matched.append((hits, t))
            matched.sort(key=lambda p: (-p[0], p[1].term))
            out = [t for _, t in matched]
        else:
            out = terms
        return out[: max(1, limit)]

    # -- query workload ----------------------------------------------------
    def workload(self, force: bool = False) -> dict[str, WorkloadSignal]:
        """Per-relation usage learned from real executions.

        Two persisted signals, both LEARNED_FROM_WORKLOAD:

        * ``query_plan_memory`` — a question whose plan the answer contract
          verified. Scoped by kb_id.
        * ``nl2sql_query_logs`` — the SQL that actually ran. The row carries no
          kb_id, so a table is counted ONLY when it already exists in the
          in-scope catalog. That intersection is the leak guard: telemetry from
          another warehouse cannot promote a table here.
        """
        if self._workload is not None and not force:
            return self._workload
        out: dict[str, WorkloadSignal] = {}
        if not self.kb_ids or not self._workload_enabled():
            self._workload = out
            return out

        known = {r.table_name.lower(): r.table_name for r in self.relations()}

        # 1. verified plans (a plan is only stored after the contract passed)
        try:
            from app.models.query_plan_memory import QueryPlanMemory

            rows = (
                self.db.query(QueryPlanMemory)
                .filter(
                    QueryPlanMemory.kb_id.in_(list(self.kb_ids)),
                    QueryPlanMemory.is_deleted.is_(False),
                )
                .all()
            )
            for r in rows:
                for tname in _json_list(r.tables):
                    real = known.get(str(tname).lower())
                    if not real:
                        continue
                    sig = out.get(real) or WorkloadSignal(table_name=real)
                    out[real] = WorkloadSignal(
                        table_name=real,
                        uses=sig.uses + 1,
                        successes=sig.successes + 1,
                        failures=sig.failures,
                        last_used=max(
                            [d for d in (sig.last_used, r.created_date) if d],
                            default=None,
                        ),
                    )
        except Exception as e:  # pragma: no cover
            logger.debug("semantic_store: plan memory unavailable: %s", e)

        # 2. raw execution telemetry
        try:
            from app.models.nl2sql_query_log import Nl2sqlQueryLog

            limit = int(
                getattr(_settings(), "SEMANTIC_STORE_LOG_SCAN_LIMIT", _LOG_SCAN_DEFAULT)
                or _LOG_SCAN_DEFAULT
            )
            rows = (
                self.db.query(Nl2sqlQueryLog)
                .order_by(Nl2sqlQueryLog.created_date.desc())
                .limit(max(1, limit))
                .all()
            )
            for r in rows:
                sql = r.generated_sql or ""
                if not sql:
                    continue
                ok = (r.outcome or "") == "success"
                seen: set[str] = set()
                for _q, ident in _TABLE_REF_RE.findall(sql):
                    name = (ident or "").split(".")[-1].strip("`\"[]").lower()
                    real = known.get(name)
                    if not real or real in seen:
                        continue
                    seen.add(real)
                    sig = out.get(real) or WorkloadSignal(table_name=real)
                    out[real] = WorkloadSignal(
                        table_name=real,
                        uses=sig.uses + 1,
                        successes=sig.successes + (1 if ok else 0),
                        failures=sig.failures + (0 if ok else 1),
                        last_used=max(
                            [d for d in (sig.last_used, r.created_date) if d],
                            default=None,
                        ),
                    )
        except Exception as e:  # pragma: no cover
            logger.debug("semantic_store: query log unavailable: %s", e)

        # 3. Mined workload stats (offline enrichment — see
        #    knowledge_graph/workload_miner.py). The miner ACCUMULATES what
        #    successful executions looked like; here it contributes USES to
        #    tables the catalog already knows. Popularity re-ranks, never
        #    creates: a table absent from ``known`` stays absent.
        try:
            from app.models.query_workload_stat import QueryWorkloadStat

            mined = (
                self.db.query(QueryWorkloadStat)
                .filter(
                    QueryWorkloadStat.kb_id.in_(list(self.kb_ids)),
                    QueryWorkloadStat.stat_kind == "popular_table",
                )
                .all()
            )
            for r in mined:
                real = known.get((r.table_name or "").lower())
                if not real:
                    continue
                sig = out.get(real) or WorkloadSignal(table_name=real)
                occ = int(r.occurrences or 0)
                out[real] = WorkloadSignal(
                    table_name=real,
                    uses=sig.uses + occ,
                    successes=sig.successes + occ,
                    failures=sig.failures,
                    last_used=max(
                        [d for d in (sig.last_used, r.last_seen_at) if d],
                        default=None,
                    ),
                )
        except Exception as e:  # pragma: no cover
            logger.debug("semantic_store: mined workload unavailable: %s", e)

        self._workload = out
        return out

    def _workload_enabled(self) -> bool:
        return bool(getattr(_settings(), "SEMANTIC_STORE_WORKLOAD_ENABLED", True))

    # -- mined workload stats (P0-1: table_pair / table_filter) -------------
    def _load_mined(self) -> dict:
        """Load the workload miner's table_pair / table_filter stats,
        kb-scoped and cached. ``{"pairs": {...}, "filters": {...}}``."""
        if self._mined is not None:
            return self._mined
        out: dict[str, dict] = {"pairs": {}, "filters": {}}
        try:
            from app.models.query_workload_stat import QueryWorkloadStat

            rows = (
                self.db.query(QueryWorkloadStat)
                .filter(
                    QueryWorkloadStat.kb_id.in_(list(self.kb_ids)),
                    QueryWorkloadStat.stat_kind.in_(["table_pair", "table_filter"]),
                    QueryWorkloadStat.occurrences > 0,
                )
                .order_by(QueryWorkloadStat.occurrences.desc())
                .limit(2000)
                .all()
            )
            for r in rows:
                occ = int(r.occurrences or 0)
                if r.stat_kind == "table_pair":
                    key = tuple(sorted((r.table_name or "", r.peer or "")))
                    if not all(key):
                        continue
                    out["pairs"][key] = out["pairs"].get(key, 0) + occ
                elif r.stat_kind == "table_filter":
                    table = (r.table_name or "").lower()
                    if not table:
                        continue
                    detail = r.detail if isinstance(r.detail, dict) else {}
                    filt = str(detail.get("filter") or "").strip()
                    if not filt:
                        continue
                    bucket = out["filters"].setdefault(table, {})
                    bucket[filt] = bucket.get(filt, 0) + occ
        except Exception as e:  # pragma: no cover — hints are best-effort
            logger.debug("semantic_store: mined stats unavailable: %s", e)
        self._mined = out
        return out

    def join_hints(self, *, min_occurrences: int = 2, k: int = 10) -> list[dict]:
        """Workload-validated join paths (table pairs analysts actually
        joined), most frequent first. These are LEARNED_FROM_WORKLOAD hints —
        ranking evidence for linking, never a promise of a real FK."""
        pairs = self._load_mined()["pairs"]
        ranked = sorted(
            ((a, b, n) for (a, b), n in pairs.items() if n >= min_occurrences),
            key=lambda t: -t[2],
        )[:k]
        return [
            {"table_a": a, "table_b": b, "occurrences": n, "tier": TIER_LEARNED}
            for a, b, n in ranked
        ]

    def common_filters(self, tables: list[str], *, k: int = 6) -> list[dict]:
        """Most frequent mined predicates for the given candidate tables —
        how analysts actually filter them (column = literal value)."""
        filters = self._load_mined()["filters"]
        wanted = {t.lower() for t in (tables or []) if t}
        flat: list[tuple[str, str, int]] = []
        for table, bucket in filters.items():
            if table not in wanted:
                continue
            for filt, occ in bucket.items():
                flat.append((table, filt, occ))
        flat.sort(key=lambda t: -t[2])
        return [
            {"table": t, "filter": f, "occurrences": n, "tier": TIER_LEARNED}
            for t, f, n in flat[:k]
        ]

    def workload_hints_for_tables(self, tables: list[str]) -> str:
        """Compact prompt block: workload-validated join paths + common
        filters for the selected tables. Empty string when nothing mined —
        the prompt must stay clean for fresh warehouses."""
        try:
            from app.config import settings as _s

            if not getattr(_s(), "NL2SQL_WORKLOAD_HINTS_ENABLED", True):
                return ""
        except Exception:  # noqa: BLE001
            pass
        if not self.kb_ids or not tables:
            return ""
        joins = self.join_hints(min_occurrences=2, k=5)
        sel = {t.lower() for t in tables if t}
        joins = [
            j for j in joins
            if j["table_a"] in sel and j["table_b"] in sel
        ]
        filters = self.common_filters(tables, k=6)
        parts: list[str] = []
        if joins:
            joined = "; ".join(
                f"{j['table_a']} ⋈ {j['table_b']} ({j['occurrences']}×)"
                for j in joins
            )
            parts.append(
                "Workload-validated join paths (tables analysts actually "
                f"joined here): {joined}."
            )
        if filters:
            filt = "; ".join(f"{f['table']}.{f['filter']} ({f['occurrences']}×)" for f in filters)
            parts.append(
                "Common filters analysts apply (reuse the convention when it "
                f"fits the question): {filt}."
            )
        if not parts:
            return ""
        return (
            "[WORKLOAD HINTS — learned from successful past queries; "
            "verify columns exist before using them]\n" + "\n".join(parts)
        )

    # -- retrieval ---------------------------------------------------------
    def rank_relations(
        self, question: str, k: int = 25, use_workload: bool | None = None
    ) -> list[RankedRelation]:
        """Narrow all in-scope relations to a small candidate set for a question.

        Deterministic and lexical-only, so a brand-new database works with no
        new code and no embedding service. Evidence for each hit is returned so
        the caller — and the model — can see WHY a table came back.

        Scores are RUNTIME_INFERRED: they are computed for this question and
        stored nowhere.
        """
        rels = self.relations()
        if not rels or not question:
            return []
        q_tokens = _tokens(question)
        if not q_tokens:
            return []

        usable_workload = (
            self._workload_enabled() if use_workload is None else bool(use_workload)
        )
        wl = self.workload() if usable_workload else {}

        scored: list[RankedRelation] = []
        for r in rels:
            evidence: list[str] = []
            score = 0.0
            # Semantic evidence only. Popularity must NEVER be able to create a
            # candidate on its own — measured live: a Chinese question with no
            # lexical match returned 25 tables scored purely by usage count,
            # i.e. "the most-used tables", presented as retrieval. Workload
            # re-ranks what meaning already found; it does not become truth.
            semantic = 0.0

            # exact identifiers named in the question are the strongest signal
            name_hits = _matched_question_tokens(q_tokens, r.table_name.replace("_", " "))
            if name_hits:
                semantic += 3.0 * len(name_hits)
                evidence.append(f"name:{'/'.join(name_hits)}")

            for alias in r.aliases:
                if _token_hits(q_tokens, alias):
                    semantic += 3.0
                    evidence.append(f"alias:{alias}")

            d_hits = max(
                _token_hits(q_tokens, r.description),
                # The other language's description is matched too, so a Chinese
                # question reaches a table whose DISPLAY description is English
                # (and vice versa) — the catalog has both, the store used one.
                _token_hits(q_tokens, getattr(r, "description_alt", "") or ""),
            )
            if d_hits:
                # Capped: a Chinese description is a long run of bigrams, so an
                # uncapped count let a verbose description outscore a VERIFIED
                # plan. Rarity-weighted scoring for the same evidence lives in
                # ``retrieval_evidence`` (BM25); this is the cheap store-side
                # signal and must stay below the plan weight.
                semantic += min(_W_DESC_CAP, 1.0 * d_hits)
                evidence.append(f"description:{min(d_hits, int(_W_DESC_CAP))}")

            if r.domain and _token_hits(q_tokens, r.domain):
                semantic += 1.5
                evidence.append(f"domain:{r.domain}")

            if semantic <= 0:
                continue

            score = semantic
            sig = wl.get(r.table_name)
            if sig and sig.uses:
                # Deliberately small: popularity must never outrank meaning.
                score += min(1.0, 0.25 * sig.uses)
                evidence.append(f"uses:{sig.uses}")

            scored.append(
                RankedRelation(
                    table_name=r.table_name,
                    kb_id=r.kb_id,
                    score=round(score, 4),
                    evidence=tuple(evidence[:4]),
                )
            )

        # relationship reachability: a table joined from a strong hit is worth
        # surfacing, at a discount, because questions span entities.
        if scored:
            strong = {s.table_name for s in scored if s.score >= 3.0}
            if strong:
                for j in self.joins():
                    for near, far in (
                        (j.source_table, j.target_table),
                        (j.target_table, j.source_table),
                    ):
                        if near in strong and far not in {s.table_name for s in scored}:
                            scored.append(
                                RankedRelation(
                                    table_name=far,
                                    kb_id=self._kb_of(far),
                                    score=1.0 * float(j.confidence or 1.0),
                                    evidence=(f"joins:{near}",),
                                )
                            )

        scored.sort(key=lambda s: (-s.score, s.table_name))
        return scored[: max(1, k)]

    def _kb_of(self, table_name: str) -> str:
        for r in self.relations():
            if r.table_name == table_name:
                return r.kb_id
        return self.kb_ids[0] if self.kb_ids else ""

    def rank_sources(self, question: str, k: int = 3) -> list[SourceCandidate]:
        """Rank the in-scope datasources for one question (cross-source retrieval).

        This is the missing step when an agent has MORE THAN ONE database
        bound: the model used to pick the warehouse out of a prompt list. The
        score is the best-scoring relation in each source, plus the source's
        name/metadata overlap, plus its domain match. It NARROWS; the agent
        still owns the goal and the final choice.
        """
        if not self.kb_ids or not question:
            return []

        q_tokens = _tokens(question)
        per_kb: dict[str, list[RankedRelation]] = {}
        for hit in self.rank_relations(question, k=max(10, k * 10)):
            per_kb.setdefault(hit.kb_id, []).append(hit)

        domains_by_kb: dict[str, list[str]] = {}
        for d in self.domains():
            if _tokens(f"{d['name_en']} {d['name_zh'] or ''}") & q_tokens:
                domains_by_kb.setdefault(d["kb_id"], []).append(d["name_en"])

        meta = self._source_meta()
        out: list[SourceCandidate] = []
        for kb_id in self.kb_ids:
            info = meta.get(kb_id) or {}
            hits = per_kb.get(kb_id) or []
            score = hits[0].score if hits else 0.0
            evidence: list[str] = []
            if hits:
                evidence.append(
                    "tables:" + ",".join(h.table_name for h in hits[:3])
                )
            if domains_by_kb.get(kb_id):
                score += 1.5
                evidence.append("domains:" + ",".join(domains_by_kb[kb_id][:2]))
            name_hits = q_tokens & _tokens(
                f"{info.get('name', '')} {info.get('database_name', '')}"
            )
            if name_hits:
                score += 2.0 * len(name_hits)
                evidence.append("source-name:" + "/".join(sorted(name_hits)))
            if score > 0:
                out.append(
                    SourceCandidate(
                        kb_id=kb_id,
                        name=info.get("name", kb_id),
                        database_name=info.get("database_name", "") or "",
                        db_type=info.get("db_type", "") or "",
                        score=round(score, 4),
                        evidence=tuple(evidence[:4]),
                    )
                )
        out.sort(key=lambda c: (-c.score, c.name))
        return out[: max(1, k)]

    def _source_meta(self) -> dict[str, dict[str, str]]:
        from app.models.knowledge_base import KnowledgeBase

        out: dict[str, dict[str, str]] = {}
        try:
            rows = (
                self.db.query(KnowledgeBase)
                .filter(KnowledgeBase.id.in_(list(self.kb_ids)))
                .all()
            )
        except Exception as e:  # pragma: no cover
            logger.debug("semantic_store: source metadata unavailable: %s", e)
            return out
        for kb in rows:
            out[kb.id] = {
                "name": kb.name or "",
                "database_name": getattr(kb, "database_name", "") or "",
                "db_type": getattr(kb, "db_type", "") or "",
            }
        return out

    # -- introspection -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Counts for telemetry and tests (no side effects)."""
        rels = self.relations()
        return {
            "kb_ids": list(self.kb_ids),
            "relations": len(rels),
            "columns_described": sum(
                1 for r in rels if (r.description or "").strip()
            ),
            "verified_relations": sum(1 for r in rels if r.tier == TIER_VERIFIED),
            "domains": len(self.domains()),
            "joins": len(self.joins()),
            "glossary_terms": len(self.glossary()),
            "workload_tables": len(self.workload()) if self._workload_enabled() else 0,
            "tiers": [TIER_VERIFIED, TIER_DISCOVERED, TIER_LEARNED, TIER_RUNTIME],
        }


def _settings() -> Any:
    from app.config import settings

    return settings


def get_semantic_store(
    db: Session,
    kb_ids: Sequence[str] | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
) -> SemanticStore:
    """Build a store for the caller's in-scope datasources.

    Returns an empty-scoped store when the feature flag is off, so callers can
    keep a single code path.
    """
    if not getattr(_settings(), "SEMANTIC_STORE_ENABLED", True):
        return SemanticStore(db, [], project_id=project_id, agent_id=agent_id)
    return SemanticStore(db, kb_ids, project_id=project_id, agent_id=agent_id)


def format_retrieval_hint(
    hits: Sequence[RankedRelation], limit: int = 12
) -> str:
    """Render ranked candidates as ONE prompt line.

    The shape matters: a deterministic guard must hand back the identifiers it
    already knows, not a verdict. This returns names plus why they matched.
    """
    if not hits:
        return ""
    parts = []
    for h in hits[:limit]:
        why = f" ({', '.join(h.evidence)})" if h.evidence else ""
        parts.append(f"{h.table_name}{why}")
    return "## Candidate tables for this question\n- " + "\n- ".join(parts)


def format_source_ranking(
    candidates: Sequence[SourceCandidate], limit: int = 3
) -> str:
    """Render cross-source retrieval as one prompt block.

    Emitted whenever at least one source has real evidence (score > 0). A
    single candidate is the MOST useful case, not a degenerate one: it means
    the other bound sources had no match at all for this question, which is
    exactly the discrimination the agent needs. Only an all-zero ranking
    (nothing matched anywhere) is suppressed.
    """
    if not candidates:
        return ""
    top = candidates[0]
    if top.score <= 0:
        return ""
    lines = ["### Datasource match for this question", ""]
    for i, c in enumerate(candidates[:limit]):
        why = f" — matched {', '.join(c.evidence)}" if c.evidence else ""
        lines.append(
            f"{i + 1}. **{c.name}** (id=`{c.kb_id}`, db_type=`{c.db_type}`)"
            f" score={c.score}{why}"
        )
    lines.append("")
    lines.append(
        "Pick the datasource that matches the question's subject; if the "
        "question spans more than one, ask separately per source."
    )
    return "\n".join(lines)
