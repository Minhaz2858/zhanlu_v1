"""One dialect capability layer — the ONLY place dialect behavior is encoded.

Before this module, dialect knowledge was scattered and mostly missing:

* ``nl2sql/dialect_rules.py`` held a 49-line dict of *quoting hints* for four
  dialects and fell back to the PostgreSQL text for anything else — so an
  Oracle or SQL&nbsp;Server source was told to use PostgreSQL quoting while the
  internal helper (``db/base.quote_ident``) quoted it correctly.  Two sources of
  truth, one of them wrong.
* ``LIMIT`` was hardcoded in ``db/schema_graph.py``, ``knowledge_graph/
  catalog_indexer.py`` and ``db/nl_answer_service.py`` — valid on MySQL,
  PostgreSQL and SQLite; a syntax error on SQL Server and Oracle.
* identifier quoting existed three times (``db/base.quote_ident``,
  ``db/schema_graph._quote_ident``, ``forecasting.datasource_registry.
  quote_identifier``) with different coverage.

This module replaces all of it with one data-driven table of profiles plus
generic methods.  Nothing here knows about a customer, a table, a business
term or a default source: a profile is selected from the source's own
dialect/capability record, and an unknown dialect RAISES (``UnknownDialect``)
rather than silently borrowing another vendor's syntax.

Consumers get, per dialect: identifier quoting, pagination/top-N, date and
timestamp expressions, null/string functions, row-count / min-max-date /
coverage probes, EXPLAIN, and a prompt hint built from the same data.  Callers
that must not fail (catalog indexing, coverage probes) use
``maybe_profile()`` and skip the dialect-specific step when it returns None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Identifiers we are willing to interpolate into SQL. Same shape the connectors
# enforce (``db.base._SAFE_IDENT``); kept here so quoting and validation are one
# decision, not two.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── declared-type classification ───────────────────────────────────────────
#
# What a DECLARED column type means for READING the data (not for typing SQL).
# Tokens are matched as substrings of the lowercased declared type, longest
# token first, so a longer name always wins over the token it contains
# ("long raw" before "raw", "varbinary" before "binary").
#
# 'binary' — must never be pulled into memory (BLOBs, geometry, XML, RAW …).
# 'large'  — readable, but its values are documents, not a vocabulary.
# 'other'  — countable (NULLs, cardinality) but nothing more: an unknown or
#            non-comparable type must NOT be guessed at. This is also the
#            final fallback, so a type we have never seen is profiled
#            conservatively instead of being forced into a wrong bucket.
GENERIC_TYPE_ROLES: tuple[tuple[str, str], ...] = (
    # never read
    ("long raw", "binary"), ("varbinary", "binary"), ("binary", "binary"),
    ("bytea", "binary"), ("blob", "binary"), ("image", "binary"),
    ("bfile", "binary"), ("raw", "binary"), ("rowversion", "binary"),
    ("geometry", "binary"), ("geography", "binary"), ("spatial", "binary"),
    ("hierarchyid", "binary"), ("xml", "binary"),
    # readable, never enumerated
    ("clob", "large"), ("long", "large"),
    # ANSI INTERVAL: 'interval' CONTAINS 'int'. Not comparable as a number.
    ("interval", "other"),
    # numeric
    ("bigint", "numeric"), ("smallint", "numeric"), ("tinyint", "numeric"),
    ("int", "numeric"), ("decimal", "numeric"), ("numeric", "numeric"),
    ("number", "numeric"), ("float", "numeric"), ("double", "numeric"),
    ("real", "numeric"), ("money", "numeric"), ("serial", "numeric"),
    # temporal
    ("timestamp", "temporal"), ("datetime", "temporal"),
    ("date", "temporal"), ("time", "temporal"),
    # string
    ("nvarchar", "string"), ("varchar", "string"), ("char", "string"),
    ("text", "string"), ("string", "string"), ("enum", "string"),
    ("set", "string"), ("uuid", "string"),
)

_GENERIC_TYPE_ROLES_SORTED = tuple(
    sorted(GENERIC_TYPE_ROLES, key=lambda kv: (-len(kv[0]), kv[0]))
)

# ── per-dialect EXCEPTIONS to the generic table ────────────────────────────
#
# Keyed by profile name (not by the alias the user typed), so `mariadb`
# resolves through the mysql record automatically.  Only types whose generic
# reading would be WRONG belong here.  Each entry below is either a vendor type
# the ANSI-ish table cannot name, or — the dangerous case — a type whose name
# contains a generic token that means something else in that vendor.
#
# A dialect with no entry (sqlite: free-form affinity names) is covered by the
# generic table alone, which is the intended default rather than an omission.
_DIALECT_TYPE_EXCEPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "mysql": (
        # Sizes of text/blob: only the longest ones are documents.
        ("longtext", "large"), ("mediumtext", "string"), ("tinytext", "string"),
        ("longblob", "binary"), ("mediumblob", "binary"), ("tinyblob", "binary"),
        # Spatial types are opaque geometry, longest name first.
        ("geometrycollection", "binary"), ("multilinestring", "binary"),
        ("multipolygon", "binary"), ("linestring", "binary"),
        ("polygon", "binary"), ("multipoint", "binary"), ("point", "binary"),
        ("json", "other"),
        # BIT arrives as raw bytes from the driver — countable, not comparable.
        ("bit", "other"),
        # BOOL/BOOLEAN are tinyint(1): comparable as 0/1 proportions.
        ("boolean", "numeric"), ("bool", "numeric"),
        # YEAR arrives as an integer, not a date.
        ("year", "other"),
    ),
    "postgres": (
        ("jsonb", "other"), ("json", "other"),
        # A uuid is a per-row identity, never a business vocabulary.
        ("uuid", "other"),
        ("tsvector", "other"), ("tsquery", "other"),
        ("inet", "other"), ("cidr", "other"), ("macaddr8", "other"),
        ("macaddr", "other"),
        ("geometry", "binary"), ("geography", "binary"),
        # Geometric primitives are not comparable values.
        ("point", "other"), ("lseg", "other"), ("circle", "other"),
        ("polygon", "other"), ("path", "other"), ("line", "other"),
        ("box", "other"),
    ),
    "mssql": (
        # 'timestamp' in SQL Server is ROWVERSION — an 8-byte binary, not a
        # time.  Reading it as temporal is the single most damaging mapping in
        # this table, because it looks right.
        ("timestamp", "binary"), ("rowversion", "binary"),
        ("sql_variant", "other"), ("uniqueidentifier", "other"),
        ("hierarchyid", "binary"),
        ("geography", "binary"), ("geometry", "binary"),
        ("xml", "binary"), ("image", "binary"),
        ("bit", "other"),
    ),
    "oracle": (
        ("long raw", "binary"), ("raw", "binary"), ("bfile", "binary"),
        ("rowid", "other"), ("urowid", "other"),
        ("xmltype", "binary"), ("sdo_geometry", "binary"),
    ),
}


def _match_type_role(
    table: tuple[tuple[str, str], ...], declared: str
) -> str | None:
    """Longest-token-wins lookup in one type table."""
    ordered = (
        table
        if table is _GENERIC_TYPE_ROLES_SORTED
        else tuple(sorted(table, key=lambda kv: (-len(kv[0]), kv[0])))
    )
    for token, role in ordered:
        if token in declared:
            return role
    return None


def _is_zero_length_binary(declared: str) -> bool:
    """True for ``binary(0)`` / ``varbinary(0)`` — a declaration, not a payload.

    MySQL types a VIEW column whose expression it cannot resolve as
    ``varbinary(0)``.  Live: ``erp_v_sale_orderentry``'s ``instock_qty``,
    ``produce_qty`` and ``plan_qty`` are declared ``varbinary(0)`` and are NULL
    in ALL 14,589 rows.  Classifying them as ``binary`` made the catalog refuse
    to read them at all, so the prompt showed three apparently usable columns
    with no annotation — when the fact that matters ("nulls 100%") is exactly
    what tells the SQL author to avoid them.

    A POSITIVE length is still ``binary`` and still never read: that is real
    payload, and reading a BLOB to count NULLs is how an index melts.
    """
    head, _, tail = declared.partition("(")
    if not tail:
        return False
    return (
        head.strip() in ("binary", "varbinary")
        and tail.strip().rstrip(")").strip() == "0"
    )


def type_role(dialect: str | None, data_type: str | None) -> str:
    """Role of a DECLARED column type for reading its data.

    Roles: ``binary`` (never read) | ``large`` (read, never enumerate) |
    ``numeric`` | ``temporal`` | ``string`` | ``other`` (NULLs + cardinality
    only).

    Resolution order: the connected dialect's own exceptions → the generic
    ANSI-ish table → ``'other'``.  A dialect is never assumed: an unknown
    dialect gets the generic table (and therefore conservative treatment of
    anything it does not recognise) rather than another vendor's vocabulary.
    """
    declared = str(data_type or "").strip().lower()
    if not declared:
        return "other"
    # An array is a list, not a comparable scalar — and vendor array syntax
    # ("integer[]") embeds the element type, which would mislead the tables.
    if declared.endswith("[]"):
        return "other"
    # A zero-length binary declaration is MySQL's "unresolved expression"
    # marker, not a payload — read it (NULLs + cardinality), never enumerate it.
    if _is_zero_length_binary(declared):
        return "other"
    profile = maybe_profile(dialect)
    if profile is not None:
        exceptions = _DIALECT_TYPE_EXCEPTIONS.get(profile.name, ())
        hit = _match_type_role(exceptions, declared) if exceptions else None
        if hit is not None:
            return hit
    hit = _match_type_role(_GENERIC_TYPE_ROLES_SORTED, declared)
    return hit if hit is not None else "other"


class UnknownDialect(ValueError):
    """Raised when no profile exists for a dialect.

    Callers must decide explicitly what to do — skip the dialect-specific step
    (fail-soft) or surface the limitation. They must NOT fall back to another
    dialect's syntax.
    """


@dataclass(frozen=True)
class DialectProfile:
    """Capability contract for one SQL dialect.

    Every field is data. Methods below are generic and read these fields, so
    adding a dialect means adding one record — never a branch in a query path.
    """

    name: str
    aliases: tuple[str, ...] = ()

    # ── identifier quoting ────────────────────────────────────────────────
    quote_open: str = '"'
    quote_close: str = '"'
    quote_style: str = "double"  # backtick | double | bracket

    # ── pagination / top-N ────────────────────────────────────────────────
    pagination: str = "limit"  # limit | top | fetch

    # ── capabilities ──────────────────────────────────────────────────────
    supports_views: bool = True
    supports_window_functions: bool = True
    supports_read_only: bool = True
    explain_prefix: str | None = "EXPLAIN "
    default_port: int = 0

    # ── value functions ───────────────────────────────────────────────────
    null_function: str = "COALESCE"
    concat_template: str = "CONCAT({a}, {b})"

    # ── date / timestamp expressions ({c}=column, {n}=days) ───────────────
    month_bucket: str = "TO_CHAR({c}, 'YYYY-MM')"
    day_bucket: str = "CAST({c} AS date)"
    current_date_expr: str = "CURRENT_DATE"
    date_add_days: str = "({c} + INTERVAL '{n} days')"
    days_between_tpl: str = "({a} - {b})"

    # ── probe SQL ({t}=table, {s}=schema, {c}=column) ─────────────────────
    version_sql: str | None = None
    timezone_sql: str | None = None
    locale_sql: str | None = None
    views_sql: str | None = None
    current_schema_sql: str | None = None
    list_schemas_sql: str | None = None
    row_count_sql: str | None = None
    read_only_sql: str | None = None
    window_probe_sql: str | None = None

    # Dialect-specific authoring rules appended verbatim to prompt hints.
    authoring_rules: tuple[str, ...] = ()

    # ── identifier quoting ────────────────────────────────────────────────

    def quote_ident(self, name: str) -> str:
        """Quote one identifier. Raises ValueError on an unsafe name."""
        if not isinstance(name, str) or not _SAFE_IDENT.match(name):
            raise ValueError(f"Unsafe identifier: {name!r}")
        return f"{self.quote_open}{name}{self.quote_close}"

    def qualified(self, table: str, schema: str | None = None) -> str:
        """Quote a possibly schema-qualified table name.

        ``"s.t"`` and ``(schema="s", table="t")`` both produce ``s.t`` quoted
        per this dialect. Raises ValueError if any part is unsafe.
        """
        parts = [p for p in str(table).split(".") if p]
        if schema and len(parts) == 1:
            parts = [schema, *parts]
        if not parts:
            raise ValueError(f"Empty table name: {table!r}")
        return ".".join(self.quote_ident(p) for p in parts)

    # ── pagination / top-N ────────────────────────────────────────────────

    def paged(self, sql: str, n: int) -> str:
        """Apply this dialect's row limit to ``sql``.

        MySQL/PostgreSQL/SQLite append ``LIMIT n``; SQL Server inserts
        ``TOP n`` after SELECT [DISTINCT]; Oracle appends
        ``FETCH FIRST n ROWS ONLY``. ``n <= 0`` means "no limit".
        """
        n = int(n)
        if n <= 0:
            return sql
        body = sql.strip().rstrip(";")
        if self.pagination == "limit":
            return f"{body} LIMIT {n}"
        if self.pagination == "top":
            m = re.match(r"(?is)^select\s+(distinct\s+)?", body)
            if not m:
                # Not a SELECT we can place TOP on — return it unchanged so the
                # caller never believes a cap was applied when it was not.
                return body
            return f"{body[:m.end()]}TOP {n} {body[m.end():]}"
        if self.pagination == "fetch":
            return f"{body} FETCH FIRST {n} ROWS ONLY"
        return body

    def probe_sql(self, table: str, where: str = "", n: int = 1) -> str:
        """Cheapest possible "does this table have rows?" statement."""
        return self.paged(f"SELECT 1 FROM {self.qualified(table)}{where}", n)

    def sample_sql(self, table: str, columns: str = "*", n: int = 10) -> str:
        """``SELECT <columns> FROM <table>`` with a dialect-correct row cap."""
        col_sql = "*" if columns.strip() == "*" else ", ".join(
            self.quote_ident(c.strip()) for c in columns.split(",") if c.strip()
        )
        return self.paged(f"SELECT {col_sql} FROM {self.qualified(table)}", n)

    def distinct_sample_sql(self, table: str, column: str, n: int = 40) -> str:
        """``SELECT DISTINCT <column> FROM <table>`` with a dialect row cap."""
        return self.paged(
            f"SELECT DISTINCT {self.quote_ident(column)} FROM {self.qualified(table)}", n
        )

    def min_max_date_sql(self, table: str, column: str) -> str:
        """MIN/MAX of a date column — temporal coverage evidence."""
        c = self.quote_ident(column)
        return (
            f"SELECT MIN({c}) AS min_date, MAX({c}) AS max_date "
            f"FROM {self.qualified(table)}"
        )

    def row_count_sql_for(self, table: str, schema: str | None = None) -> str | None:
        """Approximate row count from catalog statistics, when the dialect has it.

        ``None`` means "this dialect exposes no stats table" — callers must not
        substitute a ``COUNT(*)`` full scan without deciding to.
        """
        if not self.row_count_sql:
            return None
        name = table.split(".")[-1]
        if not _SAFE_IDENT.match(name):
            raise ValueError(f"Unsafe identifier: {table!r}")
        return self.row_count_sql.replace("{t}", name).replace("{s}", schema or "")

    def explain(self, sql: str) -> str | None:
        """``EXPLAIN``-prefixed statement, or None when the dialect can't inline it."""
        if not self.explain_prefix:
            return None
        return f"{self.explain_prefix}{sql.strip().rstrip(';')}"

    # ── date / string expressions ─────────────────────────────────────────

    def _expr(self, template: str, **kw: str) -> str:
        out = template
        for key, value in kw.items():
            out = out.replace("{" + key + "}", value)
        return out

    def month_of(self, column: str) -> str:
        return self._expr(self.month_bucket, c=self.quote_ident(column))

    def day_of(self, column: str) -> str:
        return self._expr(self.day_bucket, c=self.quote_ident(column))

    def current_date(self) -> str:
        return self.current_date_expr

    def add_days(self, column: str, n: int) -> str:
        return self._expr(self.date_add_days, c=self.quote_ident(column), n=str(int(n)))

    def days_between(self, a: str, b: str) -> str:
        """Whole days from ``b`` to ``a``."""
        return self._expr(
            self.days_between_tpl, a=self.quote_ident(a), b=self.quote_ident(b)
        )

    def null_coalesce(self, expr: str, fallback: str) -> str:
        return f"{self.null_function}({expr}, {fallback})"

    def concat(self, a: str, b: str) -> str:
        return self._expr(self.concat_template, a=a, b=b)

    # ── prompt hint ───────────────────────────────────────────────────────

    def quote_label(self) -> str:
        return {
            "backtick": "backticks",
            "double": "double quotes",
            "bracket": "square brackets",
        }.get(self.quote_style, self.quote_style)

    def pagination_label(self) -> str:
        return {
            "limit": "LIMIT n",
            "top": "SELECT TOP n",
            "fetch": "FETCH FIRST n ROWS ONLY",
        }.get(self.pagination, self.pagination)

    def prompt_hint(self) -> str:
        """Dialect contract injected into SQL-generation prompts.

        Built from the same fields the executors use, so the prompt and the
        runtime can never disagree about a dialect again.
        """
        parts = [
            f"Target dialect: {self.name}.",
            f"Quote identifiers with {self.quote_label()} "
            f"(e.g. {self.quote_ident('users')}).",
            f"Cap rows with {self.pagination_label()} "
            f"(e.g. {self.paged('SELECT 1 FROM ' + self.quote_ident('users'), 10)}).",
            f"Current date: {self.current_date_expr}. "
            f"Month bucket: {self.month_of('created_at')}. "
            f"Day bucket: {self.day_of('created_at')}. "
            f"Add days: {self.add_days('created_at', 1)}.",
            f"Null fallback: {self.null_function}(expr, default). "
            f"Text concatenation: {self.concat('a', 'b')}.",
            f"Window functions: {'supported' if self.supports_window_functions else 'NOT supported'}.",
            f"Views: {'supported' if self.supports_views else 'NOT supported'}.",
        ]
        if self.explain_prefix:
            parts.append(
                f"Query plan: prefix a statement with {self.explain_prefix.strip()}."
            )
        else:
            parts.append("Query plan: not available — do not emit EXPLAIN.")
        parts.extend(self.authoring_rules)
        return " ".join(parts)

    # ── capability record (persisted per source) ──────────────────────────

    def capability_dict(self) -> dict[str, Any]:
        """The dialect half of a source capability profile — pure data, no probes."""
        return {
            "dialect": self.name,
            "identifier_quoting": {
                "style": self.quote_style,
                "open": self.quote_open,
                "close": self.quote_close,
            },
            "pagination": self.pagination,
            "date_functions": {
                "current_date": self.current_date_expr,
                "month_bucket": self.month_bucket,
                "day_bucket": self.day_bucket,
                "date_add_days": self.date_add_days,
                "days_between": self.days_between_tpl,
            },
            "null_function": self.null_function,
            "concat_template": self.concat_template,
            "supports_views": self.supports_views,
            "supports_window_functions": self.supports_window_functions,
            "supports_read_only_probe": self.supports_read_only,
            "supports_explain": bool(self.explain_prefix),
            "default_port": self.default_port,
            "probes": {
                "version": bool(self.version_sql),
                "timezone": bool(self.timezone_sql),
                "locale": bool(self.locale_sql),
                "views": bool(self.views_sql),
                "row_count_stats": bool(self.row_count_sql),
                "read_only": bool(self.read_only_sql),
                "schemas": bool(self.list_schemas_sql),
            },
        }


