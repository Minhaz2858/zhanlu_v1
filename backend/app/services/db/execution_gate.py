"""The checks every statement owes the database — run unconditionally.

The seam this closes: validation used to live *above* the execution boundary, in
whichever caller remembered to ask for it. ``execute_query`` ran a schema check
before calling through; the dashboards, the forecasting probes, the report
recipes and the data tools did not. Anything that reached
:meth:`QueryService.execute` directly reached the database directly, so a
model-authored table or column nobody had vouched for was simply executed.

Validation is now part of the boundary itself. Every caller — the sealed NL path,
the agent's execute_query tool, an alternate agent, a report recipe, a dashboard —
gets the same checks, and no flag, context or caller can switch them off:

* **source authorization** — the datasource exists, is not deleted, and is a
  database (not a file/API source dressed as one);
* **dialect validity** — the statement parses in the dialect the source actually
  speaks, and is not a write/DDL statement;
* **table existence** and **column existence** — against the source's own
  described schema;
* **join reachability** — every join is looked at: its keys must exist on both
  sides, and the relation is recorded as catalog-backed or not;
* **policy** — blocked tables are refused, and an allow-list is honoured;
* **row filters** — a configured filter whose column is absent from the statement
  is refused;
* **metric/ratio contract** — the plan's declared contracts must be internally
  consistent before the arithmetic is executed.

Severity is deliberate and stated per check in the payload:

* an *unprovable* check (introspection unavailable for this source, no policy in
  scope) is recorded as ``unavailable``/``not-in-scope`` — never as ``pass``;
* an unbacked relation is recorded (``unbacked``) rather than refused: a join on
  a shared code column is legitimate, and the grounding layer already treats an
  unvalidated relation as evidence, not as guilt;
* a *broken* check (missing table, missing column, unsendable dialect, write
  statement, blocked table, absent row filter) refuses the statement before the
  driver sees it.

Nothing here selects a source or routes a question: it reads the source the
caller already bound. ``NL2SQL_SHADOW_MODE_ENABLED`` stays a telemetry switch for
the shadow comparison log and cannot affect any of this.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping as _Mapping
from collections.abc import Sequence as _Sequence
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

#: Every check, in the order they run. The payload always carries all of them.
EXECUTION_CHECKS = (
    "source_authorization",
    "read_only",
    "dialect_validity",
    "table_existence",
    "column_existence",
    "join_reachability",
    "policy",
    "row_filter",
    "metric_contract",
)

#: Statements that speak to the engine's own objects. They are read-only by
#: construction and carry no business table to validate.
_INTROSPECTION = re.compile(
    r"^\s*(show|describe|desc|explain|pragma|analyze)\b", re.IGNORECASE
)


class ExecutionValidationError(ValueError):
    """A statement refused before the database received it.

    Carries the structured payload (``.payload``) so the caller can put it on a
    GroundedPlan / CanonicalResultSet instead of reducing it to a string. The
    message is phrased like a database naming failure on purpose: a missing table
    or column IS a naming failure the SQL model can repair, and the NL path's
    existing repair loop keys off exactly that wording.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = dict(payload)
        errors = self.payload.get("errors") or []
        detail = self.payload.get("detail") or {}
        hints = [str(d) for d in (detail.get("hints") or [])][:3]
        message = "; ".join(str(e) for e in errors) or "execution validation failed"
        if hints:
            message += " — did you mean: " + ", ".join(hints)
        super().__init__(message + " (the statement was not sent to the database)")


def _load_source(db: Session, kb_id: str) -> KnowledgeBase:
    """Source authorization: the datasource must exist, live, and be a database."""
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise ExecutionValidationError(
            {
                "ok": False,
                "checks": {name: "fail" if name == "source_authorization" else "unknown" for name in EXECUTION_CHECKS},
                "errors": [f"data source {kb_id!r} does not exist or has been deleted"],
                "detail": {"hints": []},
                "source_id": kb_id,
                "authored_sql": "",
                "executed_sql": "",
                "enforced": True,
                "flag_effect": "observability-only",
            }
        )
    if (kb.source_kind or "").lower() != "database":
        raise ExecutionValidationError(
            {
                "ok": False,
                "checks": {name: "fail" if name == "source_authorization" else "unknown" for name in EXECUTION_CHECKS},
                "errors": [
                    f"data source {kb.name!r} is not a database source "
                    f"(source_kind={kb.source_kind!r})"
                ],
                "detail": {"hints": []},
                "source_id": kb_id,
                "authored_sql": "",
                "executed_sql": "",
                "enforced": True,
                "flag_effect": "observability-only",
            }
        )
    return kb


