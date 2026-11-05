"""Stage 2B-1 — pure schema classifiers against synthetic schemas.

Acceptance matrix from the design doc §6 (zero real-world table names —
every fixture below is synthetic). Boundaries under test:

- platform/system tables are recognized STRUCTURALLY (engine namespaces,
  credential/session column shapes) — never by name lists;
- partition families + rollup siblings are recognized from column
  fingerprints, name decomposition, and row counts — never from a region
  vocabulary;
- insufficient evidence yields ``unknown`` — never an invented verdict;
- the question-scope resolver matches OBSERVED partition values only.
"""

from __future__ import annotations

from app.services.db.schema_classification import (
    CLASS_BUSINESS,
    CLASS_SYSTEM,
    CLASS_UNKNOWN,
    classify_system_business,
    infer_scope,
    resolve_question_scope,
)


def _cols(*names, dtype="varchar"):
    return [{"column_name": n, "data_type": dtype} for n in names]


def _table(name, extra_cols=(), row_count=1000):
    cols = ["order_id", "order_date", "amount", "region", *extra_cols]
    return {
        "table_name": name,
        "engine": "mysql", "schema_name": "",
        "columns": _cols(*cols, dtype="decimal"),
        "row_count": row_count,
    }


# ── system vs business ─────────────────────────────────────────────────────


class TestEngineNamespaces:
    def test_mysql_information_schema_is_system(self):
        verdict = classify_system_business({
            "engine": "mysql", "schema_name": "information_schema",
            "table_name": "columns", "columns": _cols("table_name", "column_name"),
        })
        assert verdict.schema_class == CLASS_SYSTEM
        assert verdict.confidence >= 0.99

    def test_postgres_pg_catalog_is_system(self):
        verdict = classify_system_business({
            "engine": "postgresql", "schema_name": "pg_catalog",
            "table_name": "pg_class", "columns": _cols("relname"),
        })
        assert verdict.schema_class == CLASS_SYSTEM

    def test_sqlserver_sys_is_system(self):
        verdict = classify_system_business({
            "engine": "mssql", "schema_name": "sys",
            "table_name": "tables", "columns": _cols("object_id", dtype="int"),
        })
        assert verdict.schema_class == CLASS_SYSTEM

    def test_sqlite_master_is_system(self):
        verdict = classify_system_business({
            "engine": "sqlite", "schema_name": "",
            "table_name": "sqlite_sequence", "columns": _cols("name"),
        })
        assert verdict.schema_class == CLASS_SYSTEM

    def test_unqualified_name_merely_called_sys_is_not_system(self):
        """A business table merely NAMED 'sys' with no qualifying schema
        must never read as system (S1 needs the engine namespace)."""
        verdict = classify_system_business({
            "engine": "postgresql", "schema_name": "",
            "table_name": "sys",
            "columns": _cols("id", "order_date", "amount", dtype="decimal"),
        })
        assert verdict.schema_class != CLASS_SYSTEM


class TestCredentialShape:
    def test_credential_table_is_system(self):
        verdict = classify_system_business({
            "engine": "postgresql", "schema_name": "app",
            "table_name": "auth_sessions",
            "columns": _cols(
                "password_hash", "session_token", "refresh_token",
                "salt", "revoked_at",
            ),
        })
        assert verdict.schema_class == CLASS_SYSTEM
        assert any("S2" in s for s in verdict.signals)

    def test_one_credential_column_with_business_columns_is_not_system(self):
        verdict = classify_system_business({
            "engine": "mysql", "schema_name": "app",
            "table_name": "staff",
            "columns": _cols("id", "login_token", "hired_date", "branch"),
        })
        assert verdict.schema_class != CLASS_SYSTEM


class TestBusinessEvidence:
    def test_fact_table_with_join_edge_is_business(self):
        verdict = classify_system_business(
            {
                "engine": "mysql", "schema_name": "shop",
                "table_name": "order_items",
                "columns": _cols("id", "order_id", "qty", dtype="decimal"),
            },
            relation_degree=2,
            table_role="fact",
        )
        assert verdict.schema_class == CLASS_BUSINESS

    def test_isolated_unknown_table_is_unknown_not_business(self):
        verdict = classify_system_business(
            {
                "engine": "mysql", "schema_name": "",
                "table_name": "mysterious_blob",
                "columns": _cols("c1", "c2", "c3"),
            },
            relation_degree=0,
            table_role="unknown",
        )
        assert verdict.schema_class == CLASS_UNKNOWN
        assert any("S3" in s for s in verdict.signals)

    def test_completely_empty_evidence_is_unknown(self):
        verdict = classify_system_business(
            {"engine": "", "schema_name": "", "table_name": "t", "columns": []},
            table_role="unknown",
        )
        assert verdict.schema_class == CLASS_UNKNOWN


