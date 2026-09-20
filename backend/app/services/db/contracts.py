"""The two data contracts: GroundedPlan and CanonicalResultSet.

Why these exist
---------------
Every guard in this pipeline stops at the ROWS. Selection, retrieval, dialect,
contract, coverage, grounding — each one inspects part of the turn, and none of
them produces a single object that says *what was planned* and *what came back*,
in a shape a verifier can check and a narrator can be restricted to.

That gap has one visible symptom, measured live on a 143-table MySQL warehouse:
a question asking for five things returns four correct figures and one `0.0`,
and nothing in the payload distinguishes "the warehouse has no shipments for
August" from "the join that was supposed to produce shipments did not resolve".
The `0.0` is emitted as a claim.

So:

* :class:`GroundedPlan` — what the platform decided to answer, built from
  artifacts the existing pipeline already computed (catalog, retrieval, policy,
  dialect layer, plan memory). It carries the slots the question asked for that
  this plan CANNOT support, so they are named before any SQL is written.
* :class:`CanonicalResultSet` — what came back, with coverage, freshness, units,
  provenance, the validation verdict, and the same unsupported-slot list
  carried forward. Narration consumes this and nothing else.

Both are plain dataclasses with ``to_dict`` / ``from_dict`` / ``validate`` /
``fingerprint``. Neither contains a wall-clock field on the plan (so its
fingerprint is stable); the result set records ``executed_at`` and excludes it
from its fingerprint for the same reason.

Design rules encoded here
-------------------------
1. **Unsupported stays unsupported.** A slot the plan cannot support is recorded
   in ``unsupported_slots`` with a reason from :data:`UNSUPPORTED_REASONS` — it
   is never converted into a zero, a null-derived claim, or invented SQL.
2. **Incomplete results must say why.** ``validate`` refuses a result set that
   is not ``complete`` but carries no warning or unsupported slot.
3. **Only authorized, catalog-validated objects.** ``validate`` refuses a plan
   whose tables/columns escape ``authorized_tables``.
4. **No fixed source, table, vendor or customer.** Nothing in this module reads
   a name; it validates whatever the caller resolved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "p3.2"

# ── controlled vocabularies ────────────────────────────────────────────────

#: Why a slot the question asked for cannot be supported. Every one of these is
#: a DISCLOSURE obligation for the narrator, never a licence to emit 0.
UNSUPPORTED_REASONS = {
    # the catalog knows this concept, but only in tables this plan did not select
    "column_not_selected": "the matching column lives in a table this query does not use",
    # the plan selected the table but the catalog has no column for the concept
    "no_catalog_column": "the catalog holds no column for this",
    # the query returned no column carrying it
    "not_in_result": "the query returned no column for this",
    # the column is constant 0/NULL and nothing proves the zero is data
    "zero_unproven": "the column is zero/null everywhere and nothing proves the zero is data",
    # a constant-zero column whose aggregate crosses a join the catalog does not know
    "unvalidated_join": "the value crosses a join key the catalog does not know",
    # the column is denied to this user
    "blocked_by_policy": "this user may not read the columns that carry it",
    # the result hit the row cap, so the figure covers only part of the data
    "truncated": "the result was truncated, so the figure covers only part of the data",
    # a ratio whose two sides are not measured on one basis (grain/unit/window):
    # a claim about commensurability that the data source cannot vouch for
    "ratio_unverified": "the ratio's numerator and denominator could not be shown to share a basis",
}

#: What a question unit IS. Only a named business measurement can be
#: "unsupported": a count request, a table name, a schema term or a piece of
#: scaffolding asks for no figure, so disclosing one of them as missing would be
#: a false accusation against the source. Measured live: a correct
#: ``COUNT(*) = 26544`` came back with "the data source has nothing for
#: 一共有多少行数据, _ref_material_mapping", and a structure question produced
#: slots for 订单 and 金额相关字段.
SLOT_KINDS = {
    # a measurement the catalog can name (合同金额, shipped_qty, 履约率)
    "metric": "a named business measurement",
    # "how many rows/records" — a count request, answered by a count result
    "generic_count": "a request for how many rows or records",
    # the name of a table/entity the source holds (customers, 订单)
    "table": "the name of a table or entity this source holds",
    # schema/object vocabulary (表名, 字段清单, information_schema, pg_catalog)
    "catalog_term": "structure vocabulary, not business data",
    # a dimension or entity the question groups by
    "grouping": "a dimension or entity the question groups by",
    # natural-language scaffolding (总览, 情况, 请列出, please show)
    "scaffolding": "natural-language filler that names no measurement",
    # nothing in the catalog names this
    "unknown": "no catalog concept names this",
}

#: The engine's OWN objects: introspection/metadata, never business data. Kept
#: vendor-generic (every engine names its catalog this way), because a source is
#: bound at runtime and this module may not assume which engine answered.
METADATA_OBJECTS = (
    "information_schema",
    "pg_catalog",
    "pg_namespace",
    "pg_class",
    "pg_attribute",
    "pg_tables",
    "pg_views",
    "pg_indexes",
    "pg_constraint",
    "pg_type",
    "pg_proc",
    "pg_stat",
    "sqlite_master",
    "sqlite_schema",
    "sqlite_temp_master",
    "performance_schema",
    "dba_",
    "all_tables",
    "all_views",
    "all_tab_columns",
    "all_constraints",
    "all_indexes",
    "user_tables",
    "user_views",
    "user_tab_columns",
    "user_constraints",
    "user_indexes",
    "sys.objects",
    "sys.columns",
    "sys.tables",
    "sys.views",
    "dual",
)

#: Coverage states for the window the question asked about.
#:
#: ``not_requested`` is deliberately distinct from ``unknown``: the first means
#: no period was asked about at all (so nothing needed to be covered), the second
#: means a period was asked about and this table cannot answer whether it is
#: covered. Collapsing them would turn "we did not ask" into "we could not tell",
#: and every coverage-based decision would convict on an unmeasured table.
COVERAGE_STATES = {"covered", "partially_covered", "not_covered", "not_requested", "unknown"}

FRESHNESS_STATES = {"fresh", "stale", "unknown"}


def _canonical_sha256(payload: Mapping[str, Any], *, length: int = 16) -> str:
    """Stable hash of a payload: sorted keys, no whitespace, no float noise."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:length]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# ── GroundedPlan ───────────────────────────────────────────────────────────


