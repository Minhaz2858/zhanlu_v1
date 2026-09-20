"""Per-column statistics from a bounded row sample.

Onboarding already collected what a database *declares* (types, keys, comments).
It never collected what the data *is*: whether a column is 97 % NULL, how many
distinct values it holds, which values actually occur, and what range a measure
spans.  That is the difference between "``FSTATUS`` is a ``varchar(10)``" and
"``FSTATUS`` is one of A/B/C and B carries 80 % of the rows" — and it is what a
cryptic ERP schema needs before a model can choose the right column instead of
guessing from the name.

Cost is bounded by construction: ONE paginated SELECT per object returning at
most ``VALUES_SAMPLE_ROWS`` rows, with everything else computed in-process.
Nothing here writes anything, and nothing here reports a number it did not
measure.

Honesty contract (same as the structure pass in ``catalog_indexer``):

* A bounded sample proves a LOWER bound on distinct count, an OUTER bound on
  min/max and an in-sample null rate.  Every stats dict therefore carries
  ``basis``: ``full`` when the sample provably read the whole object (it
  returned fewer rows than the cap, so there was nothing left to read) or
  ``sample`` when the cap was reached.  ``distinct_is_exact`` and the
  ``*_is_exact`` range flags follow from it.  A sample that happens to land
  exactly on the cap is reported as ``sample`` — understating is safe, the
  opposite is not.
* "Not collected" is an ABSENT key — never ``0`` and never ``[]``.  A column we
  could not read simply has no entry, so a downstream reader can tell "we did
  not look" from "the data says none".
* Which types may be READ at all — and what a read means — is not decided
  here.  It comes from the dialect layer's declared-type role table
  (``db.dialect.type_role``): ``binary`` is never SELECTed (reading a BLOB,
  geometry or Oracle RAW into memory to count NULLs is how an index job dies),
  ``large`` is read but never enumerated, ``other`` yields NULLs and
  cardinality only, and anything a dialect does not recognise falls to
  ``other`` rather than being forced into a wrong bucket.  Adding a database
  therefore means adding data to that table, never a branch here.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# ── bounds ─────────────────────────────────────────────────────────────────

# Rows read per object.  Small enough to be free on any source, large enough
# that null rates and low-cardinality enumerations are meaningful.
VALUES_SAMPLE_ROWS = 200
# One statement per object; generous enough for a view that must materialise.
STATS_TIMEOUT_S = 8
# Values persisted/echoed per column (`{value: count}` / top-value display).
TOP_VALUE_LIMIT = 8
# Longest value kept verbatim in a sample (a 4 KB memo is not a vocabulary).
TOP_VALUE_MAX_LEN = 40
# Above this many distinct values in the sample the column is high-cardinality
# and enumerating values says nothing.  Data-driven: no name or type keywords.
TOP_VALUE_ENUM_MAX = 50
# A per-row-unique string cannot be a cohort to group by.  Below this row count
# the signal is too weak to act on, so we stay with the name-based answer.
MIN_ROWS_FOR_CARDINALITY = 20

# ── type roles ─────────────────────────────────────────────────────────────
#
# Which declared types may be read at all, and what a read means, is decided by
# the DIALECT layer (``db.dialect.type_role``) — a data table covering every
# vendor, so this module never carries a keyword list that would silently be
# another vendor's vocabulary.  Here we only consume the role.

# Roles that may be SELECTed. 'binary' is excluded: pulling a BLOB/geometry/
# RAW payload into memory to count its NULLs is how an index job dies.
_READABLE_ROLES = ("numeric", "temporal", "string", "large", "other")
# Roles whose values can be a vocabulary (a bounded set of recurring labels).
_ENUMERABLE_ROLES = ("string", "numeric")


def role_of(data_type: Any, dialect: str | None = None) -> str:
    """Declared-type role, resolved by the dialect layer."""
    from app.services.db.dialect import type_role

    return type_role(dialect, data_type)


def is_profilable(data_type: Any, dialect: str | None = None) -> bool:
    """True when the column's values may be read at all."""
    return role_of(data_type, dialect) in _READABLE_ROLES


