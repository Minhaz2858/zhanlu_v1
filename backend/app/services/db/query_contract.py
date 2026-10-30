"""Dynamic NL2SQL query contract (2026-10-29 root-cause fix).

Three defects this module closes, all observed on a live MySQL warehouse
(conv e70fa696, 2026-10-29) and all generic to ANY user-connected database:

RC-1  Verified metric definitions (``metric_definitions``) were write-only
      metadata: editable in the UI, never consulted by SQL generation or
      validation. One node computed "revenue" as SUM(FAMOUNT) (the verified
      ``net_revenue``), others as SUM(FALLAMOUNT) — the tax-inclusive column —
      and synthesis blended 105.90M with a 93.87M-basis product table.
      → ``datasource_metric_bindings`` + ``validate_sql_measure_aliases``
      (wired into ``execution_gate.enforce``) turn verified definitions into
      deterministic, pre-execution constraints keyed by datasource_id.

RC-2  A grouped top-N query at key grain (material_code) truncated product
      families spanning multiple codes: pentane reported 10.24M while its
      visible family rows already summed to ≥14.81M.
      → ``detect_family_truncation`` + ``build_family_correction_sql``:
      deterministic detection of repeated display names inside a LIMITed
      grouped result, bounded same-shape re-query at the family grain, or an
      explicit disclosure when correction is not safely constructible.

RC-3  Synthesis blended results from different measures without detection.
      → ``measure_fingerprint`` + ``reconcile_fingerprints``: every result
      carries a structured fingerprint (claimed concept → column, tables,
      grouping); cross-node conflicts surface before synthesis.

Generality invariants (no per-database knowledge):
- bindings come from the datasource's own catalog rows (name, synonyms,
  description, base_table/base_column) — the engine never names a column;
- SQL inspection uses the shared sqlglot parse (dialect-aware, same parse the
  execution gate already runs for read-only/table/column checks);
- everything is scoped by (org, datasource_id) — a binding from datasource A
  can never influence datasource B;
- an ambiguous concept with no verified definition is NOT guessed: the
  contract simply has no binding, and the SQL prompt says so.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _norm_text(value: str | None) -> str:
    """Lowercase, underscores/hyphens → spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[_\-]+", " ", str(value or "").lower())).strip()


def _norm_sql(value: str | None) -> str:
    return str(value or "").strip().lower()


# ---------------------------------------------------------------------------
# Bindings (Phase 1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricBinding:
    """One verified metric definition, resolved for a datasource."""

    term: str                 # canonical term, e.g. "net_revenue"
    display: str              # display name
    base_table: str
    base_column: str
    aggregation: str          # e.g. "sum"
    description: str = ""
    synonyms: tuple[str, ...] = field(default=())
    # Columns of the base table that the binding's own description mentions
    # by name and that are NOT the verified column — the catalog documents
    # them as other bases of the same business concept (e.g. a description
    # "FAMOUNT excluding tax; FALLAMOUNT is tax-inclusive" makes FALLAMOUNT
    # confusable with the verified FAMOUNT). Derived deterministically from
    # the datasource's own catalog text; empty when none.
    confusable_columns: tuple[str, ...] = field(default=())

    @property
    def match_phrases(self) -> tuple[str, ...]:
        """Phrases that claim this concept in an output alias."""
        phrases = [self.term, self.display, *self.synonyms]
        return tuple(sorted({_norm_text(p) for p in phrases if _norm_text(p)}))