@dataclass
class GroundedPlan:
    """What the platform decided to answer — and what it cannot.

    Built from artifacts that already exist at the selection seam; this is a
    record, not a second pipeline. Field names are the contract.
    """

    #: the question, normalised for comparison (whitespace-collapsed)
    intent: str = ""
    #: the question as asked, verbatim
    question: str = ""
    #: normalized intent signature (plan-memory compatible)
    intent_signature: str = ""
    #: {"start": ISO, "end": ISO, "phrase": str, "kind": "relative|absolute|none"}
    temporal_scope: dict[str, Any] = field(default_factory=dict)
    #: sources this turn is ALLOWED to read (post-policy, post-binding)
    source_ids: list[str] = field(default_factory=list)
    #: tables chosen for the query — a subset of ``authorized_tables``
    tables: list[str] = field(default_factory=list)
    #: table -> the columns the plan knew about when it chose the table
    columns: dict[str, list[str]] = field(default_factory=dict)
    #: every table this plan was allowed to choose from (catalog ∩ policy)
    authorized_tables: list[str] = field(default_factory=list)
    #: [{"left","right","left_column","right_column","validated","evidence"}]
    join_paths: list[dict[str, Any]] = field(default_factory=list)
    #: metric contracts resolved for this question
    #: [{"slot","label","tables","columns","unit","definition","tier"}]
    metrics: list[dict[str, Any]] = field(default_factory=list)
    #: the grouping grain the question asks for (catalog-typed dimensions)
    grain: list[str] = field(default_factory=list)
    #: [{"column","op","value","kind"}]
    filters: list[dict[str, Any]] = field(default_factory=list)
    groupings: list[str] = field(default_factory=list)
    #: [{"column","direction"}]
    ordering: list[dict[str, Any]] = field(default_factory=list)
    top_n: int | None = None
    #: the dialect the plan authorises (from db/dialect.py — never a guess)
    dialect: str = ""
    #: why each slot of the question is believed to be requested
    #: [{"slot","label","matched","score","source"}]
    evidence: list[dict[str, Any]] = field(default_factory=list)
    #: [{"slot","label","reason","detail","tables","columns"}] — ONLY business
    #: metrics can appear here (see SLOT_KINDS: a count request, a table name or
    #: a schema term is not a figure the source was supposed to produce)
    unsupported_slots: list[dict[str, Any]] = field(default_factory=list)
    #: the execution-boundary validation this plan ran under, when it got to one:
    #: {"ok","checks","errors","detail","authored_sql","executed_sql","enforced"}
    execution_validation: dict[str, Any] | None = None
    #: ratio metric contracts this plan resolved (see db/ratio_contract.py):
    #: [{"metric_id","numerator","denominator","required_grain", ...}]
    ratio_contracts: list[dict[str, Any]] = field(default_factory=list)
    #: question units that are NOT business metrics, with their kind:
    #: [{"unit","kind"}] — kept so "no disclosure was raised" is itself auditable
    non_metric_units: list[dict[str, Any]] = field(default_factory=list)
    #: questions the platform must ask before it can answer (never guessed past)
    clarification_needed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: catalog snapshot this plan was validated against
    catalog_version: str = ""
    #: capability-profile version of the source (db/capability_probe.py)
    capability_version: str = ""
    #: schema version of this contract
    schema_version: str = SCHEMA_VERSION

    # ── derived ───────────────────────────────────────────────────────────
    @property
    def supported_slots(self) -> list[str]:
        unsupported = {s.get("slot") for s in self.unsupported_slots}
        return [e.get("slot", "") for e in self.evidence if e.get("slot") not in unsupported]

    @property
    def complete(self) -> bool:
        """A plan is complete when nothing it was asked for is unsupported."""
        return not self.unsupported_slots

    def unsupported_labels(self) -> list[str]:
        return [str(s.get("label") or s.get("slot") or "") for s in self.unsupported_slots]

    def unsupported_reason(self, slot: str) -> str:
        for entry in self.unsupported_slots:
            if entry.get("slot") == slot:
                return str(entry.get("reason") or "")
        return ""

    def unit_kind(self, unit: str) -> str:
        """The classified kind of a question unit (``""`` when it is not here)."""
        for entry in self.non_metric_units:
            if entry.get("unit") == unit:
                return str(entry.get("kind") or "")
        for entry in self.evidence:
            if entry.get("slot") == unit or entry.get("unit") == unit:
                return str(entry.get("kind") or "")
        return ""

    def slot_disposition(self, slot: str) -> str:
        """One of: ``supported`` | ``unsupported_metric`` | ``non_metric`` | ``unknown_concept``.

        The four-way distinction is the point: "we answered it", "we could not
        and said so", "it was never a figure" and "we have never catalogued
        this" are four different answers, and only the second is a gap.
        """
        if any(s.get("slot") == slot for s in self.unsupported_slots):
            return "unsupported_metric"
        kind = self.unit_kind(slot)
        if kind == "metric":
            return "supported"
        if kind == "unknown":
            return "unknown_concept"
        if kind:
            return "non_metric"
        return "supported"

    # ── serialization ─────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "GroundedPlan":
        return _from_dict(cls, payload)

    def fingerprint(self) -> str:
        """Deterministic: the plan carries no wall-clock field by design."""
        return _canonical_sha256(self.to_dict())

    # ── validation ────────────────────────────────────────────────────────
    def validate(self) -> list[str]:
        """Internal consistency. Returns violations (empty == consistent).

        Deliberately does NOT need the database: the builder is responsible for
        resolution, this checks that what was recorded holds together.
        """
        v: list[str] = []
        if not self.intent:
            v.append("intent is empty")
        if not self.source_ids:
            v.append("no authorized source")
        if self.tables and not self.authorized_tables:
            v.append("selected tables but no authorized_tables set — cannot prove authorization")
        allowed = set(self.authorized_tables)
        for table in self.tables:
            if table not in allowed:
                v.append(f"table {table!r} is not in authorized_tables")
        for table, cols in self.columns.items():
            if table not in set(self.tables):
                v.append(f"columns recorded for unselected table {table!r}")
            if not isinstance(cols, list):
                v.append(f"columns for {table!r} must be a list")
        for path in self.join_paths:
            # A join may legitimately reach a table the plan did not SELECT as
            # one of its sources (a lookup table in the SQL), but it can never
            # reach one the turn was not authorized to read.
            for side in ("left", "right"):
                endpoint = path.get(side)
                if endpoint and endpoint not in allowed and endpoint not in set(self.tables):
                    v.append(
                        f"join path {endpoint!r} is neither selected nor authorized"
                    )
            if not path.get("validated") and not path.get("evidence"):
                v.append(f"unvalidated join {path.get('left')}->{path.get('right')} has no evidence")
        if self.dialect and self.dialect not in _known_dialects():
            v.append(f"dialect {self.dialect!r} is not a known dialect")
        if not self.dialect:
            v.append("dialect is empty")
        if self.top_n is not None and (not isinstance(self.top_n, int) or self.top_n <= 0):
            v.append(f"top_n must be a positive integer, got {self.top_n!r}")
        for slot in self.unsupported_slots:
            if not slot.get("slot"):
                v.append("unsupported slot without a key")
            if slot.get("reason") not in UNSUPPORTED_REASONS:
                v.append(
                    f"unsupported slot {slot.get('slot')!r} has reason "
                    f"{slot.get('reason')!r} outside the vocabulary"
                )
            kind = slot.get("kind")
            if kind is not None and kind != "metric":
                v.append(
                    f"unsupported slot {slot.get('slot')!r} is classified {kind!r} — "
                    "only a business metric may be unsupported"
                )
        if self.ratio_contracts:
            from app.services.db.ratio_contract import RatioContract, validate_contract

            for payload in self.ratio_contracts:
                v.extend(validate_contract(RatioContract.from_dict(payload)))
        unsupported_keys = {s.get("slot") for s in self.unsupported_slots}
        for entry in self.non_metric_units:
            unit = entry.get("unit")
            if not unit:
                v.append("non-metric unit without a key")
            if entry.get("kind") not in SLOT_KINDS:
                v.append(
                    f"non-metric unit {unit!r} has kind {entry.get('kind')!r} "
                    f"outside {sorted(SLOT_KINDS)}"
                )
            if entry.get("kind") == "metric":
                v.append(
                    f"non-metric unit {unit!r} is classified 'metric' — metrics belong "
                    "in unsupported_slots, not here"
                )
            if unit in unsupported_keys:
                v.append(f"unit {unit!r} is both non-metric and unsupported")
        for question in self.clarification_needed:
            if not str(question).strip():
                v.append("empty clarification question")
        return v

    def check_catalog_membership(self, catalog_tables: Iterable[str]) -> list[str]:
        """Everything the plan selected must exist in the catalog it was built on."""
        known = set(catalog_tables)
        return [f"table {t!r} is not in the catalog" for t in self.tables if t not in known]