def is_value_profilable(data_type: Any, dialect: str | None = None) -> bool:
    """True when the column's values may be enumerated as a vocabulary."""
    return role_of(data_type, dialect) in _ENUMERABLE_ROLES


# ── SQL ────────────────────────────────────────────────────────────────────


def sample_rows_sql(
    conn: Any,
    table: str,
    columns: list[str],
    n: int,
    *,
    order_by: str | None = None,
    descending: bool = False,
) -> str:
    """``SELECT <col,…> FROM <table>`` capped by the source's own syntax.

    Identifiers are quoted per the connected dialect; the row cap uses the
    dialect's pagination (``LIMIT`` / ``TOP`` / ``FETCH FIRST``).  An unknown
    dialect degrades to bare identifiers and no cap, so the probe fails on its
    own terms rather than borrowing another vendor's clause.

    A column whose name this dialect cannot quote safely is DROPPED from the
    statement — one unnameable column must never cost the row cap, because an
    uncapped ``SELECT`` of every column is a far worse outcome than profiling
    one column less.  Returns ``""`` when nothing is left to ask for.

    ``order_by`` (a column the caller established is orderable) turns this into
    a HEAD or TAIL page: the caller reads both so the sample is not one
    arbitrary page of a large table.  Live, a plain ``LIMIT 200`` on the
    14,589-row ``erp_v_sale_orderentry`` sampled only 2018-06-01…2018-09-25 of
    a PLANDATE range that actually runs to 2026-09-19.
    """
    from app.services.db.dialect import maybe_profile

    profile = maybe_profile(getattr(conn, "dialect", None))
    if profile is None:
        return f"SELECT {', '.join(columns)} FROM {table}"
    quoted: list[str] = []
    for c in columns:
        try:
            quoted.append(profile.quote_ident(c))
        except ValueError:
            logger.debug("column_stats: unquotable column %r skipped", c)
    if not quoted:
        return ""
    try:
        order = ""
        if order_by:
            try:
                order = f" ORDER BY {profile.quote_ident(order_by)}"
                order += " DESC" if descending else ""
            except ValueError:
                # An unquotable key is not an error: the sample just goes back
                # to being one unordered page.
                logger.debug("column_stats: order key %r unquotable", order_by)
                order = ""
        return profile.paged(
            f"SELECT {', '.join(quoted)} FROM {profile.qualified(table)}{order}", n
        )
    except ValueError:
        logger.debug("column_stats: table %r is unquotable in %s", table, profile.name)
        return ""


def sample_order_key(columns: list[dict]) -> str | None:
    """The column to order a spread sample by, or None when there is no key.

    Preferred: the primary key (an index scan, cheap even at a large offset) —
    then the first temporal column, which exists in most business tables and
    makes the head/tail pages the OLDEST and NEWEST rows.  None means the
    sample stays one page, exactly as before.
    """
    for want_pk in (True, False):
        for c in columns or []:
            name = str(c.get("column_name") or c.get("name") or "").strip()
            if not name:
                continue
            if want_pk and c.get("is_primary_key"):
                return name
            if not want_pk and role_of(c.get("data_type")) == "temporal":
                return name
    return None


def _read_sample_page(
    conn: Any,
    table: str,
    names: list[str],
    n: int,
    key: str | None,
    *,
    descending: bool,
) -> list:
    """Read one bounded page of the sample; ``[]`` on any failure (best-effort)."""
    try:
        sql = sample_rows_sql(
            conn, table, names, n, order_by=key, descending=descending
        )
    except Exception:
        logger.debug(
            "column_stats: could not build a sample for '%s'", table, exc_info=True
        )
        return []
    if not sql:
        return []
    try:
        rows = conn.execute(sql, max_rows=n, timeout_s=STATS_TIMEOUT_S)
    except Exception:
        logger.debug(
            "column_stats: sample page of '%s' failed (non-fatal)", table, exc_info=True
        )
        return []
    return list(rows or [])


