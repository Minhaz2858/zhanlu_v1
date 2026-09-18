"""Schema-shape change detection — the G4 regression suite.

The defect these tests exist for: every live-schema cache was keyed by CONNECTION
IDENTITY (db_type/host/port/database/schema) and nothing else. That covers "the KB was
re-pointed at a different database" and misses "the database I am already pointing at
changed": a column added to a live warehouse stayed invisible to the agent for
``SCHEMA_CACHE_TTL_SECONDS`` (1 h default), and ``describe_all``'s cache key carried
neither identity nor shape at all. The catalog meanwhile stayed stale until a human
clicked re-index, with no signal on either side.

The tests use a FAKE connector, so they assert the wiring (cache keys, invalidation,
drift reporting, policy filtering) without a live datasource. The live behaviour is
proved separately by ``backend/scripts_probe_schema_shape.py``.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.db import schema_shape as ss

KB = "kb-shape-1"
PROJ = "proj-1"


# ── helpers ────────────────────────────────────────────────────────────────
class FakeConnector:
    """Minimal connector: one dict of table -> columns, with call counters."""

    def __init__(self, tables: dict[str, list[str]]):
        self.tables = dict(tables)
        self.list_calls = 0
        self.describe_calls = 0
        self.shape_calls = 0
        self.shape_raises: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def list_tables(self):
        self.list_calls += 1
        return sorted(self.tables)

    def describe_table(self, table):
        self.describe_calls += 1
        return [
            {"name": c, "type": "text", "nullable": True, "default": None, "pk": False}
            for c in self.tables.get(table, [])
        ]

    def shape(self):
        self.shape_calls += 1
        if self.shape_raises is not None:
            raise self.shape_raises
        return [(t, c) for t, cols in self.tables.items() for c in cols]


@pytest.fixture
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register tables before create_all
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/shape.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _kb_row(db, host="warehouse.local", database="warehouse"):
    from app.models.knowledge_base import KnowledgeBase

    db.add(KnowledgeBase(
        id=KB, name="warehouse", source_kind="database", db_type="mysql",
        host=host, port=3306, database_name=database, catalog_status="ready",
    ))
    db.commit()


def _catalog(db, pairs):
    """Seed the catalog's own shape (the drift baseline)."""
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

    seen: dict[str, str] = {}
    for i, (table, column) in enumerate(pairs):
        tmid = seen.get(table)
        if tmid is None:
            tmid = f"tm-{table}"
            seen[table] = tmid
            db.add(KBTableMeta(id=tmid, kb_id=KB, table_name=table,
                               org_id="o", app_id="a"))
        db.add(KBColumnMeta(id=f"cm-{i}", table_meta_id=tmid, column_name=column,
                            data_type="text", org_id="o", app_id="a"))
    db.commit()


@pytest.fixture(autouse=True)
def _clean_caches():
    ss.invalidate_shape_cache(None)
    from app.services.db.schema_service import invalidate_schema_cache

    invalidate_schema_cache(None)
    yield
    ss.invalidate_shape_cache(None)
    invalidate_schema_cache(None)


@pytest.fixture
def wired(db, monkeypatch):
    """SchemaService + schema_shape both talking to one FakeConnector."""

    def wire(tables: dict[str, list[str]]):
        fake = FakeConnector(tables)
        import app.services.db.connector_factory as cf
        import app.services.db.schema_service as svc

        monkeypatch.setattr(cf, "get_connector", lambda kb: fake)
        monkeypatch.setattr(svc, "get_connector", lambda kb: fake)
        monkeypatch.setattr(settings, "SCHEMA_CACHE_TTL_SECONDS", 3600)
        monkeypatch.setattr(settings, "SCHEMA_SHAPE_ENABLED", True)
        # Probe on every lookup: deterministic, and it is the strictest case.
        monkeypatch.setattr(settings, "SCHEMA_SHAPE_TTL_SECONDS", 0)
        return fake

    return wire


# ── pure helpers ───────────────────────────────────────────────────────────
class TestShapeDigest:
    def test_order_and_case_do_not_matter(self):
        a = [("Orders", "ID"), ("orders", "amount"), ("CUSTOMERS", "id")]
        b = [("customers", "ID"), ("ORDERS", "id"), ("orders", "AMOUNT")]
        assert ss.shape_digest(ss.normalize_pairs(a)) == ss.shape_digest(
            ss.normalize_pairs(b)
        )

    def test_added_or_dropped_column_changes_the_digest(self):
        base = [("orders", "id"), ("orders", "amount")]
        assert ss.shape_digest(base) != ss.shape_digest(base + [("orders", "status")])
        assert ss.shape_digest(base) != ss.shape_digest([("orders", "id")])

    def test_empty_shape_has_no_digest(self):
        # An unmeasurable shape must be EMPTY, never a digest of nothing: callers
        # fall back to the connection-identity key on "".
        assert ss.shape_digest([]) == ""
        assert ss.shape_digest(ss.normalize_pairs([])) == ""

    def test_normalize_survives_odd_rows(self):
        assert ss.normalize_pairs([("t", "c"), (None, "x"), ("t", None)]) == (
            ("", "x"),
        ) or ss.normalize_pairs([("t", "c"), (None, None)]) == (("t", "c"),)