# ── profiles ──────────────────────────────────────────────────────────────
# Data only. One record per dialect; add a dialect by adding a record.

_PROFILES: tuple[DialectProfile, ...] = (
    DialectProfile(
        name="mysql",
        aliases=("mariadb", "mysql", "mariadb-server"),
        quote_open="`",
        quote_close="`",
        quote_style="backtick",
        pagination="limit",
        default_port=3306,
        null_function="IFNULL",
        concat_template="CONCAT({a}, {b})",
        month_bucket="DATE_FORMAT({c}, '%Y-%m')",
        day_bucket="DATE({c})",
        current_date_expr="CURDATE()",
        date_add_days="DATE_ADD({c}, INTERVAL {n} DAY)",
        days_between_tpl="DATEDIFF({a}, {b})",
        version_sql="SELECT VERSION() AS v",
        current_schema_sql="SELECT DATABASE() AS s",
        list_schemas_sql=(
            "SELECT SCHEMA_NAME AS s FROM information_schema.SCHEMATA "
            "ORDER BY SCHEMA_NAME"
        ),
        timezone_sql="SELECT @@session.time_zone AS tz",
        locale_sql="SELECT @@lc_time_names AS lc",
        views_sql=(
            "SELECT COUNT(*) AS n FROM information_schema.VIEWS "
            "WHERE TABLE_SCHEMA = DATABASE()"
        ),
        row_count_sql=(
            "SELECT TABLE_ROWS AS n FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{t}'"
        ),
        read_only_sql="SHOW GRANTS FOR CURRENT_USER()",
        # Two MySQL-specific traps, both measured live on 8.4.5 and both of
        # which recorded a FALSE "window functions not supported":
        #   1. no FROM clause  → error 3592 (cannot use a window function there)
        #   2. OVER (ORDER BY 1) → error 3592, "ORDER BY or PARTITION BY uses
        #      legacy position indication which is unsupported in window
        #      functions"
        # Hence: a derived table AND an order by a real column.
        window_probe_sql=(
            "SELECT ROW_NUMBER() OVER (ORDER BY x) AS rn FROM (SELECT 1 AS x) t LIMIT 1"
        ),
        authoring_rules=(
            "Never use double quotes for identifiers — double quotes are string literals in MySQL.",
        ),
    ),
    DialectProfile(
        name="postgres",
        aliases=("postgres", "postgresql", "pg"),
        quote_open='"',
        quote_close='"',
        quote_style="double",
        pagination="limit",
        default_port=5432,
        null_function="COALESCE",
        concat_template="({a} || {b})",
        month_bucket="TO_CHAR({c}, 'YYYY-MM')",
        day_bucket="({c})::date",
        current_date_expr="CURRENT_DATE",
        date_add_days="({c} + INTERVAL '{n} days')",
        days_between_tpl="(({a})::date - ({b})::date)",
        version_sql="SELECT version() AS v",
        current_schema_sql="SELECT current_schema() AS s",
        list_schemas_sql=(
            "SELECT nspname AS s FROM pg_namespace "
            "WHERE nspname NOT LIKE 'pg\\_%' ORDER BY nspname"
        ),
        timezone_sql="SELECT current_setting('TimeZone') AS tz",
        # PostgreSQL has no ``lc_collate`` setting (measured live:
        # "unrecognized configuration parameter"); the database collation is a
        # pg_database property.
        locale_sql=(
            "SELECT datcollate AS lc FROM pg_database "
            "WHERE datname = current_database()"
        ),
        views_sql=(
            "SELECT COUNT(*) AS n FROM information_schema.views "
            "WHERE table_schema = current_schema()"
        ),
        row_count_sql=(
            "SELECT c.reltuples::bigint AS n FROM pg_class c "
            "JOIN pg_namespace ns ON ns.oid = c.relnamespace "
            "WHERE ns.nspname = current_schema() AND c.relname = '{t}'"
        ),
        read_only_sql=(
            "SELECT has_schema_privilege(current_schema(), 'CREATE') AS can_write"
        ),
        window_probe_sql=(
            "SELECT ROW_NUMBER() OVER (ORDER BY x) AS rn FROM (SELECT 1 AS x) t LIMIT 1"
        ),
        authoring_rules=(
            "Unquoted identifiers fold to lower case — quote any identifier that was created with mixed case.",
        ),
    ),
    DialectProfile(
        name="sqlite",
        aliases=("sqlite", "sqlite3"),
        quote_open='"',
        quote_close='"',
        quote_style="double",
        pagination="limit",
        default_port=0,
        null_function="COALESCE",
        concat_template="({a} || {b})",
        month_bucket="STRFTIME('%Y-%m', {c})",
        day_bucket="DATE({c})",
        current_date_expr="DATE('now')",
        date_add_days="DATE({c}, '+{n} days')",
        days_between_tpl="(JULIANDAY({a}) - JULIANDAY({b}))",
        version_sql="SELECT sqlite_version() AS v",
        current_schema_sql="SELECT 'main' AS s",
        list_schemas_sql="SELECT 'main' AS s",
        timezone_sql=None,
        locale_sql=None,
        views_sql="SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'view'",
        row_count_sql=None,  # no stats table — do not fake one
        read_only_sql="PRAGMA query_only",
        window_probe_sql=(
            "SELECT ROW_NUMBER() OVER (ORDER BY x) AS rn FROM (SELECT 1 AS x) t LIMIT 1"
        ),
        authoring_rules=("No timezone or locale stored by the engine — treat stored times as source-local.",),
    ),
    DialectProfile(
        name="mssql",
        aliases=("mssql", "sqlserver", "sql_server", "tsql"),
        quote_open="[",
        quote_close="]",
        quote_style="bracket",
        pagination="top",
        default_port=1433,
        null_function="ISNULL",
        concat_template="CONCAT({a}, {b})",
        month_bucket="FORMAT({c}, 'yyyy-MM')",
        day_bucket="CAST({c} AS date)",
        current_date_expr="CAST(GETDATE() AS date)",
        date_add_days="DATEADD(day, {n}, {c})",
        days_between_tpl="DATEDIFF(day, {b}, {a})",
        version_sql="SELECT @@VERSION AS v",
        current_schema_sql="SELECT SCHEMA_NAME() AS s",
        list_schemas_sql="SELECT name AS s FROM sys.schemas ORDER BY name",
        timezone_sql="SELECT SYSDATETIMEOFFSET() AS tz",
        locale_sql="SELECT @@LANGUAGE AS lc",
        views_sql="SELECT COUNT(*) AS n FROM sys.views",
        row_count_sql=(
            "SELECT SUM(p.rows) AS n FROM sys.partitions p "
            "WHERE p.object_id = OBJECT_ID('{t}') AND p.index_id IN (0, 1)"
        ),
        read_only_sql=(
            "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT') AS can_write"
        ),
        window_probe_sql=(
            "SELECT TOP 1 ROW_NUMBER() OVER (ORDER BY x) AS rn FROM (SELECT 1 AS x) t"
        ),
        explain_prefix=None,  # SET SHOWPLAN_ALL is session-scoped, not inline
        authoring_rules=(
            "Do not append LIMIT — use SELECT TOP n instead.",
            "Aliases and column names must not be quoted with backticks.",
        ),
    ),
    DialectProfile(
        name="oracle",
        aliases=("oracle", "oracledb"),
        quote_open='"',
        quote_close='"',
        quote_style="double",
        pagination="fetch",
        default_port=1521,
        null_function="NVL",
        concat_template="({a} || {b})",
        month_bucket="TO_CHAR({c}, 'YYYY-MM')",
        day_bucket="TRUNC({c})",
        current_date_expr="TRUNC(SYSDATE)",
        date_add_days="({c} + {n})",
        days_between_tpl="(({a}) - ({b}))",
        version_sql="SELECT banner AS v FROM v$version WHERE ROWNUM = 1",
        current_schema_sql=(
            "SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') AS s FROM dual"
        ),
        list_schemas_sql="SELECT username AS s FROM all_users ORDER BY username",
        timezone_sql="SELECT SESSIONTIMEZONE AS tz FROM dual",
        locale_sql="SELECT SYS_CONTEXT('USERENV', 'LANGUAGE') AS lc FROM dual",
        views_sql="SELECT COUNT(*) AS n FROM user_views",
        row_count_sql=(
            "SELECT num_rows AS n FROM user_tables WHERE table_name = UPPER('{t}')"
        ),
        read_only_sql=None,  # no single-statement privilege check
        window_probe_sql=(
            "SELECT ROW_NUMBER() OVER (ORDER BY x) AS rn FROM (SELECT 1 AS x) t "
            "FETCH FIRST 1 ROWS ONLY"
        ),
        explain_prefix=None,  # EXPLAIN PLAN FOR writes to PLAN_TABLE
        authoring_rules=(
            "No LIMIT clause and no SELECT TOP — use FETCH FIRST n ROWS ONLY.",
            "Unquoted identifiers fold to UPPER CASE; quoted identifiers are case-sensitive.",
            "Every SELECT needs a FROM clause — use FROM dual for literals.",
        ),
    ),
)