# ── scope: partition families + rollup ─────────────────────────────────────


class TestScopeInference:
    def test_partition_family_with_rollup_sibling(self):
        tables = [
            _table("north_orders"),
            _table("south_orders"),
            _table("east_orders"),
            # rollup: superset fingerprint + row count ≈ sum of partitions
            _table("orders", extra_cols=("customer_id", "status"),
                   row_count=3000),
        ]
        verdicts = infer_scope(tables)
        for name, value in (
            ("north_orders", "north"),
            ("south_orders", "south"),
            ("east_orders", "east"),
        ):
            v = verdicts[name]
            assert v.level == "partitioned", name
            assert v.partition_value == value
            assert v.rollup == "orders"
        rollup = verdicts["orders"]
        assert rollup.level == "global"
        assert set(rollup.siblings) == {
            "north_orders", "south_orders", "east_orders",
        }

    def test_partition_family_without_rollup_still_partitions(self):
        tables = [
            _table("branch_a_sales"),
            _table("branch_b_sales"),
            _table("branch_c_sales"),
        ]
        verdicts = infer_scope(tables)
        assert verdicts["branch_a_sales"].level == "partitioned"
        assert verdicts["branch_a_sales"].partition_value == "a"
        assert verdicts["branch_b_sales"].rollup is None

    def test_token_prefix_naming_also_detected(self):
        verdicts = infer_scope([
            _table("fy2024_ledger"), _table("fy2025_ledger"),
        ])
        assert verdicts["fy2024_ledger"].level == "partitioned"
        assert verdicts["fy2024_ledger"].partition_value == "fy2024"

    def test_column_stats_match_confirms_partition_key(self):
        tables = [_table("west_pipeline"), _table("east_pipeline")]
        stats = {"west_pipeline": {"pipeline": {"values": ["west", "east"]}}}
        verdicts = infer_scope(tables, column_stats=stats)
        assert verdicts["west_pipeline"].partition_key == "pipeline"

    def test_similar_but_unpatterned_tables_stay_unknown(self):
        """Clustering alone is NOT scope evidence — no clean base/token
        decomposition → unknown, never an invented partition."""
        verdicts = infer_scope([_table("orders_v2"), _table("sales_backup")])
        assert verdicts["orders_v2"].level == "unknown"
        assert verdicts["sales_backup"].level == "unknown"

    def test_decomposable_family_is_partitioned_even_without_rollup(self):
        """Mechanical rule, stated honestly: base+token decomposition makes
        a partition family regardless of what the tokens 'mean' — the
        classifier has no vocabulary to judge that (and must not)."""
        verdicts = infer_scope([_table("orders_v2"), _table("orders_backup")])
        assert verdicts["orders_v2"].level == "partitioned"
        assert verdicts["orders_v2"].partition_value == "v2"

    def test_single_table_is_unknown(self):
        verdicts = infer_scope([_table("orders")])
        assert verdicts["orders"].level == "unknown"

    def test_unrelated_tables_do_not_cluster(self):
        tables = [
            {"table_name": "invoices", "engine": "mysql", "schema_name": "",
             "columns": _cols("invoice_id", "issuer", "total", dtype="decimal"),
             "row_count": 10},
            {"table_name": "employees", "engine": "mysql", "schema_name": "",
             "columns": _cols("emp_id", "hire_date", "salary", dtype="decimal"),
             "row_count": 10},
        ]
        verdicts = infer_scope(tables)
        assert all(v.level == "unknown" for v in verdicts.values())


# ── question-scope resolution (observed values only) ───────────────────────


class TestQuestionScopeResolution:
    def test_question_token_matching_observed_value(self):
        verdicts = infer_scope([_table("north_orders"), _table("south_orders")])
        match = resolve_question_scope(
            "show me the north orders report", verdicts,
        )
        assert match is not None
        assert match["value"] == "north"
        assert "north_orders" in match["tables"]

    def test_company_wide_question_resolves_no_constraint(self):
        verdicts = infer_scope([_table("north_orders"), _table("south_orders")])
        assert resolve_question_scope(
            "give me the total orders for the whole company", verdicts,
        ) is None

    def test_unknown_question_imposes_no_scope(self):
        verdicts = infer_scope([_table("north_orders"), _table("south_orders")])
        assert resolve_question_scope("hello", verdicts) is None

    def test_global_sibling_value_does_not_match(self):
        tables = [
            _table("north_orders"), _table("south_orders"),
            _table("orders", extra_cols=("customer_id", "status"),
                   row_count=3000),
        ]
        verdicts = infer_scope(tables)
        match = resolve_question_scope("orders", verdicts)
        # "orders" IS the rollup's name, not a partition value → no match
        assert match is None or "orders" not in match.get("tables", [])


# ── backfill script (DB-level, in-memory sqlite) ───────────────────────────


