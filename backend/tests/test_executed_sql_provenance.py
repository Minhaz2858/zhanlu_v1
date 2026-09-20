"""The executable form of the SQL, and where it is recorded.

Measured discrepancy this suite makes unrepeatable: a count question returned
``COUNT(*) = 26544`` while the audit trail carried only a fingerprint of the text
the pipeline authored — and the statement the database actually ran is NOT that
text. MySQL prepends ``/*+ MAX_EXECUTION_TIME(...) */``, Oracle wraps the query in
``ROWNUM``. A record that keeps one form and fingerprints another cannot show a
reader which statement produced the figure, and a report that renders the SQL as
plain prose lets a markdown renderer eat the ``*`` of ``COUNT(*)``.

Three seams are pinned here:

1. ``QueryService.execute`` reports the statement the DRIVER received
   (``executed_sql``), not an assumption that it equals the authored SQL;
2. ``_build_provenance`` records that executable statement verbatim, with its own
   fingerprint, next to the authored fingerprint the P3 contract already carries;
3. every registered connector records its own executable form, so a new dialect
   cannot silently drop it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.db import nl_answer_service as nas
from app.services.db.query_service import QueryService

COUNT_SQL = "SELECT COUNT(*) FROM _ref_material_mapping"
HINTED_SQL = "/*+ MAX_EXECUTION_TIME(10000) */ " + COUNT_SQL


def _sha16(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def _provenance(**kw):
    base = dict(
        kb_id="kb-1",
        source_name="source",
        sql=COUNT_SQL,
        tables=["_ref_material_mapping"],
        window=None,
        catalog=None,
        retrieval=None,
    )
    base.update(kw)
    return nas._build_provenance(**base)


# ── 1. the record keeps the executable statement ───────────────────────────


def test_provenance_preserves_the_executable_count_sql():
    prov = _provenance(executed_sql=HINTED_SQL)
    assert prov["executed_sql"] == HINTED_SQL, "the executed statement must be kept verbatim"
    assert "COUNT(*)" in prov["executed_sql"]
    assert "COUNT()" not in prov["executed_sql"].replace("COUNT(*)", ""), (
        "no normalization may reduce the count call to a form no engine accepts"
    )
    assert prov["executed_sql_fingerprint"] == _sha16(HINTED_SQL)


def test_the_authored_fingerprint_still_describes_the_authored_sql():
    """P3's key keeps its meaning: additive change, not a redefinition."""
    prov = _provenance(executed_sql=HINTED_SQL)
    assert prov["sql_fingerprint"] == _sha16(COUNT_SQL)
    assert prov["executed_sql_fingerprint"] != prov["sql_fingerprint"], (
        "a dialect rewrite is visible precisely because the two forms differ"
    )


def test_provenance_falls_back_to_the_authored_sql():
    prov = _provenance(executed_sql=None)
    assert prov["executed_sql"] == COUNT_SQL
    assert prov["executed_sql_fingerprint"] == prov["sql_fingerprint"]


def test_the_executable_sql_survives_into_the_canonical_result_set():
    """The audit trail narration consumes must carry it too (additive key)."""
    from app.services.db import grounding as g

    from tests.test_grounded_contracts import Q3_ZERO_ROWS, _plan, _crs_for

    prov = _provenance(executed_sql=HINTED_SQL)
    crs = _crs_for(_plan(), Q3_ZERO_ROWS)
    crs.provenance = dict(prov)
    assert crs.provenance["executed_sql"] == HINTED_SQL
    assert crs.to_dict()["provenance"]["executed_sql"] == HINTED_SQL
    assert crs.validate() == [], crs.validate()
    assert g.narrate_directive(crs)  # narration still builds from this record


# ── 2. the executor reports what the driver received ───────────────────────


class _StubKB:
    id = "kb-1"
    name = "stub"
    db_type = "mysql"


class _StubConnector:
    """Stands in for a dialect connector: rewrites the SQL, then runs it."""

    last_executed_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        self.last_executed_sql = HINTED_SQL if sql.strip().startswith("SELECT COUNT") else sql
        return [{"COUNT(*)": 26544}]


def test_query_service_reports_the_statement_the_driver_received(monkeypatch):
    from app.services.db import query_service as qs

    monkeypatch.setattr(qs, "get_connector", lambda kb: _StubConnector())
    monkeypatch.setattr(QueryService, "_load_kb", lambda self, kb_id: _StubKB())

    result = QueryService(db=None).execute("kb-1", COUNT_SQL)
    assert result["sql"] == COUNT_SQL, "the authored SQL is reported unchanged"
    assert result["executed_sql"] == HINTED_SQL, (
        "the executable statement must come from the connector, not be assumed"
    )
    assert result["executed_sql"].endswith(COUNT_SQL)


def test_query_service_falls_back_when_a_connector_reports_nothing(monkeypatch):
    from app.services.db import query_service as qs

    class _Silent(_StubConnector):
        def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
            return [{"1": 1}]

    monkeypatch.setattr(qs, "get_connector", lambda kb: _Silent())
    monkeypatch.setattr(QueryService, "_load_kb", lambda self, kb_id: _StubKB())

    result = QueryService(db=None).execute("kb-1", COUNT_SQL)
    assert result["executed_sql"] == COUNT_SQL


# ── 3. every dialect records its own executable form ───────────────────────


def test_every_connector_records_its_executable_statement():
    base = Path(__file__).resolve().parents[1] / "app" / "services" / "db"
    for name in ("sqlite.py", "mysql.py", "postgres.py", "mssql.py", "oracle.py"):
        text = (base / name).read_text(encoding="utf-8")
        assert "self.last_executed_sql" in text, (
            f"{name} must record the statement the driver received"
        )
    # the two dialects that rewrite the statement must record the REWRITTEN form
    assert "self.last_executed_sql = sql" in (base / "mysql.py").read_text(encoding="utf-8"), (
        "MySQL must record the statement AFTER the hint is prepended"
    )
    assert "self.last_executed_sql = wrapped" in (base / "oracle.py").read_text(encoding="utf-8"), (
        "Oracle must record the ROWNUM wrapper, not the authored text"
    )
    mysql = (base / "mysql.py").read_text(encoding="utf-8")
    hint_at = mysql.index("MAX_EXECUTION_TIME({timeout_s * 1000}) */ \" + sql")
    record_at = mysql.index("self.last_executed_sql = sql")
    assert record_at > hint_at, "the record must follow the rewrite, not precede it"


def test_the_connector_protocol_declares_the_executable_statement():
    from app.services.db.base import BaseConnector

    assert "last_executed_sql" in getattr(BaseConnector, "__annotations__", {}), (
        "the protocol must document the field every connector has to set"
    )
