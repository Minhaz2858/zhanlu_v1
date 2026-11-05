"""Stage 2B-1: generic, metadata-driven schema classification (2026-10-30).

Design: docs/plans/2026-10-30-dynamic-schema-classification-design.md §3.1.

Pure functions over catalog metadata — no DB calls, no I/O — so the
classifiers are directly unit-testable against synthetic schemas and the
indexer/backfill simply feed them.

Hard constraints (reviewer boundary, Phase C authorization):
- ZERO customer-specific rules: no table names, no domain vocabularies,
  no assumptions that any database contains sales/ERP data. Every signal
  is structural (engine namespaces, column shapes, FK graph, row counts)
  or comes from the connected database's own metadata.
- Insufficient evidence yields ``unknown`` — never an invented
  "global" or "business" classification (an undetermined fact must not
  read as a safe one; same contract as join_cardinality).
- Human overrides (ProjectCatalogOverlay) always win — enforced at the
  read sites (P-SV facts loader), never in these pure functions.

Column-name *shape* stems (date/amount/credential-like) are generic
structural vocabulary, the same precedent catalog_indexer and
schema_validator already use — they describe COLUMN KINDS, not any
customer's business nouns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── S1: engine-provided namespaces (structural, per ENGINE — generic) ──────
# Only applies to SCHEMA-QUALIFIED names; a business table merely called
# "sys" (unqualified) must never read as system.
ENGINE_SYSTEM_SCHEMAS: dict[str, frozenset[str]] = {
    "mysql": frozenset({"information_schema", "performance_schema", "sys", "mysql"}),
    "mariadb": frozenset({"information_schema", "performance_schema", "sys", "mysql"}),
    "postgresql": frozenset({"information_schema", "pg_catalog", "pg_toast"}),
    "postgres": frozenset({"information_schema", "pg_catalog", "pg_toast"}),
    "mssql": frozenset({"information_schema", "sys", "guest", "db_owner"}),
    "sqlserver": frozenset({"information_schema", "sys", "guest", "db_owner"}),
    "oracle": frozenset({"system", "sys", "ctxsys", "mdsys", "olapsys", "xdb"}),
    "sqlite": frozenset(),  # single-namespace engine; sqlite_* handled below
}
_SQLITE_SYSTEM_PREFIX = "sqlite_"

# ── S2: credential/session column shapes (structural column kinds) ─────────
_CREDENTIAL_SHAPE = re.compile(
    r"(password|passwd|pwd|secret|salt|token|api_?key|session|"
    r"credential|otp|nonce)",
    re.IGNORECASE,
)
# A business-shaped column: temporal, numeric, or descriptive — the column
# KINDS that carry business facts/dimensions (generic kinds, not nouns).
_BUSINESS_SHAPE = re.compile(
    r"(date|time|amount|qty|quantity|count|total|price|value|volume|"
    r"status|code|name|type|category|balance|rate|score|region|level)",
    re.IGNORECASE,
)
_NUMERIC_TYPES = re.compile(r"(int|decimal|numeric|float|double|real|money)", re.I)
_TEMPORAL_TYPES = re.compile(r"(date|time|timestamp)", re.I)

CLASS_SYSTEM = "system"
CLASS_BUSINESS = "business"
CLASS_UNKNOWN = "unknown"

_FINGERPRINT_JACCARD = 0.7

_NAME_SEPARATORS = re.compile(r"[_\-\s]+")


@dataclass(frozen=True)
class ClassVerdict:
    """System-vs-business verdict with its evidence trail."""

    schema_class: str            # system | business | unknown
    confidence: float
    signals: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class ScopeVerdict:
    """Scope verdict for ONE table (global vs partition member)."""

    level: str                    # global | partitioned | unknown
    partition_key: str | None = None
    partition_value: str | None = None
    siblings: tuple[str, ...] = field(default=())
    rollup: str | None = None     # the family's global sibling, if any
    confidence: float = 0.0
    signals: tuple[str, ...] = field(default=())


def _norm(name: str) -> str:
    return _NAME_SEPARATORS.sub("_", str(name or "").strip()).lower()


def _is_engine_system(table: dict) -> tuple[bool, str]:
    engine = _norm(table.get("engine", "") or "")
    schema = _norm(table.get("schema_name", "") or "")
    name = str(table.get("table_name", "") or "")
    if engine == "sqlite" and name.lower().startswith(_SQLITE_SYSTEM_PREFIX):
        return True, f"S1: sqlite system object {name!r}"
    if not schema:
        return False, ""
    if schema in ENGINE_SYSTEM_SCHEMAS.get(engine, frozenset()):
        return True, f"S1: qualified under engine namespace {schema!r}"
    return False, ""


def _shape_counts(columns: list) -> tuple[int, int, int]:
    """(credential-shaped, business-shaped, name-business-shaped) counts.

    The third count is NAME evidence only — a credential table's universal
    ``id``/timestamp structure would otherwise outvote its credential
    identity (2C defect D-4: every real internal table carries an integer
    PK and a timestamp, so type-based evidence can never discriminate
    system from business; column NAMES can).
    """
    cred = biz = biz_name = 0
    for c in columns or []:
        name = str(c.get("column_name", "") if isinstance(c, dict) else c)
        dtype = str(c.get("data_type", "") if isinstance(c, dict) else "")
        if _CREDENTIAL_SHAPE.search(name):
            cred += 1
            continue
        if _BUSINESS_SHAPE.search(name):
            biz += 1
            biz_name += 1
            continue
        if _NUMERIC_TYPES.search(dtype) or _TEMPORAL_TYPES.search(dtype):
            biz += 1
    return cred, biz, biz_name


def classify_system_business(
    table: dict,
    *,
    relation_degree: int = 0,
    domain_support: bool = False,
    table_role: str = "unknown",
) -> ClassVerdict:
    """Classify ONE table as system / business / unknown.

    Signals (design §3.1.1) — the first sufficient one wins:

    S1 engine namespace (system, 0.99) — schema-qualified names only.
    S2 credential/session column shape (system, 0.75-0.9) — fires only
       when the table shows credential shapes and NO business shape.
    Positive business evidence (join edges, domain support, business
    column shapes) → ``business`` 0.7.
    S3 graph isolation — zero join edges + no domain support + role
       unknown → ``unknown`` 0.6 with the signal recorded (the read sites
       may deprioritize it; nothing is hard-excluded).
    Otherwise ``unknown`` — never an invention. Source comments are
    deliberately NOT consulted (S4): comments are untrusted for
    auto-classification; they are human-override input.
    """
    signals: list[str] = []

    is_system, s1_sig = _is_engine_system(table)
    if is_system:
        return ClassVerdict(CLASS_SYSTEM, 0.99, (s1_sig,))

    columns = table.get("columns") or []
    cred, biz, biz_name = _shape_counts(columns)
    # 2C defect fix (D-4): S2's "no business shape" test now looks at
    # NAME-based business shapes only, and additionally requires the table
    # to carry no OTHER business evidence (join edges, domain membership).
    # Type presence (integer PK, timestamp) is universal and must never
    # outvote a credential identity — but a connected table with real
    # business data keeps its business verdict via the graph evidence.
    if cred and not biz_name and relation_degree == 0 and not domain_support:
        conf = 0.9 if cred >= 2 else 0.75
        signals.append(
            f"S2: {cred} credential-shaped columns, no name-level business "
            "shapes, no join evidence"
        )
        return ClassVerdict(CLASS_SYSTEM, conf, tuple(signals))

    if biz or relation_degree > 0 or domain_support:
        if biz:
            signals.append(f"business column shapes ({biz})")
        if relation_degree > 0:
            signals.append(f"{relation_degree} catalog join edge(s)")
        if domain_support:
            signals.append("domain membership")
        if table_role == "unknown" and relation_degree == 0:
            signals.append("role unknown — thin evidence, ranked not excluded")
        return ClassVerdict(CLASS_BUSINESS, 0.7, tuple(signals))

    if relation_degree == 0 and not domain_support and table_role == "unknown":
        signals.append("S3: zero join edges, no domain support, role unknown")
        return ClassVerdict(CLASS_UNKNOWN, 0.6, tuple(signals))

    return ClassVerdict(CLASS_UNKNOWN, 0.0, ("insufficient evidence",))


# ── scope: partition-family detection (P1) + rollup (P3/P4) ────────────────


def _fingerprint(table: dict) -> frozenset[str]:
    return frozenset(
        _norm(c.get("column_name") if isinstance(c, dict) else c)
        for c in (table.get("columns") or [])
    )


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _name_parts(name: str) -> list[str]:
    return [p for p in _NAME_SEPARATORS.split(_norm(name)) if p]


def _split_base_token(member: str, base: str) -> str | None:
    """``member == token + base`` or ``base + token`` (separator-insensitive).

    Returns the differing token — the partition VALUE — or None when the
    member does not decompose against the base in either direction.
    """
    m_parts = _name_parts(member)
    b_parts = _name_parts(base)
    if not m_parts or not b_parts or len(m_parts) <= len(b_parts):
        return None
    if m_parts[: len(b_parts)] == b_parts:
        token = "_".join(m_parts[len(b_parts):])
        return token or None
    if m_parts[-len(b_parts):] == b_parts:
        token = "_".join(m_parts[: len(m_parts) - len(b_parts)])
        return token or None
    return None


def _cluster_families(tables: list[dict]) -> list[list[dict]]:
    """Greedy fingerprint clustering (Jaccard ≥ threshold)."""
    remaining = list(tables)
    families: list[list[dict]] = []
    while remaining:
        seed = remaining.pop(0)
        fam = [seed]
        fp = _fingerprint(seed)
        rest = []
        for t in remaining:
            if _jaccard(fp, _fingerprint(t)) >= _FINGERPRINT_JACCARD:
                fam.append(t)
            else:
                rest.append(t)
        remaining = rest
        families.append(fam)
    return families


def _match_partition_key(
    family: list[dict], tokens: list[str], column_stats: dict | None
) -> str | None:
    """P2: a family column whose OBSERVED values match the suffix tokens.

    2C defect fix (D-2, 2026-10-30): the canonical name-partition family
    carries a per-table CONSTANT partition value (``north_orders.region``
    holds only ``{"north"}``), so requiring ONE table's values to equal the
    FULL token set could never fire — every family degraded to the key-less
    verdict and the scope gate's confirmed-partition rules were unreachable
    on discovered metadata. The evidence test is now family-level: every
    member's observed values must be a SUBSET of the token set, and the
    family-wide UNION must EQUAL it. Still observed data only — no
    vocabulary, no name inference beyond the tokens P1 already established.
    """
    if not column_stats:
        return None
    token_set = {t.lower() for t in tokens}
    per_column: dict[str, list[set[str]]] = {}
    for t in family:
        stats = (column_stats or {}).get(str(t.get("table_name", ""))) or {}
        for col, meta in stats.items():
            values = {
                str(v).strip().lower()
                for v in (meta.get("values") or [])
                if str(v).strip()
            }
            if values:
                per_column.setdefault(str(col), []).append(values)
    for col, per_table in per_column.items():
        if not per_table:
            continue
        if not all(v <= token_set for v in per_table):
            continue
        union: set[str] = set()
        for v in per_table:
            union |= v
        if union == token_set:
            return col
    return None


def _family_tokens(names: list[str]) -> tuple[str, list[str]] | None:
    """Base + one distinct token per member, when the names decompose.

    Multiset-aware: the parts common to ALL members form the base; each
    member must carry exactly ONE differing part, and the tokens must be
    distinct. ``north_orders/south_orders/east_orders`` →
    ``("orders", ["north","south","east"])``.
    """
    parts_lists = [_name_parts(n) for n in names]
    if not parts_lists or any(not pl for pl in parts_lists):
        return None
    common = set(parts_lists[0])
    for pl in parts_lists[1:]:
        common &= set(pl)
    if not common:
        return None
    tokens: list[str] = []
    for pl in parts_lists:
        rest = list(pl)
        for c in common:
            if c in rest:
                rest.remove(c)
        if len(rest) != 1:
            return None
        tokens.append(rest[0])
    if len(set(tokens)) != len(tokens):
        return None
    return "_".join(sorted(common)), tokens


def _find_rollup(
    family: list[dict], all_tables: list[dict]
) -> tuple[dict | None, list[str] | None]:
    """P3: the global sibling of a partition family, if one exists.

    A rollup is a table OUTSIDE the strict fingerprint family whose name
    decomposes every family member into ``base + token`` (distinct tokens)
    and whose fingerprint is a SUPERSET of a family member's. Returns
    ``(rollup_table, tokens)`` or ``(None, None)``.
    """
    member_names = [str(t.get("table_name", "")) for t in family]
    family_names = set(member_names)
    family_fp = _fingerprint(family[0])
    for cand in all_tables:
        cand_name = str(cand.get("table_name", ""))
        if cand_name in family_names:
            continue
        if not (_fingerprint(cand) >= family_fp):
            continue
        tokens = []
        ok = True
        for m in member_names:
            token = _split_base_token(m, cand_name)
            if not token:
                ok = False
                break
            tokens.append(token)
        if ok and len(set(tokens)) == len(tokens) and tokens:
            return cand, tokens
    return None, None


def infer_scope(
    tables: list[dict],
    column_stats: dict | None = None,
) -> dict[str, ScopeVerdict]:
    """Scope verdicts for every table (P1-P4), keyed by table name.

    P1 partition families: near-identical column fingerprints whose names
    decompose against a common base (with or without a rollup sibling
    present) → each member is a PARTITION; its trailing token is its
    partition VALUE. Without a clean base+token pattern → ``unknown`` —
    clustering alone is NOT scope evidence.
    P2 partition key: a family column whose observed values equal the
    token set (needs column_stats: {table: {col: {"values": [...]}}}).
    P3 rollup sibling: the superset-fingerprint table the family names
    decompose against → verdict ``global`` for that sibling.
    P4 row-count parity: rollup ≈ sum(partitions) corroborates (advisory,
    folded into confidence).
    """
    verdicts: dict[str, ScopeVerdict] = {}
    verdicted_names: set[str] = set()
    families = _cluster_families(tables)

    for family in families:
        names = [str(t.get("table_name", "")) for t in family]
        if len(family) < 2:
            # A singleton is usually "no family" — but NEVER overwrite a
            # verdict an earlier family already issued (a rollup sibling
            # clusters with nobody; its verdict comes from its partitions).
            if names[0] not in verdicted_names and names[0] not in verdicts:
                verdicts[names[0]] = ScopeVerdict(
                    "unknown", confidence=0.0, signals=("no fingerprint family",)
                )
                verdicted_names.add(names[0])
            continue

        rollup_row, tokens = _find_rollup(family, tables)
        if rollup_row is None:
            # No rollup sibling: still a partition family IF the members
            # decompose against their own common name parts.
            decomposition = _family_tokens(names)
            if decomposition is None:
                for n in names:
                    verdicts[n] = ScopeVerdict(
                        "unknown",
                        confidence=0.3,
                        signals=(
                            f"fingerprint family of {len(family)} but no "
                            "base/token name pattern — insufficient evidence",
                        ),
                    )
                    verdicted_names.add(n)
                continue
            _, tokens = decomposition
            confidence = 0.55
            partition_key = _match_partition_key(family, tokens, column_stats)
            extra = [f"P2: column {partition_key!r} values == tokens"] if partition_key else []
            member_names = list(names)
            for n, tok in zip(names, tokens):
                verdicts[n] = ScopeVerdict(
                    "partitioned",
                    partition_key=partition_key,
                    partition_value=tok,
                    siblings=tuple(m for m in member_names if m != n),
                    rollup=None,
                    confidence=confidence,
                    signals=(
                        f"P1: family of {len(family)} near-identical "
                        f"fingerprints; token {tok!r} (no rollup sibling)",
                        *extra,
                    ),
                )
                verdicted_names.add(n)
            continue

        # Family WITH a rollup sibling.
        partition_key = _match_partition_key(family, tokens, column_stats)
        signals_extra: list[str] = []
        if partition_key:
            signals_extra.append(f"P2: column {partition_key!r} values == tokens")
        confidence = 0.6 if partition_key else 0.55
        rollup_name = str(rollup_row.get("table_name", ""))
        try:
            parts_sum = sum(
                float(t.get("row_count") or 0) for t in family
            )
            rollup_count = float(rollup_row.get("row_count") or 0)
            if parts_sum and rollup_count >= parts_sum * 0.5:
                confidence = min(0.85, confidence + 0.2)
                signals_extra.append(
                    f"P4: rollup rows ({rollup_count:.0f}) ≈ partition sum "
                    f"({parts_sum:.0f})"
                )
        except (TypeError, ValueError):
            pass

        for n, tok in zip(names, tokens):
            verdicts[n] = ScopeVerdict(
                "partitioned",
                partition_key=partition_key,
                partition_value=tok,
                siblings=tuple(m for m in names if m != n),
                rollup=rollup_name,
                confidence=confidence,
                signals=(
                    f"P1: family of {len(family)} near-identical fingerprints; "
                    f"token {tok!r}",
                    *signals_extra,
                ),
            )
            verdicted_names.add(n)
        # Always assign — the rollup may have been seen earlier as a
        # singleton family; the partitions' evidence is the better verdict.
        verdicts[rollup_name] = ScopeVerdict(
            "global",
            partition_key=partition_key,
            siblings=tuple(names),
            rollup=rollup_name,
            confidence=confidence,
            signals=(
                "P3: fingerprint superset of the partition family; "
                "member names decompose against it",
                *signals_extra,
            ),
        )
        verdicted_names.add(rollup_name)

    for t in tables:
        n = str(t.get("table_name", ""))
        if n not in verdicted_names and n not in verdicts:
            verdicts[n] = ScopeVerdict(
                "unknown", confidence=0.0, signals=("no fingerprint family",)
            )
    return verdicts


# ── storage vocabulary (review point 1, 2026-10-30) ────────────────────────
# Three DISTINCT states — a likely family without a confirmed key must
# NEVER be written as ``partition:<key>``, or a later scope gate would
# treat an unverified guess as a verified partition:
#   "global"            — genuine global/rollup dataset
#   "partition:<key>"   — partition family with the key CONFIRMED (P2:
#                         observed column values == the token set)
#   "partition_family"  — partition-shaped family, key NOT identified
#   NULL                — undetermined
SCOPE_GLOBAL = "global"
SCOPE_PARTITION_FAMILY = "partition_family"


def dataset_scope_value(verdict: ScopeVerdict) -> str | None:
    """Map a ScopeVerdict to its ``kb_table_meta.dataset_scope`` value."""
    if verdict.level == "global":
        return SCOPE_GLOBAL
    if verdict.level == "partitioned":
        if verdict.partition_key:
            return f"partition:{verdict.partition_key}"
        return SCOPE_PARTITION_FAMILY
    return None


def effective_classification(
    table_meta,
    overlay=None,
) -> dict:
    """The verdict consumers (2B-2 retrieval, 2B-3 gate) must use.

    Review points 2+3 (2026-10-30), as a single tested contract:

    - A human overlay override ALWAYS wins (``schema_class_override`` /
      ``dataset_scope_override``) and is sourced ``"human"`` with
      confidence 1.0 — a human verdict is a fact, not an inference.
    - Auto columns are sourced ``"inferred"`` and carry their STORED
      confidence — a 0.6-confidence inference must remain visibly weaker
      than a human verdict (2B-2 ranking must weight by this).
    - Absent evidence → source ``None`` (value None) — never an invention.
    - The overlay's legacy ``scope`` column is NOT consulted here: it means
      something else entirely (table/kb/table_role).

    ``table_meta``/``overlay`` may be ORM rows or plain objects with the
    same attributes; both optional, though at least one is expected.
    """
    result: dict = {
        "schema_class": None,
        "schema_class_source": None,
        "schema_class_confidence": None,
        "dataset_scope": None,
        "dataset_scope_source": None,
        "dataset_scope_confidence": None,
    }
    o = overlay
    m = table_meta

    human_class = getattr(o, "schema_class_override", None) if o else None
    if human_class:
        result["schema_class"] = str(human_class)
        result["schema_class_source"] = "human"
        result["schema_class_confidence"] = 1.0
    else:
        auto_class = getattr(m, "schema_class", None) if m else None
        if auto_class:
            result["schema_class"] = str(auto_class)
            result["schema_class_source"] = "inferred"
            conf = getattr(m, "schema_class_confidence", None)
            result["schema_class_confidence"] = (
                float(conf) if conf is not None else None
            )

    human_scope = getattr(o, "dataset_scope_override", None) if o else None
    if human_scope:
        result["dataset_scope"] = str(human_scope)
        result["dataset_scope_source"] = "human"
        result["dataset_scope_confidence"] = 1.0
    else:
        auto_scope = getattr(m, "dataset_scope", None) if m else None
        if auto_scope:
            result["dataset_scope"] = str(auto_scope)
            result["dataset_scope_source"] = "inferred"
            conf = getattr(m, "dataset_scope_confidence", None)
            result["dataset_scope_confidence"] = (
                float(conf) if conf is not None else None
            )
    return result


def resolve_question_scope(
    question: str,
    verdicts: dict[str, ScopeVerdict],
) -> dict | None:
    """Match the question against OBSERVED partition values (never a vocab).

    Returns ``{"partition_key", "value", "tables"}`` when a token of the
    question equals an observed partition value, else ``None`` — an
    unresolvable question imposes NO scope constraint.
    """
    q_tokens = _question_tokens(question)
    if not q_tokens:
        return None
    best: dict | None = None
    for table, v in verdicts.items():
        if v.level != "partitioned" or not v.partition_value:
            continue
        if v.partition_value.lower() in q_tokens:
            entry = best or {"partition_key": v.partition_key, "value": v.partition_value, "tables": []}
            if table not in entry["tables"]:
                entry["tables"].append(table)
            best = entry
    return best


def _question_tokens(question: str) -> set[str]:
    return {
        t for t in re.split(r"[^a-z0-9]+", str(question or "").lower()) if t
    }


def resolve_question_scope_from_facts(
    question: str,
    facts: dict,
) -> dict | None:
    """Facts-dict variant of ``resolve_question_scope`` for retrieval.

    ``facts`` is the ``effective_classification`` shape produced by
    ``table_retrieval._load_classification_facts``:
    ``{table: {"dataset_scope": ..., "partition_value": ..., ...}}``.
    A question matching an observed partition value scopes DOWN to it;
    anything else (including "company-wide" phrasings, which carry no
    vocabulary meaning here) imposes NO constraint.
    """
    q_tokens = _question_tokens(question)
    if not q_tokens:
        return None
    best: dict | None = None
    for table, f in (facts or {}).items():
        value = str(f.get("partition_value") or "").strip().lower()
        if not value or value not in q_tokens:
            continue
        entry = best or {
            "value": value,
            "tables": [],
        }
        if table not in entry["tables"]:
            entry["tables"].append(table)
        best = entry
    return best


def _fact_for(facts: dict, name: str) -> dict:
    """Case-insensitive facts lookup (SQL identifiers arrive lowercased)."""
    f = facts.get(name)
    if f is not None:
        return f
    low = str(name).lower()
    for key, val in facts.items():
        if str(key).lower() == low:
            return val or {}
    return {}


def validate_sql_scope(
    tables: list[str],
    question_scope: dict | None,
    facts: dict,
) -> tuple[list[str], list[str]]:
    """The pure core of the 2B-3 ``scope_authorization`` check.

    ``tables`` are the tables the FINAL statement touches; ``facts`` is the
    ``effective_classification`` shape (KB-wide, so the global-existence test
    can see tables the SQL does not touch).

    Evidence-positive only — the refusal rules, and deliberately nothing else:

    * question names a partition + the statement touches a CONFIRMED
      partition (``partition:<key>``, P2-observed key) of a DIFFERENT
      value → refuse (the figures would be the wrong partition's);
    * universal question + EVERY touched table is a confirmed partition
      (any table with unknown/absent scope breaks the condition) + a
      verified ``global`` dataset EXISTS in the catalog → refuse, naming it;
    * the same partition-only shape WITHOUT a global dataset → warning, not
      refusal (refusing would make the question unanswerable; honest
      degradation means saying so, not failing).

    Everything unknown (no fact, ``partition_family`` guess, absent
    partition_value) is allowed — an inferred classification must never
    become an unquestioned refusal. Returns ``(errors, warnings)``.
    """
    errors: list[str] = []
    warnings: list[str] = []
    touched = [str(t) for t in (tables or []) if t]
    if not touched or not facts:
        return errors, warnings
    asked = str((question_scope or {}).get("value") or "").strip().lower()

    if asked:
        for t in touched:
            f = _fact_for(facts, t)
            ds = str(f.get("dataset_scope") or "")
            if not ds.startswith("partition:"):
                continue
            pv = str(f.get("partition_value") or "").strip().lower()
            if pv and pv != asked:
                errors.append(
                    f"table {t!r} holds partition {pv!r} data, but the "
                    f"question asks about {asked!r} — the statement would "
                    "report a different partition's figures"
                )
        return errors, warnings

    # Universal question: convict only when EVERY touched table is a
    # confirmed partition (a table missing from facts is unknown → allowed).
    scoped = [
        t for t in touched
        if str((_fact_for(facts, t).get("dataset_scope") or "")).startswith("partition:")
    ]
    # Reviewer table, row 3: a partition FAMILY (shape detected, key NOT
    # identified) is positive evidence of partial data. It must not silently
    # claim completeness — warn (never refuse: the key is unverified, and
    # refusing on a guess would make an inference an unquestioned fact) —
    # unless a VERIFIED global dataset is also touched, which makes
    # completeness achievable.
    families = [
        t for t in touched
        if str((_fact_for(facts, t).get("dataset_scope") or "")) == SCOPE_PARTITION_FAMILY
    ]
    touched_global = any(
        str((_fact_for(facts, t).get("dataset_scope") or "")) == SCOPE_GLOBAL
        for t in touched
    )
    if families and not touched_global:
        warnings.append(
            "the statement reads "
            + ", ".join(f"{t!r}" for t in sorted(set(families)))
            + " whose scope could not be fully verified — probable "
            "partition data; for a company-wide question the figures may "
            "not be complete"
        )
    if not scoped or len(scoped) != len(touched):
        return errors, warnings
    global_tables = sorted(
        name for name, f in facts.items()
        if str((f or {}).get("dataset_scope") or "") == SCOPE_GLOBAL
    )
    if global_tables:
        errors.append(
            "the question is company-wide but the statement reads only "
            + ", ".join(f"{t!r}" for t in sorted(set(scoped)))
            + " — single-partition data cannot answer it; the company-wide "
            "dataset(s) " + ", ".join(f"{g!r}" for g in global_tables)
            + " exist in this catalog"
        )
    else:
        warnings.append(
            "company-wide question answered from partition table(s) "
            + ", ".join(sorted(set(scoped)))
            + "; no company-wide dataset exists in this catalog, so the "
            "figures cover those partitions only"
        )
    return errors, warnings