class TestBackfillScript:
    def test_backfill_populates_classification_columns(self, monkeypatch):
        """End-to-end: seed a synthetic catalog (platform tables + a
        partition family + rollup), run the backfill, and verify the
        kb_table_meta columns land — the same columns the P-SV loader and
        overlay overrides will consume in 2B-2/2B-3."""
        import os
        import uuid as _uuid

        os.environ.setdefault(
            "DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true"
        )
        from app.database import Base, SessionLocal, engine
        import app.models  # noqa: F401

        Base.metadata.create_all(engine)

        from app.models.knowledge_base import KnowledgeBase
        from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

        kb_id = f"kb-2b1-{_uuid.uuid4().hex[:8]}"
        db = SessionLocal()
        try:
            db.add(KnowledgeBase(
                id=kb_id, name="2b1", source_kind="database", db_type="mysql",
            ))
            db.commit()

            def _seed(name, cols, schema_name="", row_count=1000):
                tm = KBTableMeta(
                    id=str(_uuid.uuid4()), kb_id=kb_id,
                    schema_name=schema_name, table_name=name,
                    table_type="TABLE", row_count=row_count,
                )
                db.add(tm)
                db.flush()
                for i, (cn, dt) in enumerate(cols):
                    db.add(KBColumnMeta(
                        id=str(_uuid.uuid4()), table_meta_id=tm.id,
                        column_name=cn, ordinal=i, data_type=dt,
                    ))
                db.commit()
                return tm

            common = [("order_id", "int"), ("order_date", "date"),
                      ("amount", "decimal"), ("region", "varchar")]
            north = _seed("north_orders", common)
            south = _seed("south_orders", common)
            _seed("orders", [*common, ("customer_id", "int"),
                             ("status", "varchar")], row_count=3000)
            _seed("columns", [("table_name", "varchar")],
                  schema_name="information_schema")
            _seed("auth_sessions", [
                ("password_hash", "varchar"), ("session_token", "varchar"),
                ("refresh_token", "varchar"), ("salt", "varchar"),
            ])
            db.commit()

            import importlib.util
            from pathlib import Path

            spec = importlib.util.spec_from_file_location(
                "bf", Path(__file__).resolve().parent.parent
                / "scripts" / "backfill_schema_classification.py"
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            result = mod.backfill(kb_id=kb_id)
            assert result["classified"] >= 5

            db.expire_all()
            assert north.schema_class == CLASS_BUSINESS
            # Review point 1: a key-less family is partition_family —
            # NEVER a partition:<key> a scope gate could read as verified.
            assert north.dataset_scope == "partition_family"
            assert not north.dataset_scope.startswith("partition:")
            assert north.dataset_scope_confidence > 0

            orders = (
                db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == kb_id,
                        KBTableMeta.table_name == "orders")
                .first()
            )
            assert orders.dataset_scope == "global"

            info_schema = (
                db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == kb_id,
                        KBTableMeta.table_name == "columns")
                .first()
            )
            assert info_schema.schema_class == CLASS_SYSTEM

            auth = (
                db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == kb_id,
                        KBTableMeta.table_name == "auth_sessions")
                .first()
            )
            assert auth.schema_class == CLASS_SYSTEM
        finally:
            db.close()


# ── review points 1-3 (2026-10-30 checkpoint) ──────────────────────────────


class TestDatasetScopeValue:
    """Point 1: three distinct states — no unnamed pseudo-partitions."""

    def test_global(self):
        from app.services.db.schema_classification import (
            SCOPE_GLOBAL,
            ScopeVerdict,
            dataset_scope_value,
        )

        assert dataset_scope_value(
            ScopeVerdict("global", rollup="orders")
        ) == SCOPE_GLOBAL

    def test_confirmed_partition_carries_its_key(self):
        from app.services.db.schema_classification import (
            ScopeVerdict,
            dataset_scope_value,
        )

        assert dataset_scope_value(
            ScopeVerdict("partitioned", partition_key="region",
                         partition_value="north")
        ) == "partition:region"

    def test_keyless_family_is_partition_family_not_partition(self):
        from app.services.db.schema_classification import (
            SCOPE_PARTITION_FAMILY,
            ScopeVerdict,
            dataset_scope_value,
        )

        value = dataset_scope_value(ScopeVerdict("partitioned"))
        assert value == SCOPE_PARTITION_FAMILY
        assert not value.startswith("partition:")

    def test_unknown_maps_to_none(self):
        from app.services.db.schema_classification import (
            ScopeVerdict,
            dataset_scope_value,
        )

        assert dataset_scope_value(ScopeVerdict("unknown")) is None