def _known_tables(db: Session, kb_id: str) -> set[str]:
    """The tables the source itself reports — empty when it cannot be asked."""
    try:
        from app.services.db.schema_service import SchemaService

        listed = SchemaService(db).list_tables(kb_id) or {}
    except Exception as e:  # pragma: no cover - source-specific introspection
        logger.debug("execution gate: list_tables failed: %s", e)
        return set()
    names: set[str] = set()
    for entry in listed.get("tables") or []:
        name = entry.get("table") if isinstance(entry, _Mapping) else entry
        if name:
            names.add(str(name).split(".")[-1].lower())
    return names


def _described_columns(db: Session, kb_id: str, table: str) -> list[str] | None:
    """``None`` when the source cannot describe the table (never a conviction)."""
    try:
        from app.services.db.schema_service import SchemaService

        described = SchemaService(db).describe_table(kb_id, table) or {}
    except Exception as e:
        logger.debug("execution gate: describe %s failed: %s", table, e)
        return None
    columns = [
        str(c.get("name")).lower()
        for c in (described.get("columns") or [])
        if isinstance(c, _Mapping) and c.get("name")
    ]
    return columns or None


def _parse(sql: str, dialect: str):
    import sqlglot

    return sqlglot.parse_one(sql, dialect=dialect)


def _dialect_for(kb: KnowledgeBase) -> str:
    from app.services.db.schema_graph import _map_dialect

    return _map_dialect(kb.db_type)


def _policy_decisions(policy: Any, kb_id: str) -> tuple[set[str], set[str]]:
    """``(blocked, allowed)`` from the caller's policy — empty when none is scope."""
    if policy is None:
        return set(), set()
    blocked: set[str] = set()
    allowed: set[str] = set()
    for attr, target in (
        ("blocked_tables_for_kb", blocked),
        ("allowed_tables_for_kb", allowed),
    ):
        getter = getattr(policy, attr, None)
        if not callable(getter):
            continue
        try:
            target.update(str(t).split(".")[-1].lower() for t in (getter(kb_id) or []))
        except Exception as e:  # pragma: no cover - policy shapes vary
            logger.debug("execution gate: %s failed: %s", attr, e)
    return blocked, allowed


def _configured_row_filters(db: Session, kb_id: str) -> list[Any]:
    """Row filters configured for this datasource, if any can be read."""
    try:
        from app.models.agent_data_binding import AgentDataBinding

        # ``datasource_id`` is the column bindings carry; the id it holds is the
        # source the binding grants access to (a KnowledgeBase id on this
        # platform). Querying a ``kb_id`` column that does not exist raised
        # AttributeError, was swallowed, and read as "no filters configured" —
        # i.e. a configured filter was never verified.
        rows = (
            db.query(AgentDataBinding)
            .filter(AgentDataBinding.datasource_id == kb_id)
            .all()
        )
    except Exception as e:  # pragma: no cover - table optional in some installs
        logger.debug("execution gate: row-filter lookup unavailable: %s", e)
        return []
    out: list[Any] = []
    for row in rows:
        filters = getattr(row, "row_filters", None)
        if isinstance(filters, str):
            try:
                import json

                filters = json.loads(filters)
            except Exception:
                filters = None
        if isinstance(filters, (list, _Sequence)):
            out.extend(filters)
    return out