def _known_dialects() -> set[str]:
    try:
        from app.services.db.dialect import known_dialects

        return set(known_dialects())
    except Exception:  # pragma: no cover - dialect layer always importable
        return set()


# ── CanonicalResultSet ─────────────────────────────────────────────────────


@dataclass
class CanonicalResultSet:
    """What came back — the ONLY input to narration.

    ``rows`` are the canonical data. ``complete`` means the result answers the
    whole plan; when it is False, ``warnings``/``unsupported_slots`` must say
    what is missing (enforced by :meth:`validate`).
    """

    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    #: True when no unsupported slot, no truncation, no violated contract
    complete: bool = True
    source_ids: list[str] = field(default_factory=list)
    sql_fingerprint: str = ""
    #: the window the question asked about (ISO start/end) or None
    requested_coverage: dict[str, Any] | None = None
    #: table -> {"min","max","date_column","state"} — what the data actually holds
    available_coverage: dict[str, Any] = field(default_factory=dict)
    #: {"state": fresh|stale|unknown, "as_of": ISO|None, "detail": str}
    freshness: dict[str, Any] = field(default_factory=lambda: {"state": "unknown"})
    #: column -> unit (from the catalog's semantic typing)
    units: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: the audit trail (source, tables, retrieval method, candidates, …)
    provenance: dict[str, Any] = field(default_factory=dict)
    #: ISO timestamp of the execution
    executed_at: str = field(default_factory=_now_iso)
    #: the deterministic verdicts that ran on this result
    #: {"answer_contract": {...}, "plan": [...], "policy": [...]}
    validation: dict[str, Any] = field(default_factory=dict)
    #: carried forward from the plan so narration cannot lose it
    unsupported_slots: list[dict[str, Any]] = field(default_factory=list)
    #: the execution-boundary validation that produced (or refused) this result:
    #: present on a refused statement, where ``rows`` is empty and ``complete`` is
    #: False, so a failure is a structured object and not just a string.
    execution_validation: dict[str, Any] | None = None
    #: one verdict per ratio-shaped result column, with the checks that ran:
    #: [{"metric","column","value","ok","reasons","checks","contract","evidence"}]
    ratio_validations: list[dict[str, Any]] = field(default_factory=list)
    #: a row-count question resolved to an actual count result:
    #: {"column","value","unit","kind":"row_count"} — "how many rows" is a
    #: supported question with a canonical answer, NOT an unsupported metric
    canonical_count: dict[str, Any] = field(default_factory=dict)
    #: the reader-facing sentence for anything unsupported ("" when complete)
    disclosure: str = ""
    schema_version: str = SCHEMA_VERSION
    #: the plan this result answers (fingerprint only — the plan rides separately)
    plan_fingerprint: str = ""

    # ── derived ───────────────────────────────────────────────────────────
    def unsupported_labels(self) -> list[str]:
        return [str(s.get("label") or s.get("slot") or "") for s in self.unsupported_slots]

    def count_value(self) -> Any:
        """The resolved row-count answer, or ``None`` when the question was not a count."""
        return self.canonical_count.get("value") if self.canonical_count else None

    def column(self, name: str) -> list[Any]:
        return [row.get(name) for row in self.rows]

    def is_constant_zero(self, name: str) -> bool:
        """True when every value of the column is 0 or NULL — the silent-zero shape."""
        values = self.column(name)
        if not values:
            return False
        for value in values:
            if value is None:
                continue
            try:
                if float(value) != 0.0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    # ── serialization ─────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "CanonicalResultSet":
        return _from_dict(cls, payload)

    def fingerprint(self) -> str:
        """Deterministic: ``executed_at`` is excluded (data identity, not timing)."""
        payload = self.to_dict()
        payload.pop("executed_at", None)
        payload.pop("disclosure", None)
        return _canonical_sha256(payload)

    # ── validation ────────────────────────────────────────────────────────
    def validate(self) -> list[str]:
        v: list[str] = []
        if self.row_count != len(self.rows):
            v.append(f"row_count {self.row_count} != len(rows) {len(self.rows)}")
        if self.rows and not self.columns:
            v.append("rows present but no columns declared")
        declared = set(self.columns)
        for i, row in enumerate(self.rows):
            extra = set(row) - declared
            if extra:
                v.append(f"row {i} carries undeclared columns {sorted(extra)}")
        if not self.source_ids:
            v.append("no source ids")
        if self.rows and not self.sql_fingerprint:
            v.append("rows present but no sql_fingerprint")
        if self.freshness.get("state") not in FRESHNESS_STATES:
            v.append(f"freshness state {self.freshness.get('state')!r} is not {sorted(FRESHNESS_STATES)}")
        if self.requested_coverage is not None and not isinstance(self.requested_coverage, dict):
            v.append("requested_coverage must be a mapping or None")
        for table, cov in self.available_coverage.items():
            if not isinstance(cov, dict):
                v.append(f"available_coverage[{table!r}] must be a mapping")
                continue
            state = cov.get("state")
            if state is not None and state not in COVERAGE_STATES:
                v.append(f"available_coverage[{table!r}].state {state!r} outside {sorted(COVERAGE_STATES)}")
        if not self.complete and not (self.warnings or self.unsupported_slots):
            v.append("result is incomplete but carries no warning or unsupported slot")
        for slot in self.unsupported_slots:
            if slot.get("reason") not in UNSUPPORTED_REASONS:
                v.append(
                    f"unsupported slot {slot.get('slot')!r} has reason "
                    f"{slot.get('reason')!r} outside the vocabulary"
                )
        if self.unsupported_slots and not self.disclosure:
            v.append("unsupported slots present but no disclosure sentence")
        if self.execution_validation:
            failed = self.execution_validation
            if "ok" not in failed:
                v.append("execution validation without a verdict")
            if not failed.get("checks"):
                v.append("execution validation records no checks")
            if failed.get("ok") is False and not failed.get("errors"):
                v.append("a refused statement must carry its errors")
            if not failed.get("authored_sql") and failed.get("ok") is False:
                v.append("a refused statement must preserve the SQL that was authored")
        for entry in self.ratio_validations:
            if not entry.get("metric"):
                v.append("ratio validation without a metric")
            if "ok" not in entry:
                v.append(f"ratio validation {entry.get('metric')!r} carries no verdict")
            if not entry.get("checks"):
                v.append(f"ratio validation {entry.get('metric')!r} records no checks")
        if self.canonical_count:
            if not self.canonical_count.get("column"):
                v.append("canonical count without a column")
            if "value" not in self.canonical_count:
                v.append("canonical count without a value")
            unit = self.canonical_count.get("unit")
            if unit and unit in {s.get("slot") for s in self.unsupported_slots}:
                v.append(f"canonical count {unit!r} is also reported as an unsupported slot")
        if not self.validation:
            v.append("no validation verdict recorded")
        return v


