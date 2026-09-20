"""Guard: no vendor SQL assumptions in dialect-agnostic paths.

The bugs this suite makes unrepeatable, all measured in this codebase:

    db/schema_graph.py:141          f"SELECT * FROM {q} LIMIT {n}"      → error on MSSQL/Oracle
    knowledge_graph/catalog_indexer f"SELECT DISTINCT {c} FROM {t} LIMIT n" → same
    db/nl_answer_service.py:1631    f"SELECT 1 FROM {q}{w} LIMIT 1"     → same
    db/nl_answer_service.py:1593    (kb.db_type or "mysql")            → silent default dialect
    db/schema_graph.py:47           mysql→backtick else double-quote    → wrong brackets on MSSQL
    nl2sql/dialect_rules.py:49      _RULES.get(key, _RULES["postgres"]) → PostgreSQL rules for Oracle
    dashboard_profiler.py:69        quote_ident(table, "default")      → "default" dialect

A unit test on each call site would not have caught the *pattern*; this one scans
the dialect-agnostic modules for the pattern itself.

Rules
-----
* The scan is over source, ignoring comments and docstrings (they legitimately
  discuss the old behaviour, e.g. "``or \"mysql\"`` bug this replaces").
* Dialect-specific files are excluded by construction: ``db/dialect.py`` (the
  table of vendor syntax), the per-dialect connectors and the per-dialect
  adapters. Those are *supposed* to contain one vendor's SQL.
* Any other intentional exception must carry an explicit
  ``# dialect-default-ok`` or ``# dialect-sql-ok`` marker on the line, with a
  reason. Silently adding an exception is not possible.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

# Paths that must stay dialect-agnostic (relative to backend/).
SCANNED = [
    "app/services/db",
    "app/services/knowledge_graph",
    "app/services/data_source_runtime",
    "app/services/nl2sql",
    "app/services/tool_handlers/db_tools.py",
    "app/services/dashboard_profiler.py",
    "app/services/universal_analytics",
    "app/services/query_rewriter.py",
]

# Files whose SQL is deliberately one vendor's, with the reason.
EXEMPT_FILES = {
    "app/services/db/dialect.py": "the dialect capability table — vendor syntax lives here",
    "app/services/db/mysql.py": "MySQL connector: its statements are MySQL's",
    "app/services/db/postgres.py": "PostgreSQL connector",
    "app/services/db/mssql.py": "SQL Server connector",
    "app/services/db/oracle.py": "Oracle connector",
    "app/services/db/sqlite.py": "SQLite connector",
    "app/services/db/business_reports.py": "backticks here are Markdown in prose, not SQL",
}

# The patterns. Each entry: (name, compiled regex, why it is forbidden).
FORBIDDEN = [
    (
        "interpolated row cap",
        re.compile(r"\bLIMIT\s+\{"),
        "a row cap must come from the dialect profile (TOP / LIMIT / FETCH FIRST)",
    ),
    (
        "literal row cap",
        re.compile(r"\bLIMIT\s+\d"),
        "a literal LIMIT is a syntax error on SQL Server and Oracle",
    ),
    (
        "backtick identifier in SQL",
        re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+`"),
        "backticks are MySQL-only; use profile.qualified()/quote_ident()",
    ),
    (
        "default dialect fallback",
        re.compile(r"\bor\s+[\"']mysql[\"']"),
        "an unknown dialect must raise, not borrow MySQL",
    ),
    (
        "default dialect argument",
        re.compile(r"quote_ident\([^)]*[\"']default[\"']"),
        "quote_ident(..., \"default\") reintroduces an unnamed dialect",
    ),
    (
        "postgres fallback for unknown dialect",
        re.compile(r"get\([^)]*,\s*_RULES\["),
        "an unknown dialect must not be described as PostgreSQL",
    ),
]

MARKERS = ("dialect-default-ok", "dialect-sql-ok")


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCANNED:
        target = BACKEND / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(p for p in target.rglob("*.py") if "__pycache__" not in p.parts))
    return files


def _code_lines(path: Path):
    """Yield (line_no, code_text, raw_line) — code only, comments stripped.

    ``raw_line`` is kept alongside so an intentional exception can be marked
    with a comment (``# dialect-sql-ok: <reason>``) that survives the strip.
    """
    in_doc = False
    quote = ""
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = raw
        stripped = text.strip()
        if in_doc:
            if quote in stripped:
                in_doc = False
            continue
        if stripped.startswith(('"""', "'''")):
            q = stripped[:3]
            # one-line docstring?
            if stripped.count(q) >= 2 and len(stripped) > 3:
                continue
            in_doc = True
            quote = q
            continue
        if stripped.startswith("#"):
            continue
        # strip inline comments so prose after code is not scanned
        if "#" in text:
            text = text.split("#", 1)[0]
        yield i, text, raw


def test_no_vendor_sql_assumptions_in_agnostic_paths():
    violations: list[str] = []
    scanned = 0
    for path in _iter_source_files():
        rel = str(path.relative_to(BACKEND))
        if rel in EXEMPT_FILES:
            continue
        scanned += 1
        for line_no, text, raw in _code_lines(path):
            if any(marker in raw for marker in MARKERS):
                continue
            for name, pattern, why in FORBIDDEN:
                if pattern.search(text):
                    violations.append(f"{rel}:{line_no} [{name}] {why}\n    {text.strip()}")
    assert scanned >= 15, f"scan covered only {scanned} files — paths moved?"
    assert not violations, (
        "dialect assumptions found in dialect-agnostic code:\n"
        + "\n".join(violations)
        + "\n\nFix: use services/db/dialect.py (profile_for / maybe_profile). "
        "If the line is genuinely correct, add a '# dialect-sql-ok: <reason>' "
        "marker with the reason."
    )


def test_dialect_module_is_the_single_quoting_source():
    """base.quote_ident must delegate — not keep a second if/elif table."""
    from app.services.db import base

    src = (BACKEND / "app/services/db/base.py").read_text(encoding="utf-8")
    assert "profile_for" in src, "base.quote_ident no longer delegates to the dialect layer"
    assert src.count('f"`{name}`"') == 0, "backtick quoting is duplicated in base.py"
    # behaviour still correct for every dialect
    assert base.quote_ident("t", "mysql") == "`t`"
    assert base.quote_ident("t", "mssql") == "[t]"
    assert base.quote_ident("t", "oracle") == '"t"'


def test_every_dialect_connector_is_registered_and_profiled():
    """A connector without capability rules is a dialect users cannot actually use."""
    from app.services.db.connector_factory import supported_db_types
    from app.services.db.dialect import maybe_profile

    for db_type in supported_db_types():
        assert maybe_profile(db_type) is not None, (
            f"{db_type} is offered in the UI but has no dialect capability profile"
        )


def test_exempt_files_still_exist():
    """Keep the exemption list honest — a stale entry hides a real file."""
    for rel in EXEMPT_FILES:
        assert (BACKEND / rel).is_file(), f"exempt file no longer exists: {rel}"


def test_scan_markers_are_used_sparingly():
    """Every override must be annotated; a pile of markers means a real problem."""
    marked = 0
    for path in _iter_source_files():
        rel = str(path.relative_to(BACKEND))
        if rel in EXEMPT_FILES:
            continue
        for _line_no, _text, raw in _code_lines(path):
            if any(marker in raw for marker in MARKERS):
                marked += 1
    assert marked <= 3, (
        f"{marked} dialect exceptions are annotated in agnostic paths — "
        "each one is a place the dialect layer should be used instead"
    )


@pytest.mark.parametrize(
    "line",
    [
        'f"SELECT * FROM {q} LIMIT {n}"',
        'f"SELECT 1 FROM {t} LIMIT 1"',
        'f"SELECT * FROM {_quote(t)} LIMIT 50"',
        'dialect = kb.db_type or "mysql"',
        'quote_ident(table, "default")',
    ],
)
def test_the_guard_actually_catches_the_old_code(line):
    """Negative control: the patterns match the exact lines that were removed."""
    assert any(p.search(line) for _n, p, _w in FORBIDDEN), f"guard misses: {line}"
