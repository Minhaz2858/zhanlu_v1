"""Stage 2B-3 — scope_authorization: enforcement at the boundaries.

Acceptance shape (authorization 2026-10-30):

* a company-wide question must never be answered solely from CONFIRMED
  partition tables while a verified global dataset exists — refused at the
  execution boundary, clarified at the plan (P-SV) level;
* a question that names a partition must never read a DIFFERENT confirmed
  partition — refused (execution) / removed-with-disclosure (plan);
* unknown scope is NEVER convicted: no evidence preserves legacy behavior,
  and a ``partition_family`` guess is not a verified partition;
* human overrides win via ``effective_classification``;
* honest degradation: a catalog problem degrades to not-applicable /
  unavailable — it never fabricates a refusal and never blocks answering;
* flags ship OFF: with them off, behavior is byte-identical legacy.

Zero customer-specific names: every scenario is a synthetic schema.
"""

from __future__ import annotations

import contextlib
import uuid as _uuid

import pytest

from app.config import settings
from app.services.db import execution_gate as gate
from app.services.db import query_service as qs
from app.services.db.execution_gate import ExecutionValidationError
from app.services.db.schema_classification import validate_sql_scope


# ── the pure core ───────────────────────────────────────────────────────────


def _f(ds=None, pv=None, source="human", conf=1.0):
    return {
        "dataset_scope": ds,
        "dataset_scope_source": source,
        "dataset_scope_confidence": conf,
        "partition_value": pv,
    }


_FACTS = {
    "orders_north": _f("partition:region", "north"),
    "orders_south": _f("partition:region", "south"),
    "all_orders_rollup": _f("global"),
    "dim_customer": _f(),  # unknown scope
}


class TestValidateSqlScopePure:
    def test_companywide_from_partitions_with_global_refused(self):
        errors, warnings = validate_sql_scope(
            ["orders_north"], None, _FACTS,
        )
        assert errors, "single-partition answer to a company-wide question"
        assert "all_orders_rollup" in errors[0], "the refusal names the fix"
        assert warnings == []

    def test_companywide_from_partitions_without_global_warns(self):
        facts = {k: v for k, v in _FACTS.items() if k != "all_orders_rollup"}
        errors, warnings = validate_sql_scope(["orders_north"], None, facts)
        assert errors == [], "no alternative exists — refusing is dishonest"
        assert warnings, "the limitation must still be recorded"

    def test_companywide_with_unknown_scope_table_is_allowed(self):
        """Any unknown-scope table in the statement breaks the all-partition
        condition — unknown ≠ a verified partition, in both directions."""
        errors, _ = validate_sql_scope(
            ["orders_north", "dim_customer"], None, _FACTS,
        )
        assert errors == []

    def test_companywide_with_global_table_selected_is_allowed(self):
        errors, _ = validate_sql_scope(
            ["all_orders_rollup"], None, _FACTS,
        )
        assert errors == []

    def test_partition_family_guess_never_convicts(self):
        facts = dict(_FACTS, orders_north=_f("partition_family", "north"))
        errors, _ = validate_sql_scope(["orders_north"], None, facts)
        assert errors == []

    def test_partition_family_warns_for_companywide(self):
        """Reviewer table row 3: a probable partition must not silently
        claim completeness — warned, never refused (the key is unverified)."""
        facts = dict(_FACTS, orders_north=_f("partition_family", "north"))
        errors, warnings = validate_sql_scope(["orders_north"], None, facts)
        assert errors == []
        assert warnings and "could not be fully verified" in warnings[0]

    def test_partition_family_with_verified_global_touched_is_silent(self):
        """A verified global among the touched tables makes completeness
        achievable — no uncertainty is claimed."""
        facts = dict(_FACTS, orders_north=_f("partition_family", "north"))
        errors, warnings = validate_sql_scope(
            ["orders_north", "all_orders_rollup"], None, facts,
        )
        assert errors == [] and warnings == []

    def test_human_override_beats_inferred_partition(self):
        """A human 'global' verdict on a partition-SHAPED table is a fact —
        the company-wide question passes without warning or refusal."""
        facts = dict(_FACTS, orders_north=_f("global", source="human"))
        errors, warnings = validate_sql_scope(["orders_north"], None, facts)
        assert errors == [] and warnings == []

    def test_wrong_partition_refused(self):
        errors, _ = validate_sql_scope(
            ["orders_south"], {"value": "north"}, _FACTS,
        )
        assert errors and "orders_south" in errors[0]

    def test_matching_partition_allowed(self):
        errors, _ = validate_sql_scope(
            ["orders_north"], {"value": "north"}, _FACTS,
        )
        assert errors == []

    def test_unknown_scope_on_explicit_question_allowed(self):
        errors, _ = validate_sql_scope(
            ["dim_customer"], {"value": "north"}, _FACTS,
        )
        assert errors == []

    def test_empty_facts_degrade_to_nothing(self):
        assert validate_sql_scope(["orders_north"], None, {}) == ([], [])
        assert validate_sql_scope([], None, _FACTS) == ([], [])

    def test_sql_identifier_case_insensitive(self):
        errors, _ = validate_sql_scope(["ORDERS_NORTH"], None, _FACTS)
        assert errors, "facts lookup must survive the engine's lowercasing"


