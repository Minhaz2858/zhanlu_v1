"""Generic join-cardinality derivation (P0-6, Phase B, 2026-10-30).

The ``kb_table_relation`` model has carried ``source_cardinality`` /
``target_cardinality`` columns since its creation — with exactly one
reference in the codebase: the model definition itself. They were dead
weight. This module makes them real, generically:

- No customer-specific rules, no table-name vocabularies. Every signal is
  structural: declared FK direction, join-column key roles (PRI/UNI/MUL
  from the connector's own introspection), and indexed row counts.
- The same derivation is used at INDEX time (the catalog indexer populates
  the columns for newly discovered relations) and at READ time (the
  execution gate derives on the fly for relations indexed before this
  existed — no backfill, no migration).

Convention: ``"one"`` means at most one match per join key on that side,
``"many"`` means potentially several, ``None`` means undetermined.
"""

from __future__ import annotations

#: Row-count ratio above which the larger side is treated as the "many" side
#: when key roles are unavailable. Conservative: a small ratio proves nothing.
ROW_RATIO_MANY_THRESHOLD = 5.0

_UNIQUE_KEY_ROLES = frozenset({"PRI", "UNI"})


def _side_is_unique(key_roles) -> bool | None:
    """Does ANY join column on this side carry a unique key role?

    ``True``  — at least one PRI/UNI column → at most one match per key.
    ``False`` — roles present but none unique → potentially several.
    ``None``  — no role information at all.
    """
    roles = [str(r).upper() for r in (key_roles or []) if r]
    if not roles:
        return None
    return any(r in _UNIQUE_KEY_ROLES for r in roles)


def _side_from_rows(own_rows, other_rows) -> str | None:
    """Row-count ratio tiebreak: the much larger side is the "many" side."""
    try:
        own = float(own_rows) if own_rows is not None else None
        other = float(other_rows) if other_rows is not None else None
    except (TypeError, ValueError):
        return None
    if not own or not other:
        return None
    if own >= other * ROW_RATIO_MANY_THRESHOLD:
        return "many"
    if other >= own * ROW_RATIO_MANY_THRESHOLD:
        return "one"
    return None


def derive_cardinality(
    relation_type: str | None,
    source_key_roles=None,
    target_key_roles=None,
    source_row_count=None,
    target_row_count=None,
) -> tuple[str | None, str | None]:
    """Derive ``(source_cardinality, target_cardinality)`` generically.

    ``relation_type == "FK"`` is authoritative: the source table DECLARES the
    foreign key (the indexer writes the child table as source), so the source
    side is the "many" side and the referenced side is "one" — by definition,
    on every engine that exposes declared FKs.

    Otherwise join-column key roles decide: a side whose join column is
    PRI/UNI admits at most one match per key ("one"); a side without one
    admits several ("many"). Row-count ratios are the conservative tiebreak
    when no role information exists.

    Returns ``(None, None)`` rather than guessing when the evidence is
    insufficient — an undetermined cardinality must never read as a safe one.
    """
    if str(relation_type or "").upper() == "FK":
        return ("many", "one")

    src_unique = _side_is_unique(source_key_roles)
    tgt_unique = _side_is_unique(target_key_roles)
    if src_unique is True and tgt_unique is True:
        return ("one", "one")
    if src_unique is True:
        # Informed "one" side against an uninformed side: the row-ratio view
        # refines it; without that evidence the uninformed side is "many"
        # (a unique key joined to a peer table is the exceptional shape).
        return ("one", "many")
    if tgt_unique is True:
        return ("many", "one")
    if src_unique is False and tgt_unique is False:
        # Neither side unique: both potentially several (many-to-many).
        return ("many", "many")

    # No key-role evidence at all — fall back to the row-count tiebreak.
    src_from_rows = _side_from_rows(source_row_count, target_row_count)
    if src_from_rows is not None:
        return (src_from_rows, "one" if src_from_rows == "many" else "many")
    return (None, None)


def fanout_risk_in_join(
    cardinality: tuple[str | None, str | None],
    source_table: str,
    target_table: str,
    aggregated_tables,
) -> str | None:
    """Human-readable fanout warning for a 1↔N join, or ``None``.

    Pure function — the execution gate calls this per catalog-backed join.
    A warning fires only when the cardinality is genuinely derived (never
    for ``(None, None)``), the join is one-to-many in the direction that
    matters, and an aggregate sits on the "many" side while the "one" side
    also participates — the classic revenue-duplication shape:

        orders (many) ⟕ customers (one)  +  SUM(orders.amount)
        → each customer's amount is counted once per matching row — safe
          only if the grain IS the order line; the warning tells the model
          to make the grain explicit or aggregate before joining.

    Aggregation on the "one" side is not flagged: many-side rows joined to
    their single one-side match cannot multiply one-side aggregates.
    """
    src_card, tgt_card = cardinality
    if not src_card or not tgt_card or src_card == tgt_card:
        return None
    many_table, one_table = (
        (source_table, target_table) if src_card == "many" else (target_table, source_table)
    )
    aggregated = {str(t).lower() for t in (aggregated_tables or []) if t}
    if str(many_table).lower() not in aggregated:
        return None
    # The one-side table must actually participate alongside the many side
    # for duplication to be possible; when the warning is computed per join
    # the caller guarantees both sides are present in the statement.
    return (
        f"join {source_table} ↔ {target_table} is one-to-many "
        f"({one_table} is the one side); aggregating over {many_table} "
        f"across this join can multiply rows — aggregate per the grain or "
        f"deduplicate before joining"
    )
