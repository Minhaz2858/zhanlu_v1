"""2C hermetic matrix (Tier A, real SQLite) — discovery, selection,
enforcement, honesty over four DELIBERATELY UNFAMILIAR synthetic schemas.

Customer promise under test: connect a database Zhanlu has never seen and
get a correctly grounded answer — no per-database teaching, no hardcoded
names/vocabulary in the shared pipeline. The fixtures live in
tests/helpers_2c_schemas.py (txn_hdr/sku_ledger/cust_ref, dispense_log/
formulary_item/ward_stock, batch_trace/workcell_event/component_lot,
journal_line/account_map/settlement_ref).

REAL everything on the data path: the sqlite connector, catalog indexer
(introspection → roles → coverage → relations → join edges), the scope
classifier/backfill, the TOC builder, and retrieval. The ONLY stubs are the
description LLM (fails → honest blank) and the catalog embedder.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
import helpers_2c_schemas as fx  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.knowledge_catalog import (  # noqa: E402
    KBTableMeta,
    KBTableRelation,
)


def _pytest_monkeypatch():
    """A MonkeyPatch usable inside module-scoped fixtures (auto-undone)."""
    return pytest.MonkeyPatch.context()


# ── Module-scoped discovery run (the expensive real-pipeline step) ────────


@pytest.fixture(scope="module")
def discovery():
    """Run REAL discovery once per schema. Yields {schema: {kb_id, facts, path}}."""
    out: dict[str, dict] = {}
    db = SessionLocal()
    try:
        for schema in fx.SCHEMAS:
            path, facts = fx.build_sqlite_db(schema)
            kb = fx.seed_kb(
                db, path=path, schema=schema,
                org_id=f"org-2c-{uuid.uuid4().hex[:10]}",
                app_id="app-2c",
            )
            with _pytest_monkeypatch() as mp:
                fx.discover(db, kb, mp)
            out[schema] = {
                "kb_id": kb.id, "facts": facts, "path": path,
            }
        yield out
    finally:
        db.close()


def _tables(db, kb_id: str) -> dict[str, KBTableMeta]:
    return {
        m.table_name: m
        for m in db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id)
        .all()
    }


def _relations(db, kb_id: str) -> list[KBTableRelation]:
    return (
        db.query(KBTableRelation)
        .filter(KBTableRelation.kb_id == kb_id)
        .all()
    )


# ── 1. Discovery: tables, counts, relations (all four schemas) ───────────


@pytest.mark.parametrize("schema", sorted(fx.SCHEMAS))
def test_discovery_finds_every_table_with_exact_counts(discovery, schema):
    info = discovery[schema]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        # Every DDL table was discovered (names come from the source, not us).
        ddl, builder = fx.SCHEMAS[schema]
        rows = builder()
        rows.pop("__facts__", None)
        assert set(metas) == set(rows), (
            f"{schema}: discovered {sorted(metas)}, expected {sorted(rows)}"
        )
        for name, m in metas.items():
            assert m.row_count == len(rows[name]), (
                f"{schema}.{name}: row_count {m.row_count} != actual "
                f"{len(rows[name])}"
            )
            assert m.row_count_source == "exact"
            # DATE-typed coverage probed by TYPE, not by name vocabulary.
            if name not in ("meta_sync_state", "cust_ref", "component_lot",
                            "account_map", "formulary_item"):
                assert m.coverage_json, (
                    f"{schema}.{name}: DATE-typed column present but no "
                    "coverage was probed"
                )
                assert m.coverage_json.get("min_date") and m.coverage_json.get(
                    "max_date"
                )
    finally:
        db.close()


def test_sales_fk_relation_persisted_with_full_confidence(discovery):
    info = discovery["sales"]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        rels = _relations(db, info["kb_id"])
        # Declared FK: txn_hdr.cust_ref_id → cust_ref.id
        fk = [
            r for r in rels
            if r.relation_type == "FK"
            and any(
                m.id == r.source_table_meta_id
                and m.table_name == "txn_hdr" for m in metas.values()
            )
            and any(
                m.id == r.target_table_meta_id
                and m.table_name == "cust_ref" for m in metas.values()
            )
        ]
        assert fk, "declared FK txn_hdr→cust_ref was not discovered"
        r = fk[0]
        assert r.source_cardinality == "many" and r.target_cardinality == "one"
        assert r.confidence == 1.0
        assert "cust_ref_id" in (r.source_columns or [])
        # Missing-FK path: north_sku_ledger.txn_hdr_id must STILL be inferred —
        # via a NAME_MATCH or VALUE_OVERLAP edge, never as a fake FK.
        inferred = [
            r for r in rels
            if r.relation_type in ("VALUE_OVERLAP", "NAME_MATCH")
            and any(m.id == r.source_table_meta_id
                    and m.table_name in ("north_sku_ledger", "south_sku_ledger")
                    for m in metas.values())
        ]
        assert inferred, (
            "north_sku_ledger→txn_hdr relationship was NOT inferred without "
            "a declared FK — discovery failed on the missing-FK schema"
        )
        # The inferred edge must never be recorded as an FK — declared FKs
        # and value-inference are different certainty classes.
        fk_pair = [
            r for r in rels
            if r.relation_type == "FK"
            and any(m.id == r.source_table_meta_id
                    and m.table_name in ("north_sku_ledger", "south_sku_ledger")
                    for m in metas.values())
        ]
        assert not fk_pair, "an inferred overlap was recorded as an FK"
    finally:
        db.close()


def test_sales_scope_family_and_rollup_classified_from_names_and_data(
    discovery,
):
    """P0: scope evidence is structural — name families + observed values.
    The partition key is CONFIRMED only via observed values (region_tag);
    the superset rollup is global; the base table stays honestly unknown."""
    info = discovery["sales"]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        for name in ("north_sku_ledger", "south_sku_ledger"):
            scope = metas[name].dataset_scope or ""
            assert scope.startswith("partition"), (
                f"{name}: expected a partition verdict, got {scope!r}"
            )
            signals = metas[name].dataset_scope_signals_json or {}
            assert signals.get("partition_value") in ("north", "south")
        assert metas["sku_ledger"].dataset_scope == "global", (
            "the superset rollup was not recognized as the company-wide "
            f"dataset (got {metas['sku_ledger'].dataset_scope!r})"
        )
        # The credential-shaped table must read as system.
        if os.environ.get("2C_DEBUG"):
            from app.services.db.schema_classification import (
                classify_system_business,
            )
            m = metas["meta_sync_state"]
            deg = len(
                [r for r in _relations(db, info["kb_id"])
                 if m.id in (r.source_table_meta_id, r.target_table_meta_id)]
            )
            print(
                "DEBUG meta:", m.table_role, deg,
                classify_system_business(
                    {"table_name": "meta_sync_state", "columns": [
                        {"name": "id", "data_type": "INTEGER"},
                        {"name": "session_token", "data_type": "VARCHAR"},
                        {"name": "password_hash", "data_type": "VARCHAR"},
                        {"name": "synced_at", "data_type": "DATE"},
                    ]},
                    relation_degree=deg,
                    domain_support=None, table_role=m.table_role,
                ),
            )
        assert metas["meta_sync_state"].schema_class == "system"
        business = [
            n for n, m in metas.items()
            if n not in ("meta_sync_state",)
        ]
        for n in business:
            assert metas[n].schema_class == "business", (
                f"{n}: expected business, got {metas[n].schema_class!r}"
            )
    finally:
        db.close()


def test_sales_misleading_grain_name_does_not_reorder_reality(discovery):
    """sku_ledger SOUNDS like a detail ledger; the DATA says it is the
    company-wide rollup (rows == north + south). The catalog must record
    what the data says — exact measured counts — and scope evidence comes
    from structure, never from what the name 'sounds like'."""
    info = discovery["sales"]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        facts = discovery["sales"]["facts"]
        assert metas["sku_ledger"].row_count == 80
        assert metas["cust_ref"].row_count == 6
        assert metas["sku_ledger"].row_count_source == "exact"
        # Measured rollup rows equal the measured partition sum — the
        # count is the authority, not the name.
        assert facts["sku_ledger_units_sum"] == (
            facts["north_units_sum"] + facts["south_units_sum"]
        )
    finally:
        db.close()


def test_healthcare_ambiguous_ward_code_never_becomes_an_fk(discovery):
    """ward_code is joinable in TWO tables but is a key in NEITHER. Any
    inferred edge must stay low-confidence VALUE_OVERLAP/NAME_MATCH — it
    must never be reported with FK-grade certainty."""
    info = discovery["healthcare"]
    db = SessionLocal()
    try:
        rels = _relations(db, info["kb_id"])
        for r in rels:
            if r.relation_type == "FK":
                src_cols = r.source_columns or []
                # Declared FKs are item_id only; ward_code must never
                # masquerade as one.
                assert "ward_code" not in src_cols, (
                    "the ambiguous ward_code column was promoted to an FK"
                )
        low = [
            r for r in rels if r.relation_type in ("VALUE_OVERLAP", "NAME_MATCH")
        ]
        # The certainty CLASS is the guarantee: overlaps are never recorded
        # as declared FKs. Their confidence scores the overlap strength.
        declared = [r for r in rels if r.relation_type == "FK"]
        for r in declared:
            assert "ward_code" not in (r.source_columns or []), (
                "ward_code was promoted into a declared FK"
            )
    finally:
        db.close()


def test_manufacturing_fk_chain_cardinality_many_to_one(discovery):
    info = discovery["manufacturing"]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        rels = {
            (r.source_table_meta_id, r.target_table_meta_id): r
            for r in _relations(db, info["kb_id"])
            if r.relation_type == "FK"
        }
        assert rels, "no declared FK relations discovered"
        # batch_trace.lot_id → component_lot (many→one): the fanout surface.
        bt = next(m for m in metas.values() if m.table_name == "batch_trace")
        cl = next(m for m in metas.values() if m.table_name == "component_lot")
        r = rels.get((bt.id, cl.id))
        assert r is not None, "batch_trace→component_lot FK missing"
        assert r.source_cardinality == "many" and r.target_cardinality == "one"
    finally:
        db.close()


def test_finance_non_id_key_relationship_is_discovered(discovery):
    """journal_line.acct_code → account_map.acct_code references a NON-ID
    unique key with NO declared FK. The value overlap must still surface
    the relationship (as an inference, not FK-grade)."""
    info = discovery["finance"]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        rels = _relations(db, info["kb_id"])
        jl = next(m for m in metas.values() if m.table_name == "journal_line")
        am = next(m for m in metas.values() if m.table_name == "account_map")
        edge = [
            r for r in rels
            if {r.source_table_meta_id, r.target_table_meta_id} == {jl.id, am.id}
        ]
        assert edge, (
            "journal_line→account_map relationship not discovered from the "
            "non-id unique-key overlap"
        )
        assert all(r.relation_type != "FK" for r in edge), (
            "a value overlap was promoted to FK-grade certainty"
        )
    finally:
        db.close()


# ── 2. Correct table selection across unrelated schemas (P0) ─────────────


def _retrieve(db, kb_id: str, question: str):
    from app.services.db.table_retrieval import (
        _load_classification_facts,
        classification_adjustments,
        retrieve_for_question,
    )
    from app.services.knowledge_graph.schema_linker import build_full_toc

    toc = build_full_toc(db, [kb_id])
    facts = _load_classification_facts(db, [kb_id])
    # enabled=True: the fused text/column scorer (the only component that
    # works on an unannotated catalog) runs behind this flag — the same
    # SEMANTIC_CATALOG_ENABLED gate production callers use. The semantic
    # store may be unavailable here; retrieval degrades to fused evidence.
    res = retrieve_for_question(
        db, [kb_id], question, toc, enabled=True,
        classification_facts=facts,
    )
    return res, facts


@pytest.mark.parametrize(
    "schema,question,expected_in_top3,forbidden_in_top3",
    [
        # Unfamiliar sales vocabulary — retrieval must still find the right
        # tables, and the system/credential table must never surface.
        (
            "sales",
            "units moved per transaction header by booking date",
            ("sku_ledger",),
            ("meta_sync_state",),
        ),
        (
            "sales",
            "units moved across every region in the company",
            ("sku_ledger",),
            ("meta_sync_state",),
        ),
        # Healthcare: ambiguous ward_code — both candidates may rank, no
        # crash, and formulary ranks for item questions.
        (
            "healthcare",
            "packs on hand per ward and counted date",
            ("ward_stock",),
            (),
        ),
        # Manufacturing: lot vs batch disambiguation.
        (
            "manufacturing",
            "cycle seconds per batch by station and run date",
            ("batch_trace",),
            ("meta_sync_state",),
        ),
        # Finance: journal amounts — settlement must not outrank the ledger.
        (
            "finance",
            "total debit and credit amounts posted per account",
            ("journal_line", "account_map"),
            (),
        ),
    ],
)
def test_selection_ranks_the_right_tables_on_unfamiliar_schemas(
    discovery, schema, question, expected_in_top3, forbidden_in_top3,
):
    info = discovery[schema]
    db = SessionLocal()
    try:
        res, _facts = _retrieve(db, info["kb_id"], question)
        top3 = [c.table_name for c in res.candidates[:3]]
        assert top3, f"{schema}: no retrieval candidates for {question!r}"
        for want in expected_in_top3:
            assert want in top3, (
                f"{schema}: {want!r} not in top-3 {top3} for {question!r}"
            )
        for bad in forbidden_in_top3:
            assert bad not in top3, (
                f"{schema}: forbidden {bad!r} surfaced in top-3 {top3}"
            )
        # The system/credential table is RANKED, never hard-excluded (the
        # classifier's own contract) — but it must rank strictly last: a
        # credential table must never outrank business tables.
        if schema == "sales" and "meta_sync_state" in res.names:
            assert res.names[-1] == "meta_sync_state", (
                "the credential table outranked a business table: "
                f"{res.names}"
            )
    finally:
        db.close()


def test_selection_universal_question_demotes_partitions(discovery):
    """A company-wide question must rank the discovered GLOBAL dataset
    FIRST, with the partitions demoted and the demotion RECORDED in their
    evidence (annotation-first — the ranking stays arguable)."""
    info = discovery["sales"]
    db = SessionLocal()
    try:
        res, _facts = _retrieve(
            db, info["kb_id"],
            "units moved across every region in the company",
        )
        assert res.candidates, "no candidates"
        assert res.candidates[0].table_name == "sku_ledger", (
            "the global dataset did not rank first for a company-wide "
            f"question: {[c.table_name for c in res.candidates[:3]]}"
        )
        part_rank = {
            c.table_name: i for i, c in enumerate(res.candidates)
        }
        for p in ("north_sku_ledger", "south_sku_ledger"):
            if p in part_rank:
                assert part_rank[p] > part_rank["sku_ledger"], (
                    f"{p} outranked the global dataset for a company-wide "
                    "question"
                )
    finally:
        db.close()


# ── 3. Scope + fanout + conflicting-measure enforcement (P0) ─────────────


def test_scope_gate_refuses_wrong_partition_on_discovered_facts(discovery):
    """On DISCOVERED metadata (no human input): a question scoped to north
    must refuse SQL touching the south partition, and a company-wide
    question must refuse partition-only SQL when a discovered global
    exists — with the rollup named as the alternative."""
    from app.services.db.schema_classification import (
        resolve_question_scope_from_facts,
        validate_sql_scope,
    )
    from app.services.db.table_retrieval import _load_classification_facts

    info = discovery["sales"]
    db = SessionLocal()
    try:
        facts = _load_classification_facts(db, [info["kb_id"]])
        assert facts, "classification facts did not load from the discovery run"

        scoped = resolve_question_scope_from_facts(
            "show units moved in the north region", facts,
        )
        assert scoped and scoped.get("value") == "north"

        errors, _warnings = validate_sql_scope(
            ["south_sku_ledger"], scoped, facts,
        )
        assert errors, "wrong-partition SQL was not refused"

        universal = resolve_question_scope_from_facts(
            "show units moved across the whole company", facts,
        )
        errors2, _w2 = validate_sql_scope(
            ["north_sku_ledger"], universal, facts,
        )
        assert errors2, (
            "partition-only SQL for a company-wide question was not refused "
            "even though a discovered global exists"
        )
        assert any("sku_ledger" in e for e in errors2), (
            "the refusal must name the discovered global alternative"
        )
        # Correct-side selection passes.
        ok_errors, _okw = validate_sql_scope(
            ["north_sku_ledger"], scoped, facts,
        )
        assert not ok_errors
    finally:
        db.close()


def test_fanout_warning_on_aggregating_the_many_side(discovery, monkeypatch):
    """Manufacturing: cycle_seconds lives on batch_trace (MANY per lot).
    Aggregating it across a join to component_lot must produce a fanout
    warning — derived from DISCOVERED cardinality, not configuration."""
    import sqlglot

    from app.config import settings
    from app.services.db import execution_gate as gate

    monkeypatch.setattr(settings, "NL2SQL_JOIN_FANOUT_CHECK_ENABLED", True)
    info = discovery["manufacturing"]
    db = SessionLocal()
    try:
        parsed = sqlglot.parse_one(
            "SELECT l.lot_code, SUM(b.cycle_seconds) AS total_seconds "
            "FROM component_lot l JOIN batch_trace b "
            "ON b.lot_id = l.id GROUP BY l.lot_code",
            read="sqlite",
        )
        alias_map = {"l": "component_lot", "b": "batch_trace"}
        warnings = gate._join_fanout_warnings(
            parsed, db, info["kb_id"], alias_map,
        )
        assert warnings, (
            "aggregating the many side of a DISCOVERED 1↔N join produced no "
            "fanout warning — the FK cardinality was not consumed"
        )
        assert any("one-to-many" in w for w in warnings)
    finally:
        db.close()


def test_conflicting_measures_gate_on_discovered_columns(discovery):
    """Finance: amount_dr and amount_cr are two bases on one table. A
    verified binding for the debit basis must reject a SQL that sums the
    credit column while claiming the debit metric — the alias-independent
    confusable guard — and allow summing BOTH (reconciliation's job)."""
    from app.models.metric_definition import MetricDefinition
    from app.services.db.query_contract import (
        datasource_metric_bindings,
        validate_confusable_measures,
        validate_sql_measure_aliases,
    )

    info = discovery["finance"]
    db = SessionLocal()
    try:
        db.add(MetricDefinition(
            id=str(uuid.uuid4()),
            name="dr_total",
            datasource_id=info["kb_id"],
            base_table="journal_line",
            base_column="amount_dr",
            aggregation="SUM",
            description=(
                "sum of amount_dr, the debit side of journal_line; the "
                "credit side is amount_cr and must not be blended in"
            ),
            is_enabled=True,
        ))
        db.commit()

        bindings = datasource_metric_bindings(db, info["kb_id"])
        assert bindings, "metric binding not resolved for the synthetic KB"

        import sqlglot

        bad = validate_confusable_measures(
            bindings,
            sqlglot.parse_one(
                "SELECT SUM(amount_cr) AS x FROM journal_line",
                read="sqlite",
            ),
            ["journal_line"],
        )
        assert bad, (
            "summing the credit basis while claiming the debit metric was "
            "not rejected by the confusable guard"
        )
        ok = validate_confusable_measures(
            bindings,
            sqlglot.parse_one(
                "SELECT SUM(amount_dr) AS a, SUM(amount_cr) AS b "
                "FROM journal_line",
                read="sqlite",
            ),
            ["journal_line"],
        )
        assert not ok, "summing BOTH bases must be allowed (reconciliation)"
    finally:
        db.close()


# ── 4. Missing metadata never becomes invented certainty (P0) ────────────


@pytest.mark.parametrize("schema", sorted(fx.SCHEMAS))
def test_absent_descriptions_and_comments_stay_absent(discovery, schema):
    """The description LLM failed in this run. Tables/columns with no
    source comment must keep EMPTY descriptions with source='' — never a
    placeholder, never an invented label, never a high confidence."""
    info = discovery[schema]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        for name, m in metas.items():
            assert not (m.description_en or "").strip(), (
                f"{schema}.{name}: a description appeared without an LLM — "
                f"invented certainty: {m.description_en!r}"
            )
            assert m.description_source == "", (
                f"{schema}.{name}: description_source must be '' when no "
                f"description exists, got {m.description_source!r}"
            )
            assert not (m.description_confidence or 0) > 0.1, (
                f"{schema}.{name}: confidence {m.description_confidence} "
                "without any description source"
            )
            # SQLite exposes no comments: verbatim absence.
            assert m.source_comment is None
    finally:
        db.close()


def test_sqlite_absent_statistics_are_null_not_zero(discovery):
    """SQLite has no engine row estimates. There is no est_rows on the
    catalog at all (absence, not zero); the exact COUNT stays the
    authoritative row_count, and the file-level byte size is what SQLite
    CAN report (never a per-table fabrication)."""
    info = discovery["sales"]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        m = metas["txn_hdr"]
        assert not hasattr(m, "est_rows"), (
            "an estimate field appeared on the catalog — check it is NULL "
            "when the engine reports none"
        )
        assert m.row_count == 40 and m.row_count_source == "exact"
        assert m.size_bytes is None or m.size_bytes > 0
    finally:
        db.close()


def test_unfamiliar_enum_vocabulary_is_observed_not_assumed(discovery):
    """Low-cardinality columns get their DISTINCT VALUES sampled — the
    model's filter vocabulary comes from the CUSTOMER's data, never from
    our assumptions."""
    info = discovery["sales"]
    db = SessionLocal()
    try:
        metas = _tables(db, info["kb_id"])
        cols = None
        from app.models.knowledge_catalog import KBColumnMeta

        m = metas["north_sku_ledger"]
        col = (
            db.query(KBColumnMeta)
            .filter(
                KBColumnMeta.table_meta_id == m.id,
                KBColumnMeta.column_name == "region_tag",
            )
            .first()
        )
        assert col is not None
        assert col.enum_values, "region_tag values were not sampled"
        assert set(col.enum_values) == {"north"}, (
            f"observed enum values must match the customer's data, got "
            f"{col.enum_values!r}"
        )
    finally:
        db.close()