class TestDiff:
    def test_reports_both_directions_with_qualified_names(self):
        report = ss.diff_pairs(
            live=[("orders", "id"), ("orders", "new_col"), ("shipments", "id")],
            catalog=[("orders", "id"), ("legacy", "old_col")],
        )
        assert report.drifted is True
        assert report.columns_added == ["orders.new_col", "shipments.id"]
        assert report.columns_dropped == ["legacy.old_col"]
        assert report.tables_added == ["shipments"]
        assert report.tables_dropped == ["legacy"]
        # A table present on BOTH sides is not "added" just because it gained a column.
        assert "orders" not in report.tables_added

    def test_casing_is_not_drift(self):
        """Measured live: MySQL reports a column in the case it was created in
        while the catalog stores it as written. Comparing raw turned a pure casing
        difference into 1,753 columns added and 1,737 dropped."""
        report = ss.diff_pairs(
            live=[("ERP_T_CRM_CONTRACT", "FBILLNO")],
            catalog=[("erp_t_crm_contract", "fbillno")],
        )
        assert report.drifted is False
        assert report.as_dict() == {} or "hint" not in report.as_dict()

    def test_in_sync_reports_nothing(self):
        pairs = [("orders", "id"), ("orders", "amount")]
        assert ss.diff_pairs(live=pairs, catalog=pairs).drifted is False

    def test_as_dict_is_bounded_and_counts_stay_truthful(self):
        live = [("t", f"c{i}") for i in range(50)]
        report = ss.diff_pairs(live=live, catalog=[])
        payload = report.as_dict(cap=5)
        assert len(payload["columns_added"]) == 5
        assert payload["counts"]["columns_added"] == 50
        assert payload["truncated"] is True
        assert "re-index" in payload["hint"]


# ── probe behaviour ────────────────────────────────────────────────────────
class TestProbe:
    def test_failure_is_fail_soft_and_never_looks_like_an_empty_schema(self, db, wired,
                                                                      monkeypatch):
        _kb_row(db)
        fake = wired({"orders": ["id"]})
        fake.shape_raises = RuntimeError("warehouse unreachable")

        state = ss.shape_state(db, KB)

        assert state.digest == ""      # → callers keep the identity-only key
        assert state.pairs == ()
        assert state.error.startswith("RuntimeError")
        assert state.known is False

    def test_flag_off_probes_nothing(self, db, wired, monkeypatch):
        _kb_row(db)
        fake = wired({"orders": ["id"]})
        monkeypatch.setattr(settings, "SCHEMA_SHAPE_ENABLED", False)

        assert ss.shape_state(db, KB).digest == ""
        assert fake.shape_calls == 0

    def test_change_is_flagged_once_with_the_previous_digest(self, db, wired):
        _kb_row(db)
        fake = wired({"orders": ["id"]})

        first = ss.shape_state(db, KB)
        assert first.changed is False and first.digest

        same = ss.shape_state(db, KB)
        assert same.changed is False and same.digest == first.digest

        fake.tables["orders"].append("status")
        after = ss.shape_state(db, KB)
        assert after.changed is True
        assert after.previous == first.digest
        assert after.digest != first.digest

    def test_probe_is_reused_inside_its_ttl(self, db, wired, monkeypatch):
        _kb_row(db)
        fake = wired({"orders": ["id"]})
        monkeypatch.setattr(settings, "SCHEMA_SHAPE_TTL_SECONDS", 300)

        ss.shape_state(db, KB)
        calls = fake.shape_calls
        second = ss.shape_state(db, KB)

        assert second.from_cache is True
        assert fake.shape_calls == calls

    def test_tool_level_schema_cache_key_carries_the_shape(self):
        """The describe_schema tool has its OWN cache — the shape must key that too,
        or a cache hit there hides the change the service layer already saw."""
        from app.services.tool_handlers.db_tools import _schema_cache_key

        same = _schema_cache_key("kb", "orders", False, 50, "fp1", "shapeA")
        assert same == _schema_cache_key("kb", "orders", False, 50, "fp1", "shapeA")
        assert same != _schema_cache_key("kb", "orders", False, 50, "fp1", "shapeB")
        # Identity still separates two databases, and the legacy call shape works.
        assert same != _schema_cache_key("kb", "orders", False, 50, "fp2", "shapeA")
        assert _schema_cache_key("kb", None, True, 50) == _schema_cache_key(
            "kb", None, True, 50, "", ""
        )


