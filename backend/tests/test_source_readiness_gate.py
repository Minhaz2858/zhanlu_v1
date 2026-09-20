"""P1 — source capability profile and NL2SQL readiness gate.

Measured defect this suite pins down (live deployment, 9 sources on :5401):

    db_type     catalog_status   count
    postgresql  ready             3
    postgresql  pending           2     <-- offered to the agent and queried anyway
    mariadb     ready             1
    mysql       ready             1

``catalog_status`` was read only by the indexer, the watchdog and the UI.
Nothing on an answering path asked whether a source had actually been prepared,
so a source whose structure was never catalogued — or whose catalog failed —
still answered questions. These tests make that impossible:

* refusal is a DISCLOSURE in business language, not a crash and not a guess;
* every lifecycle state has a defined verdict (no state falls through to
  "allowed");
* a source that predates this build is *derived*, never assumed ready;
* an unidentifiable dialect is refused rather than executed with another
  vendor's syntax;
* a newly connected database becomes queryable through discovery + capability
  recording alone — no per-source code, no routing rule.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.knowledge_base import KnowledgeBase
import app.models  # noqa: F401  register all models

from app.services.db import readiness as rd


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass


def _kb_obj(**kw):
    """An UNATTACHED KnowledgeBase — for pure-verdict tests (no session)."""
    return _kb(None, **kw)


def _kb(db, **kw):
    """A database source. Only fields the gate reads are ever set."""
    kb_id = str(uuid.uuid4())
    fields = {
        "id": kb_id,
        "name": kw.pop("name", "Warehouse"),
        "source_kind": kw.pop("source_kind", "database"),
        "db_type": kw.pop("db_type", "postgresql"),
        "database_name": "warehouse",
        "status": "active",
        "catalog_status": kw.pop("catalog_status", "ready"),
        "nl2sql_state": kw.pop("nl2sql_state", ""),
        "nl2sql_state_reason": kw.pop("nl2sql_state_reason", None),
    }
    fields.update(kw)
    kb = KnowledgeBase(**fields)
    if db is not None:
        db.add(kb)
        db.commit()
    return kb


# ── 1/2/3. not-prepared sources are refused, with a disclosure ─────────────


@pytest.mark.parametrize(
    "catalog_status,expected_code,expected_state",
    [
        # never prepared (this is the real deployment's state for 2 sources)
        ("pending", "not_prepared", rd.STATE_CONNECTED),
        # preparation failed
        ("error", "discovery_failed", rd.STATE_DISCOVERY_FAILED),
        # preparation running right now
        ("indexing", "discovering", rd.STATE_DISCOVERING),
    ],
)
def test_unprepared_source_is_refused_with_disclosure(
    catalog_status, expected_code, expected_state
):
    kb = _kb_obj( catalog_status=catalog_status)
    verdict = rd.evaluate(kb)
    assert verdict.allowed is False
    assert verdict.limited is False
    assert verdict.code == expected_code
    assert verdict.state == expected_state
    assert verdict.derived is True, "catalog_status-derived state must be flagged"
    assert verdict.disclosure, "a refusal must always carry a disclosure"


def test_refusal_payload_is_business_language_and_machine_actionable(db):
    kb = _kb(db, catalog_status="pending")
    verdict, refusal = rd.check_source(db, kb.id)
    assert verdict.allowed is False
    assert refusal is not None
    # the sentence an agent may echo to a user
    assert refusal["success"] is False
    assert refusal["error_kind"] == "source_not_ready"
    assert refusal["source_ready"] is False
    assert refusal["source_state"] == rd.STATE_CONNECTED
    assert refusal["error"] == verdict.disclosure
    # plumbing stays in machine fields
    assert "catalog_status" in refusal["internal_detail"]


def test_failed_catalog_refusal_names_the_preparation_failure():
    verdict = rd.evaluate(_kb_obj( catalog_status="error"))
    assert verdict.code == "discovery_failed"
    assert "could not be prepared" in verdict.disclosure


def test_gate_disabled_still_records_the_verdict(db, monkeypatch):
    """With the gate off the old behaviour is restored — but disclosed."""
    from app.config import settings

    monkeypatch.setattr(settings, "NL2SQL_READINESS_GATE_ENABLED", False, raising=False)
    kb = _kb(db, catalog_status="pending")
    verdict, refusal = rd.check_source(db, kb.id)
    assert refusal is None, "gate off must not refuse"
    assert verdict.allowed is False, "the verdict itself must still say not-ready"


# ── 4/5/6. every lifecycle state has a defined verdict ─────────────────────


def test_ready_source_is_allowed_and_silent():
    verdict = rd.evaluate(_kb_obj( nl2sql_state=rd.STATE_READY))
    assert verdict.allowed is True
    assert verdict.limited is False
    assert verdict.disclosure == ""
    assert verdict.code == "ready"


def test_partially_enriched_source_is_allowed_with_disclosure():
    verdict = rd.evaluate(_kb_obj( nl2sql_state=rd.STATE_ENRICHMENT_PARTIAL))
    assert verdict.allowed is True, "partial sources may answer…"
    assert verdict.limited is True, "…but never silently"
    assert verdict.disclosure
    assert "part" in verdict.disclosure.lower()


@pytest.mark.parametrize("state", ["indexed", "enriched", "validated"])
def test_stalled_mid_lifecycle_is_refused(state):
    """Preparation stopped before it finished — structure is only partly known."""
    verdict = rd.evaluate(_kb_obj( nl2sql_state=state))
    assert verdict.allowed is False
    assert verdict.code == "stalled"
    assert verdict.disclosure


def test_validation_failed_is_refused():
    verdict = rd.evaluate(_kb_obj( nl2sql_state=rd.STATE_VALIDATION_FAILED))
    assert verdict.allowed is False
    assert verdict.code == "validation_failed"


def test_no_state_falls_through_to_allowed():
    """Exhaustive: every state in the vocabulary produces a decided verdict."""
    for state in rd.ALL_STATES:
        verdict = rd.evaluate(_kb_obj( nl2sql_state=state))
        assert verdict.code, f"{state} produced no verdict code"
        if state != rd.STATE_READY:
            assert not (verdict.allowed and verdict.disclosure == ""), (
                f"{state} is allowed without disclosing anything"
            )


def test_lifecycle_vocabulary_matches_the_spec():
    assert rd.LIFECYCLE == (
        "connected",
        "discovering",
        "indexed",
        "enriched",
        "validated",
        "nl2sql_ready",
    )
    assert set(rd.FAILURE_STATES) == {
        "discovery_failed",
        "enrichment_partial",
        "validation_failed",
    }
    assert set(rd.ALL_STATES) == set(rd.LIFECYCLE) | set(rd.FAILURE_STATES)


def test_set_state_rejects_an_unknown_state(db):
    with pytest.raises(ValueError):
        rd.set_state(db, _kb(db), "totally_ready")


# ── 7. legacy rows are derived, never assumed ──────────────────────────────


def test_legacy_ready_catalog_derives_ready_but_is_flagged():
    verdict = rd.evaluate(_kb_obj( catalog_status="ready", nl2sql_state=""))
    assert verdict.allowed is True
    assert verdict.derived is True, "a pre-lifecycle source must be marked derived"


def test_staged_state_wins_over_legacy_catalog_status():
    """A source that HAS been staged reports its real state, not catalog_status."""
    verdict = rd.evaluate(
        _kb_obj( catalog_status="ready", nl2sql_state=rd.STATE_DISCOVERY_FAILED)
    )
    assert verdict.allowed is False
    assert verdict.derived is False, "staged state is not derived"


def test_derive_state_map():
    assert rd.derive_state("ready") == (rd.STATE_READY, True)
    assert rd.derive_state("indexing") == (rd.STATE_DISCOVERING, True)
    assert rd.derive_state("error") == (rd.STATE_DISCOVERY_FAILED, True)
    assert rd.derive_state("pending") == (rd.STATE_CONNECTED, True)
    assert rd.derive_state(None)[0] == rd.STATE_CONNECTED


# ── 8/9/10. unknown dialect, unknown source, document sources ──────────────


def test_unknown_dialect_is_refused_not_defaulted():
    """Even a fully prepared source cannot run if its dialect is unidentified."""
    verdict = rd.evaluate(_kb_obj( db_type="db2", nl2sql_state=rd.STATE_READY))
    assert verdict.allowed is False
    assert verdict.code == "unknown_dialect"
    assert "cannot be answered" in verdict.disclosure


def test_missing_source_is_refused():
    verdict = rd.evaluate(None)
    assert verdict.allowed is False
    assert verdict.code == "no_such_source"


def test_document_sources_are_not_gated():
    """File/document sources have their own lifecycle and must not be blocked."""
    verdict = rd.evaluate(_kb_obj( source_kind="file", catalog_status="pending"))
    assert verdict.allowed is True
    assert verdict.code == "not_applicable"
    assert verdict.disclosure == ""


def test_verdict_carries_source_identity():
    kb = _kb_obj( name="ERP Warehouse", catalog_status="pending")
    verdict = rd.evaluate(kb)
    assert verdict.source_id == kb.id
    assert verdict.source_name == "ERP Warehouse"
    assert verdict.as_dict()["code"] == verdict.code


def test_list_verdicts_returns_one_entry_per_source(db):
    a = _kb(db, name="A", nl2sql_state=rd.STATE_READY)
    b = _kb(db, name="B", catalog_status="pending")
    verdicts = rd.list_verdicts(db, [a.id, b.id])
    assert set(verdicts) == {a.id, b.id}
    assert verdicts[a.id].allowed is True
    assert verdicts[b.id].allowed is False


def test_note_is_empty_for_ready_sources():
    assert rd.note(rd.evaluate(_kb_obj(nl2sql_state=rd.STATE_READY))) == ""
    assert rd.note(rd.evaluate(_kb_obj(catalog_status="pending"))) != ""


# ── disclosures must be usable in front of a customer ──────────────────────


def test_disclosures_contain_no_plumbing():
    """These sentences reach end users: business language only."""
    subjects = [
        _kb_obj( catalog_status="pending"),
        _kb_obj( catalog_status="error"),
        _kb_obj( catalog_status="indexing"),
        _kb_obj( nl2sql_state=rd.STATE_ENRICHMENT_PARTIAL),
        _kb_obj( nl2sql_state=rd.STATE_VALIDATION_FAILED),
        _kb_obj( nl2sql_state=rd.STATE_INDEXED),
        _kb_obj( db_type="db2", nl2sql_state=rd.STATE_READY),
        None,
    ]
    forbidden = (
        "catalog_status",
        "nl2sql",
        "kb_id",
        "source_id",
        "error_kind",
        "/api/",
        "reindex_catalog",
        "dialect",
        "_state",
    )
    for subject in subjects:
        disclosure = rd.evaluate(subject).disclosure
        assert disclosure, "every non-ready verdict needs a sentence"
        low = disclosure.lower()
        for token in forbidden:
            assert token not in low, f"{token!r} leaked into a user-facing sentence"
        assert disclosure[0].isupper(), "disclosures must be sentences"
        assert len(disclosure) > 40


# ── index-completion classification (ENRICHED → VALIDATED → READY) ─────────


def _tables(n, described=True):
    return [
        {
            "table_name": f"t{i}",
            "description_zh": "业务说明" if described else "",
            "description_en": "business description" if described else "",
            "columns": [{"name": "f1", "description_en": "col" if described else ""}],
        }
        for i in range(n)
    ]


def test_completion_is_ready_only_when_the_work_is_really_done():
    from app.services.knowledge_graph.catalog_indexer import _classify_completion

    state, reason = _classify_completion(_tables(10))
    assert state == rd.STATE_READY
    assert "10 tables" in reason


def test_unreadable_tables_downgrade_to_partial():
    from app.services.knowledge_graph.catalog_indexer import _classify_completion

    state, reason = _classify_completion(_tables(10), unreadable=["t_missing"])
    assert state == rd.STATE_ENRICHMENT_PARTIAL
    assert "could not be read" in reason


def test_undescribed_tables_downgrade_to_partial():
    from app.services.knowledge_graph.catalog_indexer import _classify_completion

    state, reason = _classify_completion(_tables(10, described=False))
    assert state == rd.STATE_ENRICHMENT_PARTIAL
    assert "descriptions" in reason


def test_empty_discovery_is_validation_failed():
    from app.services.knowledge_graph.catalog_indexer import _classify_completion

    state, _reason = _classify_completion([])
    assert state == rd.STATE_VALIDATION_FAILED


def test_unidentified_dialect_is_validation_failed():
    from app.services.knowledge_graph.catalog_indexer import _classify_completion

    state, reason = _classify_completion(
        _tables(3), caps_error="No dialect capability profile for db_type='db2'"
    )
    assert state == rd.STATE_VALIDATION_FAILED
    assert "db2" in reason


def test_enrichment_gap_string_is_informative():
    from app.services.knowledge_graph.catalog_indexer import _enrichment_gap

    gap = _enrichment_gap(_tables(4), embedded=False)
    assert "4/4 tables described" in gap
    assert "embeddings pending" in gap


# ── the indexer writes the lifecycle as the work happens ───────────────────


def test_indexer_advances_states_in_order(monkeypatch):
    """The staged states must be written at the moment the work happens.

    Guarded structurally (source inspection) because driving the full async
    index job needs a live source; the live walk is covered by the probe
    scripts. What must never regress is that the indexer writes the states at
    all — a KB that never advances past ``pending`` is what let unprepared
    sources answer.
    """
    import inspect

    from app.services.knowledge_graph import catalog_indexer as ci

    src = inspect.getsource(ci.index_kb_catalog)
    order = [
        "STATE_DISCOVERING",
        "STATE_INDEXED",
        "STATE_ENRICHED",
    ]
    positions = [src.find(s) for s in order]
    assert all(p != -1 for p in positions), "indexer no longer writes the lifecycle"
    assert positions == sorted(positions), "lifecycle states written out of order"
    assert "STATE_DISCOVERY_FAILED" in inspect.getsource(ci)
    assert "_classify_completion" in src, "indexer does not classify its completion"


def test_trigger_marks_a_new_source_as_connected_not_ready():
    import inspect

    from app.services.knowledge_graph import catalog_triggers as ct

    src = inspect.getsource(ct._reindex_catalog_safe)
    assert "STATE_CONNECTED" in src


# ── P1 + P2: a newly connected database, no special routing ────────────────


class _FakeOracleConn:
    """Just enough of a connector to exercise the probe."""

    dialect = "oracle"

    def __init__(self, fail_window=False, fail_tz=False):
        self._fail_window = fail_window
        self._fail_tz = fail_tz

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        s = sql.upper()
        if "V$VERSION" in s:
            return [{"v": "Oracle Database 19c Enterprise Edition"}]
        if "CURRENT_SCHEMA" in s:
            return [{"s": "ERP"}]
        if "ALL_USERS" in s:
            return [{"s": "ERP"}, {"s": "SYS"}]
        if "SESSIONTIMEZONE" in s:
            if self._fail_tz:
                raise RuntimeError("ORA-00904: invalid identifier")
            return [{"tz": "+08:00"}]
        if "LANGUAGE" in s:
            return [{"lc": "AMERICAN_AMERICA.AL32UTF8"}]
        if "USER_VIEWS" in s:
            return [{"n": 12}]
        if "ROW_NUMBER" in s:
            if self._fail_window:
                raise RuntimeError("ORA-30483: window functions are not allowed here")
            return [{"rn": 1}]
        return []


def test_newly_connected_source_becomes_queryable_without_code_changes(db):
    """A dialect the platform has never seen live: discovery + capabilities only.

    No per-source branch, no customer alias, no fixed route — the source is
    profiled from its own answers and then allowed through the gate.
    """
    from app.services.db import capability_probe

    kb = _kb(db, name="Brand New Oracle", db_type="oracle", catalog_status="pending")
    assert rd.evaluate(kb).allowed is False  # not prepared yet

    payload = capability_probe.probe_source(
        kb, db, tables=_tables(2), conn=_FakeOracleConn()
    )
    assert payload["dialect"] == "oracle"
    assert payload["pagination"] == "fetch"
    assert payload["quote_style"] == "double"
    assert payload["supports_views"] is True
    assert payload["supports_window_functions"] is True
    assert payload["timezone"] == "+08:00"
    assert payload["schemas"] == ["ERP", "SYS"]
    assert "Oracle Database 19c" in payload["server_version"]
    assert payload["catalog_version"]

    row = capability_probe.store_profile(db, kb, payload)
    assert row is not None, "profile must persist"
    assert capability_probe.cap_dialect(db, kb.id) == "oracle"

    # Walking the lifecycle is all that is needed to allow it.
    rd.set_state(db, kb, rd.STATE_READY)
    verdict, refusal = rd.check_source(db, kb.id)
    assert verdict.allowed is True and refusal is None


def test_probe_failure_is_recorded_as_evidence_not_as_a_default(db):
    """A capability the source refused is False/unknown, never assumed."""
    from app.services.db import capability_probe

    kb = _kb(db, db_type="oracle", catalog_status="pending")
    payload = capability_probe.probe_source(
        kb, db, tables=_tables(2), conn=_FakeOracleConn(fail_window=True, fail_tz=True)
    )
    assert payload["supports_window_functions"] is False
    assert payload["timezone"] is None
    assert payload["probe_evidence"]["window_functions"]["ok"] is False
    assert "ORA-30483" in payload["probe_evidence"]["window_functions"]["error"]
    assert payload["probe_evidence"]["timezone"]["ok"] is False
    # The dialect's own syntax map is still recorded — it is a dialect fact,
    # not a connection fact.
    assert payload["dialect"] == "oracle"


def test_probe_without_a_known_dialect_records_the_refusal():
    from app.services.db import capability_probe

    kb = _kb_obj( db_type="db2", catalog_status="pending")
    payload = capability_probe.probe_source(kb, None)
    assert payload["dialect"] == ""
    assert "No dialect capability profile" in payload["detection_error"]
    assert payload["probe_evidence"]["dialect"]["ok"] is False


def test_profile_for_kb_prefers_the_persisted_profile(db):
    """The source's own recorded dialect beats the raw db_type string."""
    from app.services.db import capability_probe
    from app.models.kb_capability import KBCapabilityProfile

    kb = _kb(db, db_type="postgresql", nl2sql_state=rd.STATE_READY)
    db.add(
        KBCapabilityProfile(
            id=str(uuid.uuid4()), kb_id=kb.id, dialect="oracle", pagination="fetch"
        )
    )
    db.commit()
    assert capability_probe.cap_dialect(db, kb.id) == "oracle"
    assert capability_probe.profile_for_kb(db, kb).name == "oracle"