# ── the execution boundary ──────────────────────────────────────────────────


class _Source:
    def __init__(self, db_type="mysql"):
        self.id = "kb-scope"
        self.name = "warehouse"
        self.db_type = db_type
        self.source_kind = "database"
        self.is_deleted = False


SCHEMA = {
    "orders_north": ["order_id", "amount", "created_at"],
    "orders_south": ["order_id", "amount", "created_at"],
    "all_orders_rollup": ["order_id", "amount", "created_at"],
}
SQL_PARTITION = "SELECT SUM(amount) FROM orders_north"
SQL_GLOBAL = "SELECT SUM(amount) FROM all_orders_rollup"


def _stub_source(monkeypatch):
    monkeypatch.setattr(gate, "_load_source", lambda db, kb_id: _Source())
    tables = {t.lower() for t in SCHEMA}
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: tables)
    monkeypatch.setattr(
        gate, "_described_columns",
        lambda db, kb_id, t: list(SCHEMA.get(t.split(".")[-1].lower(), [])) or None,
    )
    monkeypatch.setattr(gate, "_configured_row_filters", lambda db, kb_id: [])


_SCOPE_CTX = {
    "classification_facts": _FACTS,
    "question_scope": None,  # company-wide
}


def test_gate_flag_off_is_byte_identical_legacy(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(
        settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", False
    )
    payload = gate.enforce(None, "kb-scope", SQL_PARTITION, scope_context=_SCOPE_CTX)
    assert payload["ok"] is True
    assert payload["checks"]["scope_authorization"] == "not-in-scope"


def test_gate_refuses_companywide_from_partition_with_global(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True)
    with pytest.raises(ExecutionValidationError) as exc:
        gate.enforce(None, "kb-scope", SQL_PARTITION, scope_context=_SCOPE_CTX)
    assert "all_orders_rollup" in str(exc.value)


def test_gate_allows_companywide_from_partition_without_global(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True)
    ctx = {
        "classification_facts": {
            k: v for k, v in _FACTS.items() if k != "all_orders_rollup"
        },
        "question_scope": None,
    }
    payload = gate.enforce(None, "kb-scope", SQL_PARTITION, scope_context=ctx)
    assert payload["ok"] is True
    assert payload["checks"]["scope_authorization"] == "pass"
    assert payload["detail"]["scope_warnings"], "honest, not silent"


def test_gate_refuses_wrong_partition(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True)
    ctx = dict(_SCOPE_CTX, question_scope={"value": "north"})
    with pytest.raises(ExecutionValidationError):
        gate.enforce(None, "kb-scope", "SELECT SUM(amount) FROM orders_south",
                     scope_context=ctx)


def test_gate_without_facts_records_not_applicable(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True)
    payload = gate.enforce(None, "kb-scope", SQL_PARTITION)
    assert payload["ok"] is True
    assert payload["checks"]["scope_authorization"] == "not-applicable"


def test_gate_scope_check_failure_degrades_never_refuses(monkeypatch):
    _stub_source(monkeypatch)
    monkeypatch.setattr(settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True)
    import app.services.db.schema_classification as sc

    def _boom(*a, **k):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(sc, "validate_sql_scope", _boom)
    payload = gate.enforce(None, "kb-scope", SQL_PARTITION, scope_context=_SCOPE_CTX)
    assert payload["ok"] is True, "a broken check must not fabricate a refusal"
    assert payload["checks"]["scope_authorization"] == "unavailable"


def test_query_service_passes_scope_context_and_gate_refuses(monkeypatch):
    """The boundary wiring: scope_context set on QueryService reaches the
    gate and a violating statement is refused BEFORE the driver sees it."""
    _stub_source(monkeypatch)
    monkeypatch.setattr(settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True)
    monkeypatch.setattr(
        qs.QueryService, "_load_kb", lambda self, kb_id: _Source()
    )

    @contextlib.contextmanager
    def _factory(kb):
        raise AssertionError("connector must never be reached on a refusal")
        yield None  # pragma: no cover

    monkeypatch.setattr(qs, "get_connector", _factory)
    svc = qs.QueryService(None, plan=None)
    svc.scope_context = _SCOPE_CTX
    with pytest.raises(ExecutionValidationError):
        svc.execute("kb-scope", SQL_PARTITION)


# ── the plan level (P-SV) ───────────────────────────────────────────────────


@pytest.fixture()
def scope_db():
    import os

    os.environ.setdefault(
        "DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true"
    )
    from app.database import Base, SessionLocal, engine
    import app.models  # noqa: F401

    Base.metadata.create_all(engine)
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta

    kb_id = f"kb-2b3-{_uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    db.add(KnowledgeBase(
        id=kb_id, name="2b3", source_kind="database", db_type="mysql",
    ))
    for name, scope, conf, pv in (
        ("orders_north", "partition:region", 0.6, "north"),
        ("orders_south", "partition:region", 0.6, "south"),
        ("all_orders_rollup", "global", 0.6, None),
    ):
        signals = {"partition_value": pv} if pv else {}
        db.add(KBTableMeta(
            id=str(_uuid.uuid4()), kb_id=kb_id, schema_name="",
            table_name=name, table_type="TABLE",
            dataset_scope=scope, dataset_scope_confidence=conf,
            dataset_scope_signals_json=signals,
        ))
    db.commit()
    yield db, kb_id
    db.close()