# ── the wiring: a changed datasource must re-introspect ────────────────────
class TestCacheInvalidation:
    def test_a_new_column_is_visible_without_a_restart_or_a_wait(self, db, wired):
        """THE user-visible acceptance: ALTER the datasource, ask again."""
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        fake = wired({"orders": ["id"]})
        svc = SchemaService(db)

        first = svc.describe_table(KB, "orders")
        assert [c["name"] for c in first["columns"]] == ["id"]

        # Second call inside the 1h TTL: cache hit, no introspection.
        svc.describe_table(KB, "orders")
        assert fake.describe_calls == 1

        # A DBA adds a column on the live database.
        fake.tables["orders"].append("po_number")
        third = svc.describe_table(KB, "orders")

        assert fake.describe_calls == 2, "the shape change must force re-introspection"
        assert [c["name"] for c in third["columns"]] == ["id", "po_number"]

    def test_a_cold_change_detector_still_gets_a_cache_miss(self, db, wired):
        """The key is not belt-and-braces — it is the only guard in the cold case.

        A fresh process (or a cleared probe cache) has no previous observation to
        diff against, so ``changed`` stays False and invalidation never fires. If
        the cache key did not carry the shape, the agent would serve the old
        schema for the rest of the TTL.
        """
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        fake = wired({"orders": ["id"]})
        svc = SchemaService(db)

        svc.describe_table(KB, "orders")
        assert fake.describe_calls == 1

        fake.tables["orders"].append("po_number")
        # Simulate a COLD detector: no previous digest to compare against.
        ss._SHAPE_CACHE.clear()
        ss._LAST_DIGEST.clear()

        result = svc.describe_table(KB, "orders")

        assert fake.describe_calls == 2, "a shape-aware key must miss on its own"
        assert [c["name"] for c in result["columns"]] == ["id", "po_number"]

    def test_describe_all_cold_miss_without_identity_change(self, db, wired):
        """Same cold case for describe_all, whose key used to carry NEITHER half."""
        from app.models.knowledge_base import KnowledgeBase
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        fake = wired({"orders": ["id"]})
        svc = SchemaService(db)

        svc.describe_all(KB, max_tables=5)
        assert fake.list_calls == 1

        fake.tables = {"orders": ["id"], "invoices": ["id"]}
        ss._SHAPE_CACHE.clear()
        ss._LAST_DIGEST.clear()
        # Identity is UNCHANGED — only the shape moved.
        assert db.get(KnowledgeBase, KB).database_name == "warehouse"

        result = svc.describe_all(KB, max_tables=5)

        assert fake.list_calls == 2
        assert sorted(t["table"] for t in result["tables"]) == ["invoices", "orders"]

    def test_describe_all_is_keyed_by_identity_and_shape(self, db, wired, monkeypatch):
        """Its key used to carry NEITHER, so re-pointing the KB at another
        database served the previous database's tables for up to an hour."""
        from app.models.knowledge_base import KnowledgeBase
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        fake = wired({"orders": ["id"]})
        svc = SchemaService(db)

        svc.describe_all(KB, max_tables=5)
        assert fake.list_calls == 1
        svc.describe_all(KB, max_tables=5)
        assert fake.list_calls == 1  # cached

        # Same kb_id, DIFFERENT database.
        kb = db.get(KnowledgeBase, KB)
        kb.database_name = "another_warehouse"
        db.commit()
        fake.tables = {"invoices": ["id"]}

        result = svc.describe_all(KB, max_tables=5)

        assert fake.list_calls == 2
        assert [t["table"] for t in result["tables"]] == ["invoices"]

    def test_shape_change_invalidates_the_whole_kb_cache(self, db, wired):
        """A cached describe_all is up to ~25 s of introspection of a schema that
        no longer exists — one change must drop every entry for the KB."""
        from app.services.db import schema_service as svc_mod
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        fake = wired({"orders": ["id"], "customers": ["id"]})
        svc = SchemaService(db)

        before = ss.shape_state(db, KB)
        svc.describe_table(KB, "orders")
        svc.list_tables(KB)
        fake.tables["customers"].append("tier")

        svc.list_tables(KB)  # observes the change → invalidate, then re-cache

        after = ss.shape_state(db, KB)
        assert after.digest != before.digest
        with svc_mod._SCHEMA_CACHE_LOCK:
            keys = [k for k in svc_mod._SCHEMA_CACHE if k[0] == KB]
        assert keys, "the fresh observation should be cached"
        assert all(k[2] == after.digest for k in keys), (
            "no entry may survive from the previous shape"
        )
        svc.describe_table(KB, "orders")
        assert fake.describe_calls == 2