class TestEffectiveClassification:
    """Points 2+3: overlay wins with human provenance; inference keeps its
    (weaker) confidence; absence stays absent."""

    class _Meta:
        schema_class = "system"
        schema_class_confidence = 0.9
        dataset_scope = "partition_family"
        dataset_scope_confidence = 0.55

    class _Overlay:
        schema_class_override = None
        dataset_scope_override = None

    def test_inferred_keeps_stored_confidence_and_source(self):
        from app.services.db.schema_classification import (
            effective_classification,
        )

        result = effective_classification(self._Meta())
        assert result["schema_class"] == "system"
        assert result["schema_class_source"] == "inferred"
        assert result["schema_class_confidence"] == 0.9
        assert result["dataset_scope_source"] == "inferred"
        assert result["dataset_scope_confidence"] == 0.55

    def test_human_override_wins_and_is_confidence_one(self):
        from app.services.db.schema_classification import (
            effective_classification,
        )

        class _Ov:
            schema_class_override = "business"
            dataset_scope_override = "global"

        result = effective_classification(self._Meta(), _Ov())
        assert result["schema_class"] == "business"
        assert result["schema_class_source"] == "human"
        assert result["schema_class_confidence"] == 1.0
        assert result["dataset_scope"] == "global"
        assert result["dataset_scope_source"] == "human"

    def test_absent_evidence_stays_absent(self):
        from app.services.db.schema_classification import (
            effective_classification,
        )

        class _Empty:
            schema_class = None
            schema_class_confidence = None
            dataset_scope = None
            dataset_scope_confidence = None

        result = effective_classification(_Empty())
        assert result["schema_class"] is None
        assert result["schema_class_source"] is None
        assert result["dataset_scope"] is None

    def test_a_0_6_inference_is_never_human(self):
        """Point 2, pinned: the contract must be able to tell a 0.6 machine
        guess from a human verdict — sources differ, confidences differ."""
        from app.services.db.schema_classification import (
            effective_classification,
        )

        class _Half:
            schema_class = "business"
            schema_class_confidence = 0.6
            dataset_scope = None
            dataset_scope_confidence = None

        inferred = effective_classification(_Half())
        assert inferred["schema_class_source"] == "inferred"
        assert inferred["schema_class_confidence"] == 0.6


class TestBackfillPreservesHumanOverrides:
    """Point 3: backfill (even --force) must never overwrite human
    decisions — the overlay rows are the human record."""

    def test_force_backfill_leaves_overlay_rows_untouched(self):
        import os
        import uuid as _uuid

        os.environ.setdefault(
            "DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true"
        )
        from app.database import Base, SessionLocal, engine
        import app.models  # noqa: F401

        Base.metadata.create_all(engine)

        from app.models.knowledge_base import KnowledgeBase
        from app.models.knowledge_catalog import (
            KBColumnMeta,
            KBTableMeta,
            ProjectCatalogOverlay,
        )

        kb_id = f"kb-ovr-{_uuid.uuid4().hex[:8]}"
        db = SessionLocal()
        try:
            db.add(KnowledgeBase(
                id=kb_id, name="ovr", source_kind="database", db_type="mysql",
            ))
            db.commit()
            tm = KBTableMeta(
                id=str(_uuid.uuid4()), kb_id=kb_id, schema_name="",
                table_name="human_decided", table_type="TABLE", row_count=5,
            )
            db.add(tm)
            db.flush()
            for i, (cn, dt) in enumerate([("a", "int"), ("b", "varchar")]):
                db.add(KBColumnMeta(
                    id=str(_uuid.uuid4()), table_meta_id=tm.id,
                    column_name=cn, ordinal=i, data_type=dt,
                ))
            # The human record: business + global, against the structural
            # signals (2 isolated int/varchar columns read as unknown).
            overlay = ProjectCatalogOverlay(
                id=str(_uuid.uuid4()), project_id=f"proj-{kb_id}",
                kb_id=kb_id, table_name="human_decided",
                schema_class_override="business",
                dataset_scope_override="global",
            )
            db.add(overlay)
            db.commit()

            import importlib.util
            from pathlib import Path

            spec = importlib.util.spec_from_file_location(
                "bf", Path(__file__).resolve().parent.parent
                / "scripts" / "backfill_schema_classification.py"
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.backfill(kb_id=kb_id, force=True)

            db.expire_all()
            ov = db.get(ProjectCatalogOverlay, overlay.id)
            assert ov.schema_class_override == "business"
            assert ov.dataset_scope_override == "global"

            # The read contract resolves the HUMAN verdict, not the auto one.
            meta = db.get(KBTableMeta, tm.id)
            from app.services.db.schema_classification import (
                effective_classification,
            )

            result = effective_classification(meta, ov)
            assert result["schema_class"] == "business"
            assert result["schema_class_source"] == "human"
            assert result["dataset_scope"] == "global"
            assert result["dataset_scope_source"] == "human"
        finally:
            db.close()


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