def _verify(db, kb_id, question, selected, *, flag=True, project_id=None):
    old = settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED
    settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = flag
    try:
        from app.services.db.source_verification import verify_sources

        return verify_sources(db, kb_id, question, selected, project_id)
    finally:
        settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = old


def test_psv_clarifies_companywide_when_global_exists(scope_db):
    db, kb_id = scope_db
    res = _verify(db, kb_id, "company-wide totals", ["orders_north"])
    assert res.clarify and "all_orders_rollup" in res.clarify
    assert res.passed is False, "clarify must actually stop the turn"


def test_psv_discloses_companywide_without_global(scope_db):
    from app.models.knowledge_catalog import KBTableMeta

    db, kb_id = scope_db
    db.query(KBTableMeta).filter(
        KBTableMeta.kb_id == kb_id,
        KBTableMeta.table_name == "all_orders_rollup",
    ).delete()
    db.commit()
    res = _verify(db, kb_id, "company-wide totals", ["orders_north"])
    assert res.clarify is None
    assert res.passed is True, "nothing better exists — answer, disclosed"
    assert any("partition" in d for d in res.disclosures)


def test_psv_removes_wrong_partition_with_disclosure(scope_db):
    db, kb_id = scope_db
    res = _verify(db, kb_id, "north totals", ["orders_south", "orders_north"])
    assert res.clarify is None
    assert "orders_south" in res.removed_tables
    assert any("different partition" in d for d in res.disclosures)


def test_psv_clarifies_when_wrong_partition_was_the_only_plan(scope_db):
    db, kb_id = scope_db
    res = _verify(db, kb_id, "north totals", ["orders_south"])
    assert res.clarify, "a plan of only the wrong partition must stop"
    assert res.passed is False


def test_psv_flag_off_preserves_legacy_behavior(scope_db):
    db, kb_id = scope_db
    res = _verify(
        db, kb_id, "company-wide totals", ["orders_north"], flag=False,
    )
    assert res.clarify is None
    assert res.passed is True
    assert res.disclosures == []