def datasource_metric_bindings(db, kb_id: str) -> list[MetricBinding]:
    """Verified metric definitions bound to THIS datasource (org-scoped).

    Generic: reads whatever the user's catalog defines. A datasource with no
    definitions yields an empty list — nothing is enforced, nothing invented.
    """
    if db is None or not kb_id:
        return []
    try:
        from app.models.metric_definition import MetricDefinition

        rows = (
            db.query(MetricDefinition)
            .filter(
                MetricDefinition.is_enabled.is_(True),
                MetricDefinition.datasource_id == kb_id,
                MetricDefinition.is_deleted.is_(False),
                # P5-T1: only PUBLISHED metrics constrain NL2SQL. Draft
                # (awaiting review) and retired definitions must never
                # resolve vocabulary or reject queries.
                MetricDefinition.lifecycle == "published",
            )
            # Deterministic conflict resolution (P0-6): the oldest authored
            # row wins; without an order the "first" binding depended on
            # engine row order, making alias validation non-deterministic.
            .order_by(MetricDefinition.created_date, MetricDefinition.id)
            .all()
        )
    except Exception as e:  # pragma: no cover — catalog unavailable → no bindings
        # P0-6: this branch silently disabled ALL metric enforcement on a
        # catalog hiccup — that silence is exactly how enforcement drifts.
        logger.warning(
            "query_contract: metric bindings unavailable for %s (%s) — "
            "metric enforcement is OFF for this call",
            kb_id, e,
        )
        return []
    bindings: list[MetricBinding] = []
    _doc_cache: dict[str, set[str]] = {}
    # P0-6 consumption integrity: one concept, one binding. Conflicting
    # duplicates (same term, different basis) previously BOTH shipped —
    # contradictory BINDING constraints in one prompt and order-dependent
    # alias validation. Identical duplicates collapse; the first row wins.
    _seen_terms: dict[str, MetricBinding] = {}
    # P0-6 staleness guard: a binding whose base table no longer exists in
    # the indexed catalog (renamed/deleted table) previously kept shipping
    # and the alias validator rejected every query touching the old name.
    # The catalog is the truth for what can be queried — skip dead rows.
    _catalog_tables = _catalog_table_names(db, kb_id)
    for r in rows:
        term = str(getattr(r, "name", "") or "").strip()
        if not term or not _norm_sql(getattr(r, "base_column", "")):
            continue
        synonyms = tuple(_json_list(getattr(r, "synonyms", None)))
        description = str(getattr(r, "description", "") or "")
        base_table = _norm_sql(getattr(r, "base_table", ""))
        base_column = _norm_sql(r.base_column)
        if base_table and _catalog_tables is not None and base_table not in _catalog_tables:
            logger.warning(
                "query_contract: metric %r references table %r which is not "
                "in the indexed catalog for %s — skipping stale binding",
                term, base_table, kb_id,
            )
            continue
        # Blocker-1 enforcement basis: columns the catalog itself documents
        # for the base table AND that the binding's description names as a
        # different basis of this concept. One small indexed lookup per
        # distinct base table, cached within this call.
        confusable: tuple[str, ...] = ()
        if base_table and description:
            if base_table not in _doc_cache:
                _doc_cache[base_table] = _documented_columns(db, kb_id, base_table)
            # Normalized (lower) — the validator compares against sqlglot's
            # normalized column names, and real warehouses mix case (FAMOUNT).
            confusable = tuple(sorted({
                _norm_sql(c) for c in _doc_cache[base_table]
                if _norm_sql(c) != base_column
                and _description_mentions(description, c)
            }))
        binding = MetricBinding(
            term=term,
            display=str(getattr(r, "display_name", "") or r.name),
            base_table=base_table,
            base_column=base_column,
            aggregation=_norm_sql(getattr(r, "aggregation", "")) or "sum",
            description=description,
            synonyms=synonyms,
            confusable_columns=confusable,
        )
        prior = _seen_terms.get(term.lower())
        if prior is not None:
            same = (
                prior.base_table == binding.base_table
                and prior.base_column == binding.base_column
                and prior.aggregation == binding.aggregation
            )
            if same:
                continue  # identical duplicate — collapse silently
            logger.warning(
                "query_contract: conflicting metric definitions for %r in %s "
                "(%s.%s vs %s.%s) — keeping the older definition",
                term, kb_id, prior.base_table, prior.base_column,
                binding.base_table, binding.base_column,
            )
            continue
        _seen_terms[term.lower()] = binding
        bindings.append(binding)
    return bindings


def _catalog_table_names(db, kb_id: str) -> set[str] | None:
    """Normalized table names in the indexed catalog, or None when unknown.

    ``None`` (catalog unreadable) disables the staleness skip rather than
    disabling the metrics — degrade the guard, not the enforcement.
    """
    if db is None or not kb_id:
        return None
    try:
        from sqlalchemy import func as _func

        from app.models.knowledge_catalog import KBTableMeta

        rows = (
            db.query(_func.lower(KBTableMeta.table_name))
            .filter(
                KBTableMeta.kb_id == kb_id,
                KBTableMeta.is_deleted.is_(False),
            )
            .all()
        )
        return {str(r[0]).strip().lower() for r in rows if r and r[0]}
    except Exception:  # pragma: no cover — catalog hiccup
        return None