def _from_dict(cls, payload: Mapping[str, Any] | None):
    """Tolerant construction: unknown keys are dropped, absent ones defaulted.

    The payload is serialized into tool results and logs, so it must survive a
    round trip even when a newer writer added a field this reader does not know.
    """
    if payload is None:
        return cls()
    if isinstance(payload, cls):
        return payload
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in dict(payload).items() if k in known})


# ── shared helpers for the builders ────────────────────────────────────────


def dialect_of(kb: Any) -> str:
    """The dialect the plan authorises — resolved, never guessed.

    Prefers the persisted capability profile (what the source actually answered)
    and falls back to the raw ``db_type``; returns ``""`` when neither resolves,
    which the plan validator reports as a violation rather than defaulting to
    some other vendor's syntax.
    """
    db_type = (getattr(kb, "db_type", "") or "").strip().lower()
    try:
        from app.services.db.dialect import maybe_profile

        profile = maybe_profile(db_type)
        return profile.name if profile is not None else ""
    except Exception:
        return ""


def coverage_state(
    entry: Mapping[str, Any] | None, window: tuple | None
) -> tuple[str, dict[str, Any]]:
    """Classify a catalog table's window against the window the question asked for.

    Missing ≠ empty: an absent catalog window or an unparsable end date is
    ``unknown``, never ``not_covered``. Convicting on an unmeasured table is how
    a coverage check becomes a false accusation.
    """
    cov = {
        "min": (entry or {}).get("min_date") or "",
        "max": (entry or {}).get("max_date") or "",
        "date_column": (entry or {}).get("date_column") or "",
    }
    if not entry or not cov["max"]:
        return "unknown", cov
    if not window:
        return "not_requested", cov
    start, end = _as_dates(window)
    lo, hi = _parse_date(cov["min"]), _parse_date(cov["max"])
    if hi is None:
        return "unknown", cov
    if lo is not None and lo >= start and hi < end:
        return "covered", cov
    if hi < start:
        return "not_covered", cov
    if hi < end:
        return "partially_covered", cov
    return "covered", cov