def test_psv_partition_family_discloses_uncertainty_not_refusal(scope_db):
    """Reviewer table row 3, plan level: a probable partition with the key
    unidentified answers with an uncertainty disclosure — never a refusal,
    never a silent pass."""
    from app.models.knowledge_catalog import KBTableMeta

    db, kb_id = scope_db
    db.query(KBTableMeta).filter(
        KBTableMeta.kb_id == kb_id,
        KBTableMeta.table_name == "orders_north",
    ).update({"dataset_scope": "partition_family"})
    db.commit()
    res = _verify(db, kb_id, "company-wide totals", ["orders_north"])
    assert res.clarify is None and res.passed is True
    assert any(
        d.startswith("scope:") and "could not be fully verified" in d
        for d in res.disclosures
    )
    # the verified global must be named as the better source when it exists
    assert any("all_orders_rollup" in d for d in res.disclosures)


def test_psv_human_override_wins_over_inferred_partition(scope_db):
    """A human 'global' verdict on a partition-detected table is a fact:
    no clarify, no disclosure, legacy pass."""
    from app.models.knowledge_catalog import ProjectCatalogOverlay

    db, kb_id = scope_db
    project_id = f"proj-{kb_id}"
    db.add(ProjectCatalogOverlay(
        id=str(_uuid.uuid4()), project_id=project_id, kb_id=kb_id,
        table_name="orders_north", dataset_scope_override="global",
    ))
    db.commit()
    res = _verify(
        db, kb_id, "company-wide totals", ["orders_north"],
        project_id=project_id,
    )
    assert res.clarify is None and res.passed is True
    assert not [d for d in res.disclosures if d.startswith("scope:")]


def test_psv_catalog_failure_degrades_without_scope_claims(scope_db, monkeypatch):
    """A broken classification-facts loader must not fabricate scope
    outcomes: the gate passes with no scope clarify/disclosure at all."""
    import app.services.db.table_retrieval as tr

    db, kb_id = scope_db
    def _boom(*a, **k):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(tr, "_load_classification_facts", _boom)
    res = _verify(db, kb_id, "company-wide totals", ["orders_north"])
    assert res.passed is True
    assert not [d for d in res.disclosures if d.startswith("scope:")]
    assert "all_orders_rollup" not in (res.clarify or "")


# ── the sub-loop path (review check 2) ──────────────────────────────────────


async def test_subloop_execute_query_receives_scope_context(monkeypatch):
    """The delegation/DataAgent sub-loop authors raw SQL and executes via
    db_tools._execute_query — its QueryService must carry the scope context
    when the flag is on and the context has a question."""
    import app.services.tool_handlers.db_tools as dbt
    from app.database import Base, SessionLocal, engine
    import app.models  # noqa: F401
    import os

    os.environ.setdefault(
        "DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true"
    )
    Base.metadata.create_all(engine)
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta

    kb_id = f"kb-loop-{_uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        db.add(KnowledgeBase(
            id=kb_id, name="loop", source_kind="database", db_type="mysql",
        ))
        db.add(KBTableMeta(
            id=str(_uuid.uuid4()), kb_id=kb_id, schema_name="",
            table_name="orders_north", table_type="TABLE",
            dataset_scope="partition:region", dataset_scope_confidence=0.6,
            dataset_scope_signals_json={"partition_value": "north"},
        ))
        db.commit()

        captured: dict = {}

        class _StubQS:
            def __init__(self, db_, *a, **k):
                captured["scope_context"] = None
                self.scope_context = None

            def execute(self, kb_id_, sql, **k):
                captured["scope_context"] = self.scope_context
                return {"rows": [{"x": 1}], "row_count": 1, "sql": sql}

        monkeypatch.setattr(dbt, "QueryService", _StubQS)
        monkeypatch.setattr(dbt, "_refuse_if_not_ready", lambda db_, kb_: None)
        monkeypatch.setattr(
            settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False,
        )
        old = settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED
        settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = True
        try:
            result = await dbt._execute_query(
                {"data_source_id": kb_id, "sql": "SELECT 1"},
                db, None,
                context={"question": "north region totals"},
            )
        finally:
            settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = old
        assert result.get("success") is True
        ctx = captured["scope_context"]
        assert ctx and ctx["classification_facts"], "scope context must reach the gate"
        assert ctx["question_scope"] is not None
        assert ctx["question_scope"]["value"] == "north"
    finally:
        db.close()