def _json_list(raw) -> tuple[str, ...]:
    import json

    if isinstance(raw, (list, tuple)):
        items = raw
    elif raw:
        try:
            items = json.loads(raw)
        except Exception:
            items = []
    else:
        items = []
    return tuple(str(x).strip() for x in items if str(x).strip())


def _documented_columns(db, kb_id: str, table_norm: str) -> set[str]:
    """Column names the datasource's own catalog documents for a table.

    Reads kb_table_meta / kb_column_meta (the indexed schema inventory —
    source comments and descriptions the user's database itself provides).
    Generic: whatever the catalog holds; nothing hardcoded.
    """
    if db is None or not kb_id or not table_norm:
        return set()
    try:
        from sqlalchemy import func as _sa_func

        from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

        rows = (
            db.query(KBColumnMeta.column_name)
            .join(KBTableMeta, KBColumnMeta.table_meta_id == KBTableMeta.id)
            .filter(
                KBTableMeta.kb_id == kb_id,
                _sa_func.lower(KBTableMeta.table_name) == table_norm,
            )
            .all()
        )
        return {str(r[0]).strip() for r in rows if str(r[0] or "").strip()}
    except Exception:  # pragma: no cover — catalog unavailable → no confusables
        return set()


def _description_mentions(description: str, column: str) -> bool:
    """True when the catalog text names a column verbatim (word-bounded,
    case-insensitive). len>=4 keeps single-letter/short tokens from
    matching prose coincidentally."""
    if not description or not column or len(column) < 4:
        return False
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])",
        description,
        re.IGNORECASE,
    ) is not None


def _alias_claims(alias_norm: str, binding: MetricBinding) -> bool:
    """True when an output alias claims the metric's concept.

    Word-boundary phrase containment on normalized text: the alias
    "current week revenue" claims "revenue" (a synonym of net_revenue);
    "order count" claims nothing.
    """
    padded = f" {alias_norm} "
    return any(
        len(p) >= 3 and f" {p} " in padded for p in binding.match_phrases
    )


def validate_sql_measure_aliases(
    bindings: list[MetricBinding],
    parsed,
    tables: list[str],
    alias_map: dict[str, str] | None = None,
) -> list[str]:
    """Deterministic pre-execution check: a query whose output alias claims a
    verified metric concept but computes it from a different column is a
    violation — rejected before execution with an honest, actionable message.

    sqlglot-based (dialect-aware — the same parse the execution gate already
    trusts); no MySQL-only assumptions; no column-name hardcoding.
    """
    if not bindings or parsed is None:
        return []
    from sqlglot import exp

    alias_map = alias_map or {}
    # Aggregate expressions in the SELECT list: (agg_fn, column, out_alias).
    claims: list[tuple[str, str, str, str]] = []
    for select in parsed.find_all(exp.Select):
        for proj in select.expressions:
            if not isinstance(proj, exp.Alias) or not getattr(proj, "alias", None):
                continue
            alias_norm = _norm_text(str(proj.alias))
            if not alias_norm:
                continue
            for agg in proj.find_all(exp.AggFunc):
                col = agg.find(exp.Column)
                if col is None:
                    continue
                table_ref = str(col.table or "").lower()
                table_name = alias_map.get(table_ref, table_ref)
                claims.append((
                    alias_norm,
                    str(col.name),
                    _norm_sql(table_name),
                    type(agg).__name__.lower(),
                ))
    if not claims:
        return []

    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    for b in bindings:
        for alias_norm, col, tbl, _fn in claims:
            col_norm = _norm_sql(col)
            if col_norm == b.base_column:
                continue  # compliant: the verified column itself
            if not _alias_claims(alias_norm, b):
                continue  # this output does not claim the verified concept
            # Table scope: binding applies to its own table; a single-table
            # query is unambiguous, multi-table requires the same table.
            if b.base_table and tbl and tbl != b.base_table and len(tables) > 1:
                continue
            key = (b.term, col_norm)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                f"the data source's catalog defines '{b.term or b.display}' as "
                f"{b.aggregation}({b.base_column}) on {b.base_table or 'its base table'} "
                f"(verified metric). This query computes '{alias_norm}' from "
                f"{col_norm} instead. Use {b.base_column} for '{b.term}', or rename "
                f"the output so it does not claim to be '{b.display or b.term}'."
            )
    return violations