def _as_dates(window: Any) -> tuple[Any, Any]:
    from datetime import date, timedelta

    try:
        start, end = window[0], window[1]
    except Exception:
        return date(1900, 1, 1), date(2999, 12, 31)
    start = _parse_date(start) or date(1900, 1, 1)
    # the window end is exclusive in the pipeline (半开区间): normalise to inclusive
    end = _parse_date(end) or date(2999, 12, 31)
    return start, end - timedelta(days=1) if end > start else end


def _parse_date(value: Any):
    from datetime import date, datetime

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if text[:4].isdigit():
        try:
            return date(int(text), 1, 1)
        except ValueError:
            return None
    return None


def freshness_of(
    *available: Mapping[str, Any], window: tuple | None = None
) -> dict[str, Any]:
    """Freshness of the data that actually answered, relative to the window asked.

    Deliberately takes the coverage of the tables the query TOUCHED, not of the
    whole catalog: a source where one table stops in July and another runs to
    today is fresh for most questions.
    """
    states = []
    newest = None
    for cov in available:
        if not cov:
            continue
        hi = _parse_date(cov.get("max"))
        if hi is not None:
            newest = hi if newest is None else max(newest, hi)
        states.append(coverage_state(cov, window)[0])
    if not states or newest is None:
        return {"state": "unknown", "as_of": None, "detail": "no catalog window for the tables used"}
    if window:
        start, _ = _as_dates(window)
        if newest < start:
            return {
                "state": "stale",
                "as_of": newest.isoformat(),
                "detail": f"the tables used hold data up to {newest.isoformat()}",
            }
    return {"state": "fresh", "as_of": newest.isoformat(), "detail": ""}