async def test_subloop_without_question_gets_no_scope_context(monkeypatch):
    """No question in context → intent cannot be judged → no scope context
    (the gate records not-applicable; never a guessed universal refusal)."""
    import app.services.tool_handlers.db_tools as dbt
    from app.database import Base, SessionLocal, engine
    import app.models  # noqa: F401
    import os

    os.environ.setdefault(
        "DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true"
    )
    Base.metadata.create_all(engine)
    from app.models.knowledge_base import KnowledgeBase

    kb_id = f"kb-loop2-{_uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        db.add(KnowledgeBase(
            id=kb_id, name="loop2", source_kind="database", db_type="mysql",
        ))
        db.commit()

        captured: dict = {}

        class _StubQS:
            def __init__(self, db_, *a, **k):
                self.scope_context = None

            def execute(self, kb_id_, sql, **k):
                captured["scope_context"] = self.scope_context
                return {"rows": [], "row_count": 0, "sql": sql}

        monkeypatch.setattr(dbt, "QueryService", _StubQS)
        monkeypatch.setattr(dbt, "_refuse_if_not_ready", lambda db_, kb_: None)
        monkeypatch.setattr(
            settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False,
        )
        old = settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED
        settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = True
        try:
            await dbt._execute_query(
                {"data_source_id": kb_id, "sql": "SELECT 1"}, db, None, context={},
            )
        finally:
            settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = old
        assert captured["scope_context"] is None
    finally:
        db.close()


async def test_subloop_real_gate_refuses_partition_for_companywide(
    scope_db, monkeypatch
):
    """Review requirement 2, end to end with NO stubbed gate: the raw SQL
    execution path (db_tools._execute_query → real QueryService → real
    execution_gate.enforce) INVOKES scope_authorization and refuses a
    confirmed partition answering a company-wide question while a verified
    global exists. Source verification is disabled so the EXECUTION gate's
    refusal is demonstrated in isolation (the plan level has its own test)."""
    import app.services.tool_handlers.db_tools as dbt

    db, kb_id = scope_db
    monkeypatch.setattr(dbt, "_refuse_if_not_ready", lambda db_, kb_: None)
    monkeypatch.setattr(
        settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False,
    )
    old_flags = (
        settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED,
        settings.NL2SQL_SOURCE_VERIFICATION_ENABLED,
    )
    settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = True
    settings.NL2SQL_SOURCE_VERIFICATION_ENABLED = False
    try:
        result = await dbt._execute_query(
            {
                "data_source_id": kb_id,
                "sql": "SELECT SUM(amount) FROM orders_north",
            },
            db, None,
            context={"question": "company-wide totals"},
        )
    finally:
        settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = old_flags[0]
        settings.NL2SQL_SOURCE_VERIFICATION_ENABLED = old_flags[1]
    assert result.get("success") is False, "the real gate must refuse"
    assert "orders_north" in result.get("error", "")
    assert "all_orders_rollup" in result.get("error", ""), (
        "the refusal must name the company-wide alternative"
    )


async def test_subloop_psv_clarifies_partition_for_companywide(scope_db, monkeypatch):
    """Layered defense on the same raw path: with source verification ON the
    P-SV plan-level scope check stops the turn even earlier, with a clarify
    naming the verified global."""
    import app.services.tool_handlers.db_tools as dbt

    db, kb_id = scope_db
    monkeypatch.setattr(dbt, "_refuse_if_not_ready", lambda db_, kb_: None)
    monkeypatch.setattr(
        settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False,
    )
    old_flags = (
        settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED,
        settings.NL2SQL_SOURCE_VERIFICATION_ENABLED,
    )
    settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = True
    try:
        result = await dbt._execute_query(
            {
                "data_source_id": kb_id,
                "sql": "SELECT SUM(amount) FROM orders_north",
            },
            db, None,
            context={"question": "company-wide totals"},
        )
    finally:
        settings.SCHEMA_SCOPE_AUTHORIZATION_ENABLED = old_flags[0]
        settings.NL2SQL_SOURCE_VERIFICATION_ENABLED = old_flags[1]
    assert result.get("success") is False
    assert result.get("error_kind") == "source_verification_clarify"
    assert "all_orders_rollup" in result.get("error", "")



if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
    pytest.main([__file__, "-v"])