def validate_confusable_measures(
    bindings: list[MetricBinding],
    parsed,
    tables: list[str],
    alias_map: dict[str, str] | None = None,
) -> list[str]:
    """Alias-INDEPENDENT counterpart to :func:`validate_sql_measure_aliases`.

    The alias rule only fires when an output column CLAIMS the verified
    concept. The incident also had queries whose alias did not claim
    anything yet aggregated the tax-inclusive sibling column in place of
    the verified one. When a binding's own catalog description documents
    such a confusable column (see ``MetricBinding.confusable_columns``),
    aggregating it WITHOUT also aggregating the verified column anywhere
    in the statement is a violation — the report would present a different
    measure under an unclaimed name, which is exactly the RC-1 defect.

    Escape hatches, both deterministic:
    - the query also aggregates the verified base_column (an explicit
      multi-basis comparison — conflicts are then caught by cross-node
      reconciliation, not silently);
    - the aggregated column does not belong to the binding's base table
      in a multi-table query.
    """
    if not bindings or parsed is None:
        return []
    from sqlglot import exp

    alias_map = alias_map or {}
    aggs: list[tuple[str, str]] = []
    for agg in parsed.find_all(exp.AggFunc):
        col = agg.find(exp.Column)
        if col is None:
            continue
        table_ref = str(col.table or "").lower()
        tbl = _norm_sql(alias_map.get(table_ref, table_ref))
        aggs.append((_norm_sql(col.name), tbl))
    if not aggs:
        return []

    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    for b in bindings:
        if not b.confusable_columns:
            continue
        used_conf = [(c, t) for c, t in aggs if c in b.confusable_columns]
        if not used_conf:
            continue
        uses_verified = any(
            c == b.base_column
            and (not b.base_table or not t or t == b.base_table or len(tables) <= 1)
            for c, t in aggs
        )
        if uses_verified:
            continue  # explicit multi-basis query — reconciliation's job
        for col, tbl in used_conf:
            if b.base_table and tbl and tbl != b.base_table and len(tables) > 1:
                continue
            key = (b.term, col)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                f"the data source's catalog defines '{b.term or b.display}' as "
                f"{b.aggregation}({b.base_column}) on {b.base_table or 'its base table'} "
                f"(verified metric). This query instead aggregates '{col}', which the "
                f"catalog's own description of '{b.term}' documents as a different basis "
                f"of that concept. A report built on '{col}' is not '{b.term or b.display}'. "
                f"Use {b.base_column} for '{b.term}', or aggregate BOTH columns with "
                f"explicitly labeled outputs so the bases cannot be blended."
            )
    return violations