def test_profile_for_kb_falls_back_to_db_type_only(db):
    from app.services.db import capability_probe

    kb = _kb(db, db_type="sqlite", nl2sql_state=rd.STATE_READY)
    assert capability_probe.profile_for_kb(db, kb).name == "sqlite"


def test_profile_for_kb_returns_none_when_unknown(db):
    """No capability anywhere ⇒ None, never a default dialect object."""
    from app.services.db import capability_probe

    kb = _kb(db, db_type="db2", nl2sql_state=rd.STATE_READY)
    assert capability_probe.profile_for_kb(db, kb) is None


@pytest.mark.parametrize(
    "dialect,outcome,expected",
    [
        ("mysql", [{"Grants for app@%": "GRANT SELECT ON *.* TO 'app'@'%'"}], True),
        ("mysql", [{"Grants for app@%": "GRANT ALL PRIVILEGES ON *.* TO 'app'@'%'"}], False),
        ("postgres", [{"can_write": False}], True),
        ("postgres", [{"can_write": True}], False),
        ("sqlite", [{"query_only": 1}], True),
        ("sqlite", [{"query_only": 0}], False),
        ("oracle", [{"x": 1}], None),  # no privilege probe ⇒ honest unknown
    ],
)
def test_read_only_interpretation(dialect, outcome, expected):
    from app.services.db.capability_probe import _parse_read_only
    from app.services.db.dialect import profile_for

    assert _parse_read_only(profile_for(dialect), outcome) is expected