def _key_bound(rows: list, key: str, pick) -> Any:
    """``max``/``min`` of one order key across rows, or None when unreadable."""
    values = []
    for row in rows:
        value = _row_lookup(row).get(str(key).lower())
        if value is not None:
            values.append(value)
    if not values:
        return None
    try:
        return pick(values)
    except TypeError:
        return None


# ── value extraction ───────────────────────────────────────────────────────


def _row_lookup(row: Any) -> dict:
    """Case-insensitive value map for one returned row."""
    if isinstance(row, dict):
        return {str(k).lower(): v for k, v in row.items()}
    return {}


def _hashable(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:  # pragma: no cover — connectors only emit primitives
        return str(value)
    return value


def _as_number(value: Any) -> float | None:
    """A real number, or None.  ``bool`` is not a number, whatever Python says."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_iso(value: Any) -> str | None:
    """ISO form of a temporal value, or None when it is not one.

    Connectors JSONify the driver's ``date``/``datetime`` into ISO strings, so
    the input here is normally already a string; ``fromisoformat`` both
    validates it and gives a sort key.  Anything unparseable is not treated as
    a temporal measurement.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return None
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return None


def _clean_value(value: Any) -> str | None:
    text = str(value).strip()[:TOP_VALUE_MAX_LEN]
    return text or None


# ── statistics ─────────────────────────────────────────────────────────────


def compute_column_stats(
    rows: list[dict],
    columns: list[dict],
    *,
    cap: int,
    dialect: str | None = None,
) -> dict[str, dict]:
    """Statistics for every introducible column of one object.

    ``rows`` is the bounded sample (already read); ``columns`` is the
    introspected column list (``column_name`` / ``data_type``).  Pure — no I/O
    — so it is testable without a database and reusable for any sample source.
    ``dialect`` selects the type vocabulary; without it the generic table
    applies, which is conservative on anything it does not recognise.
    """
    sampled = len(rows)
    basis = "full" if sampled < cap else "sample"
    exact = basis == "full"
    lookups = [_row_lookup(r) for r in rows]
    out: dict[str, dict] = {}

    for col in columns or []:
        name = col.get("column_name") or col.get("name")
        name = str(name or "").strip()
        role = role_of(col.get("data_type"), dialect)
        if not name or role == "binary":
            # 'binary' is never read at all — see the DialectProfile role table.
            continue
        key = name.lower()
        values = [lk.get(key) for lk in lookups]
        if not values:
            continue

        entry: dict[str, Any] = {
            "rows_sampled": sampled, "basis": basis, "role": role,
        }
        non_null = [v for v in values if v is not None]
        entry["nulls"] = len(values) - len(non_null)
        entry["null_rate"] = round(entry["nulls"] / len(values), 4)

        distinct = {_hashable(v) for v in non_null}
        entry["distinct"] = len(distinct)
        entry["distinct_is_exact"] = exact
        # Registered before the type-specific branches: each of them only ADDS
        # keys to this dict, so a column is profiled as far as its type allows
        # and never dropped for lacking one of them.
        out[name] = entry

        # ── value enumeration ────────────────────────────────────────────
        # A top-values list is a VOCABULARY — it only makes sense for a column
        # whose values recur and could be filtered on. Two classes of column
        # cannot be one, and enumerating them produces noise that downstream
        # consumers (the answer-verification gate's dimension validation, the
        # domain cluster digest) would treat as business vocabulary:
        #   * temporal — a date histogram is not a label; the range is the fact.
        #   * primary key — per-row by definition.
        # Otherwise it is bounded by distinct count: above TOP_VALUE_ENUM_MAX
        # the column is high-cardinality and its values say nothing.
        enumerable = (
            role in _ENUMERABLE_ROLES and not col.get("is_primary_key")
        )
        top_values: list[dict] = []
        if enumerable and 0 < len(distinct) <= TOP_VALUE_ENUM_MAX:
            counts = Counter(_hashable(v) for v in non_null)
            for value, count in counts.most_common(TOP_VALUE_LIMIT):
                text = _clean_value(value)
                if text is not None:
                    top_values.append({"value": text, "count": count})
        if top_values:
            entry["top_values"] = top_values
            entry["top_values_basis"] = basis

        if role in ("string", "large"):
            # 'large' is readable but is a document, not a vocabulary: its
            # size is the useful fact, its values are not (enumerated above
            # only for 'string', because it is not in _ENUMERABLE_ROLES).
            lengths = [len(str(v)) for v in non_null]
            if lengths:
                entry["min_len"] = min(lengths)
                entry["max_len"] = max(lengths)
            continue

        numbers = [n for n in (_as_number(v) for v in non_null) if n is not None]
        if numbers and role == "numeric":
            entry["numeric"] = True
            entry["min"] = min(numbers)
            entry["max"] = max(numbers)
            entry["avg"] = round(sum(numbers) / len(numbers), 6)
            entry["zeros"] = sum(1 for n in numbers if n == 0)
            entry["min_is_exact"] = exact
            entry["max_is_exact"] = exact
            continue

        if role == "temporal":
            stamps = [s for s in (_as_iso(v) for v in non_null) if s is not None]
            if stamps:
                entry["temporal"] = True
                entry["min"] = min(stamps)
                entry["max"] = max(stamps)
                entry["min_is_exact"] = exact
                entry["max_is_exact"] = exact
        # role == 'other' deliberately stops here: NULLs and cardinality are
        # all that can honestly be said about a type we cannot compare.

    return out


def profile_table_columns(
    conn: Any,
    table: str,
    columns: list[dict],
    *,
    sample_rows: int = VALUES_SAMPLE_ROWS,
) -> dict[str, dict]:
    """Read ONE bounded sample of ``table`` and return per-column statistics.

    Returns ``{}`` — never zeros — when the object cannot be read, when it has
    no profilable column, or when it holds no rows.  Best-effort by design: a
    statistics pass must never fail a catalog index.
    """
    dialect = getattr(conn, "dialect", None)
    # Only the columns we will actually ASK for are handed to the statistics
    # pass. A column left out of the statement comes back absent from every
    # row, which would otherwise be reported as "100 % NULL" — an absence of
    # evidence turned into a measurement.
    wanted: list[dict] = []
    names: list[str] = []
    for c in columns or []:
        name = str(c.get("column_name") or c.get("name") or "").strip()
        if not name or role_of(c.get("data_type"), dialect) == "binary":
            continue
        wanted.append(c)
        names.append(name)
    if not wanted or sample_rows <= 0:
        return {}
    # ── the SPREAD sample ────────────────────────────────────────────────
    # A plain ``LIMIT n`` is one arbitrary page, and on a large table that page
    # is whatever the storage engine hands back first — live, the 200-row
    # sample of the 14,589-row ``erp_v_sale_orderentry`` covered
    # 2018-06-01…2018-09-25 while the column actually runs to 2026-09-19. Rows
    # only seen in the unseen tail were invisible to the vocabulary the
    # selector and the embedder read. So: a HEAD page and a TAIL page, ordered
    # by a key the catalog already knows.
    #
    # Disjointness is PROVEN, never assumed. The tail counts only when its
    # lowest key is strictly above the head's highest, i.e. the two pages
    # cannot share a row. A table no bigger than two pages comes back
    # overlapping, and there the single page is the honest answer: de-duping
    # would collapse rows that are legitimately duplicates (a table with no
    # unique key can have them) and corrupt both the value counts and the null
    # rate. Overlap is also why this is not simply "read 2n rows".
    key = sample_order_key(wanted)
    head = _read_sample_page(conn, table, names, sample_rows, key, descending=False)
    merged = list(head)
    if key and head:
        tail = _read_sample_page(
            conn, table, names, sample_rows, key, descending=True
        )
        if tail:
            head_max = _key_bound(head, key, max)
            tail_min = _key_bound(tail, key, min)
            if (
                head_max is not None
                and tail_min is not None
                and tail_min > head_max
            ):
                merged += tail
            else:
                logger.debug(
                    "column_stats: head/tail pages of '%s' overlap (small table) "
                    "— one page kept", table,
                )
    if not merged:
        # An empty object has no statistics to report — and that is a real
        # answer, so it is recorded by writing nothing rather than by zeros.
        return {}
    return compute_column_stats(merged, wanted, cap=sample_rows, dialect=dialect)


# ── shapes consumed downstream ─────────────────────────────────────────────


def column_sample_values(stats: dict | None) -> dict | None:
    """``{value: count}`` for ONE column — the vocabulary bridge.

    Consumed by ``data_source_runtime._load_catalog_meta`` (answer-verification
    catalog validation) and by the description pass.  None when the column has
    no enumerable values.
    """
    if not isinstance(stats, dict):
        return None
    out: dict[str, int] = {}
    for item in stats.get("top_values") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if value is None or str(value) == "":
            continue
        out[str(value)] = int(item.get("count") or 0)
    return out or None


def table_sample_values(
    stats_by_column: dict[str, dict] | None, *, limit: int = TOP_VALUE_LIMIT
) -> dict | None:
    """``{column: [values]}`` for ONE TABLE.

    The shape ``KBTableMeta.sample_values`` is read as (``domain_discovery``
    cluster digests, ``semantic_store.sample_values``).  None when nothing was
    enumerated.
    """
    if not isinstance(stats_by_column, dict):
        return None
    out: dict[str, list[str]] = {}
    for col, stats in stats_by_column.items():
        values = list(column_sample_values(stats) or {})
        if values:
            out[str(col)] = values[:limit]
    return out or None


def stats_annotation(stats: dict | None) -> str:
    """One-line rendering of a column's measurement, for any prompt or comment.

    Every figure carries its own qualifier, so no consumer can present a
    sampled bound as a measured total.  ``""`` when nothing was measured —
    callers append it unconditionally.
    """
    if not isinstance(stats, dict) or not stats:
        return ""
    bits: list[str] = []
    rate = stats.get("null_rate")
    if isinstance(rate, (int, float)) and rate > 0:
        bits.append(f"nulls {rate:.0%}")
    distinct = stats.get("distinct")
    if isinstance(distinct, int):
        mark = "" if stats.get("distinct_is_exact") else "≥"
        bits.append(f"distinct {mark}{distinct}")
    if stats.get("numeric") or stats.get("temporal"):
        lo, hi = stats.get("min"), stats.get("max")
        if lo is not None and hi is not None:
            mark = "" if stats.get("min_is_exact") else "≥"
            bits.append(f"range {mark}{lo} … {hi}")
        avg = stats.get("avg")
        if isinstance(avg, (int, float)):
            bits.append(f"avg {avg:,.2f}")
    elif stats.get("max_len"):
        bits.append(f"len ≤{stats['max_len']}")
    if not bits:
        return ""
    sampled = stats.get("rows_sampled")
    scope = "全表" if stats.get("basis") == "full" else f"{sampled} 行抽样"
    return f"实测({scope}): " + ", ".join(bits)


def stats_prompt_suffix(stats: dict | None) -> str:
    """The same measurement, framed as a ``//`` comment for the description pass."""
    annotation = stats_annotation(stats)
    return f" // {annotation}" if annotation else ""


def vocabulary_hint(
    sample_values: dict | None, *, columns: int = 3, values: int = 4, width: int = 60
) -> str:
    """Bounded ``col: v1, v2`` hint from a table's measured vocabulary.

    Consumed by the table SELECTOR, whose prompt is budget-capped, so this is
    deliberately small: the point is that a question phrased in the customer's
    own words ("混三甲苯 sales") can match a column's VALUES, not just its name.
    ``""`` when the table has no measured vocabulary.
    """
    if not isinstance(sample_values, dict) or not sample_values:
        return ""
    parts: list[str] = []
    for col, values_list in sample_values.items():
        if not isinstance(values_list, list) or not values_list:
            continue
        shown = ", ".join(str(v) for v in values_list[:values])
        parts.append(f"{col}: {shown}")
        if len(parts) >= columns:
            break
    return ("values " + "; ".join(parts))[:width] if parts else ""