def contract_prompt_block(bindings: list[MetricBinding]) -> str:
    """Constraint block for the SQL-generation prompt. Empty when the
    datasource defines no verified metrics — the prompt then says nothing,
    and no binding is invented."""
    if not bindings:
        return ""
    lines = [
        "<verified-metrics>",
        "The data source's catalog defines these business measures. They are",
        "BINDING constraints, not suggestions — using a different column for a",
        "bound measure will be rejected before execution:",
    ]
    for b in bindings:
        line = f"- {b.term} = {b.aggregation}({b.base_column}) on {b.base_table}"
        if b.description:
            line += f" — {b.description}"
        if b.synonyms:
            line += f" (also answers to: {', '.join(b.synonyms)})"
        lines.append(line)
    lines.append("</verified-metrics>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fingerprints + cross-node reconciliation (Phase 3)
# ---------------------------------------------------------------------------

def measure_fingerprint(parsed, tables: list[str]) -> dict:
    """Structured, engine-neutral fingerprint of what a query measures:
    which tables, which (aggregation, column) pairs, grouped by what, and its
    row cap. Consumed by cross-node reconciliation before synthesis."""
    fp: dict = {"tables": sorted(_norm_sql(t) for t in (tables or []))}
    if parsed is None:
        return fp
    from sqlglot import exp

    aggs: set[str] = set()
    for agg in parsed.find_all(exp.AggFunc):
        col = agg.find(exp.Column)
        if col is not None:
            aggs.add(f"{type(agg).__name__.lower()}:{_norm_sql(col.name)}")
    fp["aggs"] = sorted(aggs)
    group = parsed.find(exp.Group)
    if group is not None:
        fp["group_by"] = [
            _norm_sql(c.name) if isinstance(c, exp.Column) else _norm_text(c.sql())
            for c in group.expressions
        ]
    limit = parsed.find(exp.Limit)
    if limit is not None:
        try:
            fp["limit"] = int(limit.expression.name)
        except Exception:
            pass
    return fp


def claimed_measure_terms(parsed, bindings: list[MetricBinding]) -> dict[str, str]:
    """{metric_term → column actually used} for every SELECT alias that claims
    a verified concept. The cross-node conflict signal: two nodes claiming the
    same term from different columns are the incident's exact defect."""
    out: dict[str, str] = {}
    if parsed is None or not bindings:
        return out
    from sqlglot import exp

    for select in parsed.find_all(exp.Select):
        for proj in select.expressions:
            if not isinstance(proj, exp.Alias) or not getattr(proj, "alias", None):
                continue
            alias_norm = _norm_text(str(proj.alias))
            for agg in proj.find_all(exp.AggFunc):
                col = agg.find(exp.Column)
                if col is None:
                    continue
                for b in bindings:
                    if _alias_claims(alias_norm, b):
                        out.setdefault(b.term, _norm_sql(col.name))
    return out


def reconcile_fingerprints(claimed_list: list[dict[str, str]]) -> list[str]:
    """Cross-node reconciliation: same claimed concept + different columns →
    conflict descriptions. Empty list = all nodes agree (or nothing claimed)."""
    per_term: dict[str, set[str]] = {}
    for claimed in claimed_list or []:
        if not isinstance(claimed, dict):
            continue
        for term, col in claimed.items():
            per_term.setdefault(term, set()).add(_norm_sql(col))
    conflicts: list[str] = []
    for term, cols in sorted(per_term.items()):
        if len(cols) > 1:
            conflicts.append(
                f"results disagree on '{term}': computed from "
                + " vs ".join(sorted(f"'{c}'" for c in cols))
                + " — these are different measures and must not be blended"
            )
    return conflicts


# ---------------------------------------------------------------------------
# Grouping-grain / LIMIT family truncation (Phase 2)
# ---------------------------------------------------------------------------

def detect_family_truncation(sql: str, rows: list) -> dict | None:
    """Detect a grouped+LIMIT result whose display names repeat across rows —
    the signature of a product family spanning multiple grouping keys, where
    keys beyond the LIMIT silently understate the family total.

    Returns {column, families} when detected, else None. Generic: works on
    any result whose display column repeats; no domain knowledge.
    """
    if not sql or not isinstance(rows, list) or len(rows) < 2:
        return None
    low = _norm_sql(sql)
    if " group by" not in low or " limit" not in low:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    for col, _v in first.items():
        values: dict[str, int] = {}
        ok = True
        for r in rows:
            if not isinstance(r, dict) or col not in r:
                ok = False
                break
            v = r.get(col)
            if not isinstance(v, str) or not v.strip():
                ok = False
                break
            values[_norm_text(v)] = values.get(_norm_text(v), 0) + 1
        if not ok:
            continue
        repeated = {k: c for k, c in values.items() if c > 1}
        if repeated:
            return {"column": col, "families": repeated}
    return None


def build_family_correction_sql(sql: str, family_col: str) -> str | None:
    """Bounded, deterministic correction: same query regrouped at the family
    grain (the repeated display column) instead of the key grain. Returns None
    when the statement is not the simple, safe shape this rewrite supports —
    the caller then discloses instead of guessing."""
    try:
        import sqlglot
        from sqlglot import exp

        parsed = sqlglot.parse_one(sql)
        select = parsed.find(exp.Select)
        group = parsed.find(exp.Group)
        if select is None or group is None:
            return None
        # The family column must already be projected (it is the display the
        # report shows) and must be a plain column reference.
        projected = [
            p for p in select.expressions
            if isinstance(p, exp.Alias) and _norm_text(str(p.alias)) == _norm_text(family_col)
        ] or [
            p for p in select.expressions
            if isinstance(p, exp.Column) and _norm_sql(p.name) == _norm_sql(family_col)
        ]
        if not projected:
            return None
        family_expr = projected[0].this if isinstance(projected[0], exp.Alias) else projected[0]
        if not isinstance(family_expr, exp.Column):
            return None
        group.set("expressions", [family_expr.copy()])
        # Aggregations are preserved as-authored; ordering key stays valid.
        return parsed.sql()
    except Exception:
        return None