# Filled lazily so aliases can be declared before/after the record itself.
_INDEX: dict[str, DialectProfile] = {}
for _p in _PROFILES:
    _INDEX[_p.name] = _p
    for _a in _p.aliases:
        _INDEX[_a] = _p
_INDEX.setdefault("pg", _INDEX["postgres"])


def normalize_dialect(dialect: str | None) -> str:
    """Lower-case and strip separators from a dialect or db_type string.

    ``"PostgreSQL"``, ``"sql-server"`` and ``"SQL Server"`` all collapse to a
    single lookup key. Empty stays empty.
    """
    return (
        (dialect or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )


def _lookup_keys(dialect: str | None) -> list[str]:
    """Candidate keys for a raw dialect string, most specific first.

    Connection strings and driver-qualified names (``"postgresql+psycopg2"``,
    ``"MySQL/MariaDB"``, ``"oracle+cx_oracle"``) name the vendor before the
    separator, so the head is tried as well as the whole string.
    """
    raw = (dialect or "").strip()
    if not raw:
        return []
    keys = [normalize_dialect(raw)]
    for sep in ("+", "/", ":", ";"):
        if sep in raw:
            keys.append(normalize_dialect(raw.split(sep, 1)[0]))
    seen: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.append(k)
    return seen


def maybe_profile(dialect: str | None) -> DialectProfile | None:
    """Profile for a dialect, or None when unknown. Never borrows a default."""
    for key in _lookup_keys(dialect):
        hit = _INDEX.get(key)
        if hit is not None:
            return hit
    return None


def profile_for(dialect: str | None) -> DialectProfile:
    """Profile for a dialect, raising UnknownDialect instead of guessing."""
    prof = maybe_profile(dialect)
    if prof is None:
        raise UnknownDialect(
            f"No dialect capability profile for {dialect!r}. "
            f"Known dialects: {sorted({p.name for p in _PROFILES})}. "
            "Refusing to assume another vendor's syntax."
        )
    return prof


def ansi_quote_ident(name: str) -> str:
    """ANSI-standard identifier quoting for DIALECT-NEUTRAL FORMATTING ONLY.

    This is not a dialect profile and must never be selected to run a statement
    against a source: choosing it silently would reintroduce exactly the
    "borrow another vendor's syntax" bug this module exists to remove. It is
    exported for builders that format SQL *without* a known source (previews,
    golden fixtures, dashboard templates) and for the legacy
    ``base.quote_ident`` fallback so old formatting callers keep working while
    every execution path moves to a real profile.
    """
    if not isinstance(name, str) or not _SAFE_IDENT.match(name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return f'"{name}"'


def known_dialects() -> list[str]:
    """Canonical dialect names this layer can execute against."""
    return sorted({p.name for p in _PROFILES})


# Dialect used when NO source dialect is known. It states the limitation and
# forbids vendor syntax instead of silently emitting one vendor's dialect.
UNKNOWN_DIALECT_HINT = (
    "Target dialect: UNKNOWN. The database type for this source has not been "
    "identified, so use ANSI-standard SQL only: quote identifiers with "
    'double quotes (e.g. "users"), avoid backticks, avoid LIMIT and '
    "SELECT TOP, and avoid vendor-specific date functions."
)