# ── catalog drift ──────────────────────────────────────────────────────────
class TestCatalogDrift:
    def test_live_column_missing_from_the_catalog_is_reported(self, db, wired):
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        _catalog(db, [("orders", "id"), ("orders", "amount")])
        fake = wired({"orders": ["id", "amount", "po_number"],
                      "invoices": ["id"]})
        svc = SchemaService(db)

        result = svc.describe_all(KB, max_tables=5)
        drift = result["schema_drift"]

        assert drift["drifted"] is True
        assert "orders.po_number" in drift["columns_added"]
        assert "invoices" in drift["tables_added"]
        assert drift["catalog_tables"] == 1
        assert "re-index" in drift["hint"]

    def test_no_drift_key_when_in_sync(self, db, wired):
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        _catalog(db, [("orders", "id"), ("orders", "amount")])
        wired({"orders": ["id", "amount"]})

        result = SchemaService(db).describe_all(KB, max_tables=5)

        assert "schema_drift" not in result

    def test_unindexed_source_reports_no_drift_rather_than_agreement(self, db, wired):
        """An unindexed source has nothing to drift FROM. Reporting "in sync"
        there would be a claim we cannot make."""
        from app.services.db.schema_service import SchemaService

        _kb_row(db)
        wired({"orders": ["id"]})

        result = SchemaService(db).describe_all(KB, max_tables=5)

        assert "schema_drift" not in result
        assert ss.drift(db, KB) is None


# ── drift must respect the access policy ───────────────────────────────────
class TestDriftRespectsAccessPolicy:
    def _policy(self, blocked=(), allowed=None, column_allow=None):
        from app.services.access_policy_service import ResolvedPolicy

        policy = ResolvedPolicy(has_policies=True, blocked_kb_ids=set())
        for table in blocked:
            policy.denied_tables[(KB, table.lower())] = "deny"
        if allowed is not None:
            policy.allowlisted_kbs[KB] = {t.lower() for t in allowed}
        if column_allow is not None:
            table, cols = column_allow
            policy.column_allowlists[(KB, table.lower())] = list(cols)
        return policy

    def test_blocked_table_names_are_removed_from_drift(self, db):
        from app.services.tool_handlers.db_tools import _apply_schema_policy

        result = {
            "success": True,
            "tables": [
                {"table": "orders", "columns": [{"name": "id"}]},
            ],
            "schema_drift": {
                "drifted": True,
                "tables_added": ["orders", "payroll"],
                "tables_dropped": ["salaries"],
                "columns_added": ["orders.po_number", "payroll.iban"],
                "columns_dropped": ["salaries.amount"],
                "counts": {
                    "tables_added": 2, "tables_dropped": 1,
                    "columns_added": 2, "columns_dropped": 1,
                },
                "hint": "…re-index…",
            },
        }
        policy = self._policy(blocked={"payroll", "salaries"})

        out = _apply_schema_policy(result, policy, KB, None)
        drift = out["schema_drift"]

        assert drift["tables_added"] == ["orders"]
        assert drift["tables_dropped"] == []
        assert drift["columns_added"] == ["orders.po_number"]
        assert drift["columns_dropped"] == []
        assert drift["counts"]["tables_added"] == 1
        assert drift["counts"]["columns_dropped"] == 0
        assert drift["filtered_by_policy"] is True
        assert "payroll" not in str(drift) and "salaries" not in str(drift)

    def test_column_allowlist_applies_to_drift_of_an_allowed_table(self, db):
        from app.services.tool_handlers.db_tools import _apply_schema_policy

        result = {
            "table": "orders",
            "columns": [{"name": "id"}],
            "schema_drift": {
                "drifted": True,
                "tables_added": [],
                "tables_dropped": [],
                "columns_added": ["orders.po_number", "orders.secret_margin"],
                "columns_dropped": [],
                "counts": {"tables_added": 0, "tables_dropped": 0,
                           "columns_added": 2, "columns_dropped": 0},
                "hint": "…",
            },
        }
        policy = self._policy(allowed={"orders"}, column_allow=("orders", ["id", "po_number"]))

        out = _apply_schema_policy(result, policy, KB, "orders")

        assert out["schema_drift"]["columns_added"] == ["orders.po_number"]
        assert out["schema_drift"]["filtered_by_policy"] is True

    def test_unrestricted_policy_leaves_drift_untouched(self, db):
        from app.services.tool_handlers.db_tools import _apply_schema_policy

        class Open:
            has_policies = False
            blocked_kb_ids: set = set()

        drift = {"drifted": True, "tables_added": ["payroll"], "tables_dropped": [],
                 "columns_added": [], "columns_dropped": [], "counts": {}}
        result = {"success": True, "tables": [{"table": "payroll", "columns": []}],
                  "schema_drift": drift}

        out = _apply_schema_policy(result, Open(), KB, None)

        assert out["schema_drift"]["tables_added"] == ["payroll"]
        assert "filtered_by_policy" not in out["schema_drift"]
