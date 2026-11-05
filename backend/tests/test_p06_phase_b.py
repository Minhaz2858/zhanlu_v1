"""Phase B — P0-6: metric promotion integrity + join fanout detection.

Defects reproduced here (all verified against clean HEAD on 2026-10-30):

1. PROMOTION GAP: approving a ProjectMetric (Data Map UI) only flips a
   status field — the NL2SQL contract (MetricDefinition) never sees it.
   Human-approved metrics must reach the enforced contract; inferred
   (proposed, LLM-sourced) ones must NEVER auto-promote.
2. CONSUMPTION INTEGRITY: ``datasource_metric_bindings`` shipped
   conflicting duplicate metric names and bindings whose base table no
   longer exists in the catalog.
3. DEAD CARDINALITY: ``kb_table_relation.source_cardinality`` /
   ``target_cardinality`` had exactly one reference in the codebase — the
   model definition. Join fanout risk was undetectable.

No customer-specific rules anywhere: every signal is structural (FK
direction, key roles, row counts) per the P0 audit's dynamic-compatibility
boundary.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest

import app.models  # noqa: F401  register all models
from app.database import Base, SessionLocal, engine

from app.services.knowledge_graph.join_cardinality import (
    derive_cardinality,
    fanout_risk_in_join,
)


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ── 1. Metric promotion bridge ──────────────────────────────────────────────


def _seed_project_metric(db, *, kb_id="kb-prom", status="proposed", source="user",
                         name="net_revenue", aliases=None, bindings=None):
    from app.models.knowledge_catalog import ProjectMetric

    # (project_id, name) is UNIQUE — reuse the row when a previous run/test
    # created it, refreshing the fields this test cares about.
    pm = (
        db.query(ProjectMetric)
        .filter(
            ProjectMetric.project_id == "proj-p06",
            ProjectMetric.name == name,
        )
        .first()
    )
    if pm is not None:
        pm.kb_id = kb_id
        pm.status = status
        pm.source = source
        db.commit()
        return pm
    pm = ProjectMetric(
        id=str(uuid.uuid4()),
        project_id="proj-p06",
        kb_id=kb_id,
        name=name,
        aliases=aliases if aliases is not None else ["净额", "net amount"],
        definition="FAMOUNT — revenue net of tax.",
        sql_expression="SELECT SUM(FAMOUNT) FROM sales_order",
        unit="CNY",
        default_aggregation="sum",
        bindings=bindings if bindings is not None else [
            {"table": "sales_order", "measure_columns": ["FAMOUNT"],
             "date_column": "ORDER_DATE", "dimensions": ["region"]},
        ],
        source=source,
        status=status,
    )
    db.add(pm)
    db.commit()
    return pm


class TestMetricPromotionBridge:
    def test_approval_promotes_to_enabled_metric_definition(self, db):
        """RED at HEAD: nothing connected ProjectMetric.approved to
        MetricDefinition — approval was cosmetic w.r.t. enforcement."""
        from app.services.knowledge_graph.metric_bootstrap import (
            promote_to_metric_definition,
        )

        pm = _seed_project_metric(db)
        # Simulate the human approval event (the ONLY promotion trigger).
        pm.status = "approved"
        db.commit()
        result = promote_to_metric_definition(db, pm)
        assert result["promoted"] is True

        from app.models.metric_definition import MetricDefinition

        md = (
            db.query(MetricDefinition)
            .filter(
                MetricDefinition.datasource_id == "kb-prom",
                MetricDefinition.name == "net_revenue",
                MetricDefinition.is_deleted.is_(False),
            )
            .first()
        )
        assert md is not None, "approved metric never reached the contract"
        assert md.is_enabled is True
        assert md.base_table == "sales_order"
        assert md.base_column == "FAMOUNT"
        assert md.aggregation == "sum"
        assert "净额" in (md.synonyms or [])
        assert md.unit == "CNY"

    def test_promotion_is_idempotent_no_duplicate(self, db):
        from app.services.knowledge_graph.metric_bootstrap import (
            promote_to_metric_definition,
        )

        pm = _seed_project_metric(db)
        pm.status = "approved"
        db.commit()
        first = promote_to_metric_definition(db, pm)
        second = promote_to_metric_definition(db, pm)
        assert first["promoted"] is True
        assert second["promoted"] is True

        from app.models.metric_definition import MetricDefinition

        rows = (
            db.query(MetricDefinition)
            .filter(
                MetricDefinition.datasource_id == "kb-prom",
                MetricDefinition.name == "net_revenue",
                MetricDefinition.is_deleted.is_(False),
            )
            .all()
        )
        assert len(rows) == 1, "re-approval duplicated the definition"

    def test_proposed_llm_metric_is_never_auto_promoted(self, db):
        """The boundary the reviewer set: no automatic promotion of inferred
        metrics — only a human approval event promotes."""
        from app.services.knowledge_graph.metric_bootstrap import (
            promote_to_metric_definition,
        )

        pm = _seed_project_metric(db, source="llm", status="proposed")
        assert pm.status == "proposed"
        assert promote_to_metric_definition(db, pm, require_approved=True)[
            "promoted"
        ] is False

    def test_metric_without_kb_cannot_promote(self, db):
        from app.services.knowledge_graph.metric_bootstrap import (
            promote_to_metric_definition,
        )

        pm = _seed_project_metric(db, kb_id=None)
        result = promote_to_metric_definition(db, pm, require_approved=False)
        assert result["promoted"] is False
        assert result.get("reason")


# ── 2. Consumption integrity in datasource_metric_bindings ─────────────────


def _seed_metric_definition(db, datasource_id, name, base_table, base_column,
                            *, aggregation="sum", enabled=True):
    from app.models.metric_definition import MetricDefinition

    md = MetricDefinition(
        id=str(uuid.uuid4()),
        datasource_id=datasource_id,
        name=name,
        base_table=base_table,
        base_column=base_column,
        aggregation=aggregation,
        display_name=name,
        synonyms=[],
        is_enabled=enabled,
    )
    db.add(md)
    db.commit()
    return md


class TestMetricConsumptionIntegrity:
    @staticmethod
    def _seed_catalog(db, kb_id="kb-int", tables=("sales_order",)):
        """A minimal indexed catalog: the KB row (FK target) + live tables."""
        from app.models.knowledge_base import KnowledgeBase
        from app.models.knowledge_catalog import KBTableMeta

        if db.get(KnowledgeBase, kb_id) is None:
            db.add(KnowledgeBase(
                id=kb_id, name=f"kb-{kb_id}", source_kind="database",
                db_type="mysql",
            ))
            db.commit()
        for table in tables:
            if (
                db.query(KBTableMeta)
                .filter(
                    KBTableMeta.kb_id == kb_id,
                    KBTableMeta.table_name == table,
                )
                .first()
                is None
            ):
                db.add(KBTableMeta(
                    id=str(uuid.uuid4()), kb_id=kb_id, schema_name="",
                    table_name=table, table_type="TABLE",
                ))
        db.commit()

    def test_conflicting_duplicate_names_collapse_deterministically(self, db):
        """RED at HEAD: two enabled rows named net_revenue with different
        base columns BOTH became bindings — contradictory BINDING
        constraints in one prompt, non-deterministic alias validation."""
        from app.services.db.query_contract import datasource_metric_bindings

        self._seed_catalog(db)
        _seed_metric_definition(db, "kb-int", "net_revenue", "sales_order", "FAMOUNT")
        _seed_metric_definition(db, "kb-int", "net_revenue", "sales_order", "FALLAMOUNT")

        bindings = datasource_metric_bindings(db, "kb-int")
        named = [b for b in bindings if b.term == "net_revenue"]
        assert len(named) == 1, (
            "conflicting definitions must collapse to ONE binding, "
            f"got {len(named)}: {[b.base_column for b in named]}"
        )
        assert named[0].base_column == "famount"  # deterministic order

    def test_identical_duplicate_definitions_collapse(self, db):
        from app.services.db.query_contract import datasource_metric_bindings

        self._seed_catalog(db)
        _seed_metric_definition(db, "kb-int", "net_revenue", "sales_order", "FAMOUNT")
        _seed_metric_definition(db, "kb-int", "net_revenue", "sales_order", "FAMOUNT")
        bindings = [b for b in datasource_metric_bindings(db, "kb-int")
                    if b.term == "net_revenue"]
        assert len(bindings) == 1

    def test_binding_whose_base_table_left_the_catalog_is_skipped(self, db):
        """RED at HEAD: after a table rename/delete the stale binding kept
        shipping and the validator rejected everything touching the old
        name — drift had zero handling."""
        from app.services.db.query_contract import datasource_metric_bindings

        self._seed_catalog(db)
        _seed_metric_definition(db, "kb-int", "live_metric", "sales_order", "FAMOUNT")
        _seed_metric_definition(db, "kb-int", "ghost_metric", "deleted_table", "X")

        bindings = datasource_metric_bindings(db, "kb-int")
        terms = [b.term for b in bindings]
        assert "live_metric" in terms
        assert "ghost_metric" not in terms, (
            "a binding whose base table no longer exists in the catalog "
            "must not reach the contract/validators"
        )


# ── 3. Generic cardinality derivation ──────────────────────────────────────


class TestDeriveCardinality:
    def test_declared_fk_is_many_to_one(self):
        assert derive_cardinality("FK") == ("many", "one")

    def test_unique_key_against_non_unique_is_one_to_many(self):
        assert derive_cardinality("NAME_MATCH",
                                  source_key_roles=["MUL"],
                                  target_key_roles=["PRI"]) == ("many", "one")

    def test_both_unique_is_one_to_one(self):
        assert derive_cardinality("VALUE_OVERLAP",
                                  source_key_roles=["PRI"],
                                  target_key_roles=["UNI"]) == ("one", "one")

    def test_neither_unique_is_many_to_many(self):
        assert derive_cardinality("NAME_MATCH",
                                  source_key_roles=["MUL"],
                                  target_key_roles=["MUL"]) == ("many", "many")

    def test_row_count_ratio_tiebreak(self):
        assert derive_cardinality(None, source_row_count=10000,
                                  target_row_count=50) == ("many", "one")

    def test_no_evidence_yields_none_never_a_guess(self):
        assert derive_cardinality(None) == (None, None)
        assert derive_cardinality("NAME_MATCH", source_row_count=100,
                                  target_row_count=90) == (None, None)


class TestFanoutWarning:
    def test_aggregate_on_many_side_of_one_to_many_warns(self):
        warning = fanout_risk_in_join(
            ("many", "one"), "orders", "customers",
            aggregated_tables=["orders"],
        )
        assert warning and "one-to-many" in warning

    def test_aggregate_on_one_side_does_not_warn(self):
        assert fanout_risk_in_join(
            ("many", "one"), "orders", "customers",
            aggregated_tables=["customers"],
        ) is None

    def test_undetermined_cardinality_never_warns(self):
        assert fanout_risk_in_join(
            (None, None), "orders", "customers",
            aggregated_tables=["orders"],
        ) is None

    def test_no_aggregates_no_warning(self):
        assert fanout_risk_in_join(
            ("many", "one"), "orders", "customers", aggregated_tables=[],
        ) is None


# ── 4. Execution-gate wiring ───────────────────────────────────────────────


class _GateSource:
    id = "kb-1"
    name = "warehouse"
    db_type = "mysql"
    source_kind = "database"
    is_deleted = False


_GATE_RELATION = {
    "source_table": "orders", "target_table": "customers",
    "source_columns": ["customer_id"], "target_columns": ["customer_id"],
    "relation_type": "FK",
    "source_cardinality": "many", "target_cardinality": "one",
}


class TestJoinFanoutGateCheck:
    def _gate_sql(self):
        return (
            "SELECT c.customer_name, SUM(o.order_qty) AS qty FROM orders o "
            "JOIN customers c ON o.customer_id = c.customer_id "
            "GROUP BY c.customer_name"
        )

    def test_fanout_check_registered(self):
        from app.services.db.execution_gate import EXECUTION_CHECKS

        assert "join_fanout" in EXECUTION_CHECKS

    def test_warning_fires_when_flag_on_and_cardinality_known(self, monkeypatch):
        from app.config import settings
        from app.services.db import execution_gate as gate

        monkeypatch.setattr(settings, "NL2SQL_JOIN_FANOUT_CHECK_ENABLED", True)
        monkeypatch.setattr(
            gate, "_relation_cardinalities", lambda db, kb_id: [_GATE_RELATION],
        )
        import sqlglot

        parsed = sqlglot.parse_one(self._gate_sql(), read="mysql")
        alias_map = {"o": "orders", "c": "customers"}
        warnings = gate._join_fanout_warnings(parsed, None, "kb-1", alias_map)
        assert warnings, "SUM over the many side of a 1:N join must warn"
        assert any("one-to-many" in w for w in warnings)

    def test_no_warning_when_flag_off(self, monkeypatch):
        from app.config import settings
        from app.services.db import execution_gate as gate

        monkeypatch.setattr(settings, "NL2SQL_JOIN_FANOUT_CHECK_ENABLED", False)
        monkeypatch.setattr(gate, "_relation_cardinalities", lambda db, kb_id: [])
        import sqlglot

        parsed = sqlglot.parse_one(self._gate_sql(), read="mysql")
        assert gate._join_fanout_warnings(parsed, None, "kb-1", {}) == []

    def test_warning_is_not_a_refusal(self, monkeypatch):
        """Warnings never fail the gate (schema_validator contract):
        the payload must stay ok=True with the warning attached."""
        from app.config import settings
        from app.services.db import execution_gate as gate

        monkeypatch.setattr(settings, "NL2SQL_JOIN_FANOUT_CHECK_ENABLED", True)
        monkeypatch.setattr(
            gate, "_relation_cardinalities", lambda db, kb_id: [_GATE_RELATION],
        )
        monkeypatch.setattr(gate, "_load_source", lambda db, kb_id: _GateSource())
        monkeypatch.setattr(
            gate, "_known_tables", lambda db, kb_id: {"orders", "customers"},
        )
        schema = {
            "orders": ["order_id", "customer_id", "order_qty"],
            "customers": ["customer_id", "customer_name"],
        }
        monkeypatch.setattr(
            gate, "_described_columns",
            lambda db, kb_id, table: schema.get(table.split(".")[-1].lower()),
        )
        monkeypatch.setattr(
            gate, "_configured_row_filters", lambda db, kb_id: [],
        )

        payload = gate.enforce(None, "kb-1", self._gate_sql())
        assert payload["ok"] is True
        assert payload["checks"]["join_fanout"] == "warning"
        assert payload["detail"]["fanout_warnings"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