def _filter_columns(filters: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for entry in filters:
        if isinstance(entry, _Mapping):
            for key in ("column", "filter"):
                value = str(entry.get(key) or "")
                if key == "filter" and value:
                    match = re.search(r"[`\"\[]?([A-Za-z_][\w]*)[`\"\]]?\s*(=|>|<|in|like)", value, re.I)
                    if match:
                        out.append(match.group(1).lower())
                elif value:
                    out.append(value.split(".")[-1].lower())
    return [c for c in out if c]


def enforce(
    db: Session,
    kb_id: str,
    sql: str,
    *,
    policy: Any = None,
    plan: Any = None,
    row_filters: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Run every execution check. Raises :class:`ExecutionValidationError` on any broken one.

    Returns the payload (``ok``, per-check state, evidence) on success so the
    caller can record what was actually verified rather than assert that
    "validation ran".
    """
    kb = _load_source(db, kb_id)
    checks: dict[str, str] = {name: "unknown" for name in EXECUTION_CHECKS}
    errors: list[str] = []
    detail: dict[str, Any] = {
        "hints": [],
        "source": kb.name,
        "db_type": kb.db_type,
        "tables": [],
        "joins": [],
        "row_filters": [],
    }
    checks["source_authorization"] = "pass"

    statement = (sql or "").strip()
    introspection = bool(_INTROSPECTION.match(statement))
    dialect = ""
    parsed = None
    if introspection:
        checks["dialect_validity"] = "pass"
        checks["read_only"] = "pass"
        checks["table_existence"] = "not-applicable"
        checks["column_existence"] = "not-applicable"
        checks["join_reachability"] = "not-applicable"
        detail["introspection"] = True
    else:
        try:
            dialect = _dialect_for(kb)
        except Exception as e:
            errors.append(f"the source's dialect could not be identified: {e}")
            checks["dialect_validity"] = "fail"
        if dialect:
            try:
                parsed = _parse(statement, dialect)
                checks["dialect_validity"] = "pass"
            except Exception as e:
                checks["dialect_validity"] = "fail"
                errors.append(f"the statement is not valid {dialect} SQL: {e}")

    tables: list[str] = []
    if parsed is not None:
        from app.services.nl2sql.schema_validator import (
            _READ_ONLY_ERROR,
            _extract_alias_map,
            _extract_tables,
            _is_write_or_ddl,
        )

        if _is_write_or_ddl(parsed):
            checks["read_only"] = "fail"
            errors.append(_READ_ONLY_ERROR)
        else:
            checks["read_only"] = "pass"
        from sqlglot import exp

        cte_names = {
            str(cte.alias).lower()
            for cte in parsed.find_all(exp.CTE)
            if getattr(cte, "alias", None)
        }
        tables = sorted(t for t in _extract_tables(parsed) if t.lower() not in cte_names)
        detail["tables"] = tables

        known = _known_tables(db, kb_id)
        if not known:
            checks["table_existence"] = "unavailable"
            checks["column_existence"] = "unavailable"
            detail["hints"].append("this source could not be introspected for table names")
        else:
            unknown = [t for t in tables if t.lower() not in known]
            if unknown:
                checks["table_existence"] = "fail"
                errors.append(
                    "table " + ", ".join(f"{t!r}" for t in unknown) + " does not exist in this data source"
                )
                from app.services.db.schema_service import SchemaService
                from app.services.nl2sql.schema_validator import _suggest_table_matches

                try:
                    detail["hints"].extend(
                        _suggest_table_matches(SchemaService(db), kb_id, unknown[0])
                    )
                except Exception:
                    pass
            else:
                checks["table_existence"] = "pass"

            # Column existence — per referenced table, against the described schema.
            alias_map = _extract_alias_map(parsed)
            # A SELECT-list alias is not a column of the table it was computed
            # from: `SELECT STRFTIME('%Y-Q', d) AS quarter ... GROUP BY quarter`
            # is valid SQL, and reading `quarter` as `sales.quarter` refused a
            # correct query (golden case g08). Unqualified identifiers that a
            # SELECT alias defines are therefore not convicted here.
            select_aliases = {
                str(a.alias).lower()
                for a in parsed.find_all(exp.Alias)
                if getattr(a, "alias", None)
            }
            wanted: dict[str, set[str]] = {}
            for column in parsed.find_all(exp.Column):
                table_ref = column.table
                table_name = alias_map.get(str(table_ref).lower(), str(table_ref)) if table_ref else ""
                if not table_name and len(tables) == 1:
                    table_name = tables[0]
                name = column.name
                if not table_ref and str(name).lower() in select_aliases:
                    continue
                if table_name and name:
                    wanted.setdefault(str(table_name).lower(), set()).add(str(name).lower())
            unverifiable = False
            unknown_columns: list[str] = []
            for table, names in sorted(wanted.items()):
                if table not in {t.lower() for t in tables}:
                    continue  # an alias we could not resolve: the table check owns it
                columns = _described_columns(db, kb_id, table)
                if columns is None:
                    unverifiable = True
                    continue
                for name in sorted(names):
                    if name not in columns:
                        unknown_columns.append(f"{table}.{name}")
            if unknown_columns:
                checks["column_existence"] = "fail"
                errors.append(
                    "column " + ", ".join(f"{c!r}" for c in unknown_columns) + " does not exist in this data source"
                )
            elif unverifiable:
                checks["column_existence"] = "unavailable"
            else:
                checks["column_existence"] = "pass"

            # Join reachability: every join is examined; a broken key is refused,
            # an unbacked relation is recorded.
            joins, join_errors, unbacked = _join_reachability(parsed, db, kb_id, alias_map)
            detail["joins"] = joins
            if join_errors:
                checks["join_reachability"] = "fail"
                errors.extend(join_errors)
            elif unbacked:
                checks["join_reachability"] = "unbacked"
            elif joins:
                checks["join_reachability"] = "pass"
            else:
                checks["join_reachability"] = "not-applicable"

    # Policy: blocked tables are refused; an allow-list is honoured.
    blocked, allowed = _policy_decisions(policy, kb_id)
    if policy is None:
        checks["policy"] = "not-in-scope"
    else:
        refused = [t for t in tables if t.lower() in blocked]
        if refused:
            checks["policy"] = "fail"
            errors.append("this user may not read " + ", ".join(f"{t!r}" for t in refused))
        elif allowed and [t for t in tables if t.lower() not in allowed]:
            checks["policy"] = "fail"
            errors.append(
                "this user may not read "
                + ", ".join(f"{t!r}" for t in tables if t.lower() not in allowed)
            )
        else:
            checks["policy"] = "pass"

    # Row filters: a configured filter whose column is absent is a bypass.
    filters = list(row_filters) if row_filters is not None else _configured_row_filters(db, kb_id)
    columns = _filter_columns(filters)
    detail["row_filters"] = columns
    if not filters:
        checks["row_filter"] = "not-applicable"
    else:
        lowered = statement.lower()
        missing = [c for c in columns if c not in lowered]
        if missing:
            checks["row_filter"] = "fail"
            errors.append(
                "the row filter on " + ", ".join(f"{c!r}" for c in missing) + " is not applied to this statement"
            )
        else:
            checks["row_filter"] = "pass"

    # Metric/ratio contracts declared for this question must be consistent.
    contracts = list(getattr(plan, "ratio_contracts", None) or []) if plan is not None else []
    if plan is None:
        checks["metric_contract"] = "not-in-scope"
    elif not contracts:
        checks["metric_contract"] = "not-applicable"
    else:
        from app.services.db.ratio_contract import RatioContract, validate_contract

        problems: list[str] = []
        for payload in contracts:
            problems.extend(validate_contract(RatioContract.from_dict(payload)))
        if problems:
            checks["metric_contract"] = "fail"
            errors.extend(problems)
        else:
            checks["metric_contract"] = "pass"
    detail["ratio_contracts"] = len(contracts)

    payload = {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
        "detail": detail,
        "source_id": kb_id,
        "source": kb.name,
        "dialect": dialect,
        # Provenance, preserved: the statement as authored...
        "authored_sql": statement,
        # ...and what the driver received. Empty when nothing was sent.
        "executed_sql": "",
        "enforced": True,
        # The compatibility flag's only remaining meaning, stated where it is read.
        "flag_effect": "observability-only",
    }
    if errors:
        raise ExecutionValidationError(payload)
    return payload


def failure_sentence(payload: Mapping[str, Any]) -> str:
    """One reader-facing sentence for a statement the platform refused."""
    errors = [str(e) for e in (payload.get("errors") or [])]
    checks = payload.get("checks") or {}
    failed = [name for name, state in checks.items() if state == "fail"]
    if not errors:
        return (
            "This query was stopped before it reached the database: it could not be "
            "verified against the data source."
        )
    return (
        "This query was stopped before it reached the database — "
        + "; ".join(errors)
        + ". No figure is reported for it: the amount asked about is unknown, not zero."
        + (f" (checks that failed: {', '.join(failed)})" if failed else "")
    )


def failure_result_set(plan: Any, payload: Mapping[str, Any]):
    """A CanonicalResultSet for a statement refused before execution.

    No rows, no slot convictions: a refused statement says nothing about what the
    source holds, and claiming "the source has nothing for X" would be a guess.
    The payload and the disclosure are what is known.
    """
    from app.services.db.contracts import CanonicalResultSet

    sentence = failure_sentence(payload)
    source_ids = [str(s) for s in (getattr(plan, "source_ids", None) or [])]
    if not source_ids and payload.get("source_id"):
        source_ids = [str(payload["source_id"])]
    crs = CanonicalResultSet(
        complete=False,
        source_ids=source_ids,
        execution_validation=dict(payload),
        #: the same verdict shape a served result carries, marked as refused
        validation={
            "executed": False,
            "reason": "refused_before_execution",
            "checks": dict(payload.get("checks") or {}),
            "errors": [str(e) for e in (payload.get("errors") or [])],
        },
        disclosure=sentence,
        warnings=[sentence],
    )
    if plan is not None:
        try:
            crs.plan_fingerprint = plan.fingerprint()
        except Exception:  # pragma: no cover - a hand-built plan may not fingerprint
            pass
    return crs


def _join_reachability(
    parsed: Any, db: Session, kb_id: str, alias_map: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """``(joins, errors, unbacked)`` for every JOIN in the statement.

    ``errors`` are structural: a join key that does not exist on the table it is
    written against cannot be evaluated at all. ``unbacked`` records a relation
    the catalog does not know — legitimate (a shared code column) but on the
    record, which is what the grounding layer uses as evidence.
    """
    from sqlglot import exp

    joins: list[dict[str, Any]] = []
    errors: list[str] = []
    unbacked: list[dict[str, Any]] = []
    for join in parsed.find_all(exp.Join):
        target = join.this
        right = ""
        if isinstance(target, exp.Table):
            right = str(target.name or "")
        elif isinstance(target, exp.Subquery):
            right = "subquery"
        on = join.args.get("on")
        kinds = "cross" if on is None else "on"
        key_pairs: list[tuple[str, str]] = []
        if on is not None:
            for equality in on.find_all(exp.EQ):
                left = equality.left
                right_side = equality.right
                for side, other in ((left, right_side), (right_side, left)):
                    if isinstance(side, exp.Column) and isinstance(other, exp.Column):
                        key_pairs.append(
                            (
                                f"{alias_map.get(str(side.table).lower(), str(side.table))}.{side.name}".lower(),
                                f"{alias_map.get(str(other.table).lower(), str(other.table))}.{other.name}".lower(),
                            )
                        )
        entry = {"right": right, "kind": kinds, "keys": key_pairs}
        joins.append(entry)
        if on is None and not getattr(join, "kind", "").upper() == "CROSS":
            entry["note"] = "join without a key (cartesian)"
        for left_key, right_key in key_pairs:
            for qualified in (left_key, right_key):
                table, _, column = qualified.partition(".")
                if not table or not column:
                    continue
                columns = _described_columns(db, kb_id, table)
                if columns is not None and column not in columns:
                    errors.append(
                        f"join key {qualified!r} does not exist, so the join cannot be evaluated"
                    )
        if key_pairs and right:
            if not _catalog_backs(db, kb_id, right, [k for pair in key_pairs for k in pair]):
                unbacked.append(entry)
    return joins, errors, unbacked


def _catalog_backs(db: Session, kb_id: str, table: str, keys: Iterable[str]) -> bool:
    """Does the semantic catalog acknowledge a relation over these keys?"""
    try:
        from app.services.db.semantic_store import SemanticStore

        store = SemanticStore(db, kb_ids=[kb_id])
        relations = store.relations() if hasattr(store, "relations") else []
    except Exception as e:  # pragma: no cover - catalog is optional
        logger.debug("execution gate: catalog lookup failed: %s", e)
        return False
    wanted = {k.split(".")[-1].lower() for k in keys}
    for relation in relations or []:
        payload = relation if isinstance(relation, _Mapping) else getattr(relation, "__dict__", {})
        names = {
            str(payload.get("from_column") or "").lower(),
            str(payload.get("to_column") or "").lower(),
        }
        if names & wanted:
            return True
    return False