# ── prompt surface ─────────────────────────────────────────────────────────


def test_prompt_marks_unprepared_sources_and_tells_the_agent_to_say_so():
    from app.services.data_source_runtime.data_source_runtime import (
        _build_data_source_prompt_section,
    )

    bound = [
        {
            "id": "kb-ready",
            "name": "Ready Warehouse",
            "db_type": "postgresql",
            "database_name": "ready",
            "source_kind": "database",
            "file_type": "",
            "indexing_status": None,
            "chunk_count": 0,
            "queryable": True,
            "readiness_note": "",
            "readiness_disclosure": "",
        },
        {
            "id": "kb-pending",
            "name": "Half Prepared Warehouse",
            "db_type": "postgresql",
            "database_name": "half",
            "source_kind": "database",
            "file_type": "",
            "indexing_status": None,
            "chunk_count": 0,
            "queryable": False,
            "readiness_note": "not queryable yet — do not attempt to answer from it",
            "readiness_disclosure": "This data source has not been prepared for questions yet.",
        },
    ]
    text = _build_data_source_prompt_section(bound)
    assert "Ready Warehouse" in text
    assert "NOT QUERYABLE YET" in text
    assert "has not been prepared" in text
    # …and the agent is told what to DO instead of improvising
    assert "Tell the user that plainly" in text
    # a ready source must not be annotated (prompt noise)
    ready_line = [ln for ln in text.splitlines() if "Ready Warehouse" in ln][0]
    assert "NOT QUERYABLE" not in ready_line


def test_load_bound_kb_meta_carries_readiness(db):
    from app.services.data_source_runtime.data_source_runtime import _load_bound_kb_meta

    ready = _kb(db, name="R", nl2sql_state=rd.STATE_READY)
    pending = _kb(db, name="P", catalog_status="pending")
    meta = {m["id"]: m for m in _load_bound_kb_meta(db, [ready.id, pending.id])}
    assert meta[ready.id]["queryable"] is True
    assert meta[ready.id]["readiness_note"] == ""
    assert meta[pending.id]["queryable"] is False
    assert meta[pending.id]["readiness_note"]
    assert meta[pending.id]["readiness_disclosure"]
