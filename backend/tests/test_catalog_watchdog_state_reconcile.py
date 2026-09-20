"""P1 — the watchdog must reset *every* column that decides queryability.

Measured defect this suite pins down: ``run_catalog_watchdog`` reset
``catalog_status`` only, while ``readiness.effective_state`` trusts a non-empty
``nl2sql_state`` and derives from ``catalog_status`` solely when it is empty.
A preparation run that died silently (container restart, OOM, ``kill -9``)
therefore left ``nl2sql_state="discovering"`` behind for ever, and the source
stayed unqueryable **after** the watchdog had "recovered" it — refused as *in
flight*, with the user told "still being prepared, please try again in a
moment". The row said ready, the agent said wait, and nothing ever moved.

What is pinned here:

* a dead run's staged state is reconciled to a state the source can actually
  honour (``enrichment_partial`` when tables were catalogued, otherwise
  ``discovery_failed``), whichever pass of the scan finds it;
* a run that may still be alive is never touched (the historical false-kill);
* a source that was never staged keeps the historical behaviour exactly, and no
  lifecycle is invented for it;
* the terminal writer (``_classify_completion``) still never returns a
  mid-lifecycle state — the invariant the reconciliation rule rests on.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401  register all models
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta

from app.services.db import readiness as rd
from app.services.knowledge_graph import catalog_watchdog as wd


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def sessions():
    """Temp-sqlite session factory, wired in as the watchdog's SessionLocal."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def scan(sessions, monkeypatch):
    """Run one watchdog scan against the temp database."""
    monkeypatch.setattr(wd, "SessionLocal", sessions)

    def _run():
        wd._scan_and_reset_stuck()

    return _run


def _kb(sessions, **kw):
    """A database source, with only the fields the watchdog and gate read."""
    session = sessions()
    try:
        kb = KnowledgeBase(
            id=kw.pop("id", str(uuid.uuid4())),
            name=kw.pop("name", "Warehouse"),
            source_kind=kw.pop("source_kind", "database"),
            db_type=kw.pop("db_type", "postgresql"),
            status="active",
            catalog_status=kw.pop("catalog_status", "ready"),
            nl2sql_state=kw.pop("nl2sql_state", ""),
            nl2sql_state_reason=kw.pop("nl2sql_state_reason", None),
            nl2sql_state_at=kw.pop("nl2sql_state_at", None),
            item_count=kw.pop("item_count", 0),
        )
        for key, value in kw.items():
            setattr(kb, key, value)
        session.add(kb)
        session.commit()
        kb_id = kb.id
    finally:
        session.close()
    return kb_id


def _tables(sessions, kb_id: str, n: int) -> None:
    session = sessions()
    try:
        for i in range(n):
            session.add(
                KBTableMeta(
                    kb_id=kb_id,
                    schema_name="public",
                    table_name=f"t_{i}",
                    table_type="TABLE",
                )
            )
        session.commit()
    finally:
        session.close()


def _read(sessions, kb_id: str) -> KnowledgeBase:
    session = sessions()
    try:
        return (
            session.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
        )
    finally:
        session.close()


def _ago(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


# ── 1. the documented defect: a dead run's staged state outlives the run ────


def test_dead_run_discovering_does_not_leave_the_source_in_flight(sessions, scan):
    """The headline bug: 'still being prepared, try again in a moment', for ever.

    catalog_status was already reset by an earlier scan, so this is the
    second-pass fingerprint — the staged claim is the only thing left.
    """
    kb_id = _kb(
        sessions,
        catalog_status="ready",
        nl2sql_state=rd.STATE_DISCOVERING,
        nl2sql_state_at=_ago(hours=2),
        updated_date=_ago(hours=2),
    )
    _tables(sessions, kb_id, 3)

    before = _read(sessions, kb_id)
    pre = rd.evaluate(before)
    assert pre.allowed is False and pre.code == "discovering", (
        "precondition: a mid-lifecycle staged state is refused as in flight"
    )

    scan()

    after = _read(sessions, kb_id)
    assert after.nl2sql_state == rd.STATE_ENRICHMENT_PARTIAL
    assert after.catalog_status == "ready"
    assert after.item_count == 3
    assert after.nl2sql_state_at is not None, "the repair must be timestamped"
    assert "died" in (after.nl2sql_state_reason or "")

    post = rd.evaluate(after)
    assert post.code == "partial"
    assert post.allowed is True, "the source must be usable after recovery"
    assert post.limited is True, "and the limitation must travel with the answer"
    assert "still being prepared" not in post.disclosure


def test_dead_run_without_metadata_becomes_an_actionable_failure(sessions, scan):
    """No catalogued structure ⇒ refusal that names the retry, not a wait."""
    kb_id = _kb(
        sessions,
        catalog_status="indexing",
        nl2sql_state=rd.STATE_DISCOVERING,
        nl2sql_state_at=_ago(minutes=30),
        updated_date=_ago(minutes=30),
    )

    scan()

    after = _read(sessions, kb_id)
    assert after.catalog_status == "error"
    assert after.nl2sql_state == rd.STATE_DISCOVERY_FAILED
    verdict = rd.evaluate(after)
    assert verdict.code == "discovery_failed"
    assert verdict.allowed is False
    assert "could not be prepared" in verdict.disclosure


@pytest.mark.parametrize("stalled", ["indexed", "enriched", "validated"])
def test_every_mid_lifecycle_state_is_reconciled(sessions, scan, stalled):
    """indexed / enriched / validated are the same dead-run fingerprint."""
    kb_id = _kb(
        sessions,
        catalog_status="ready",
        nl2sql_state=stalled,
        nl2sql_state_at=_ago(hours=1),
        updated_date=_ago(hours=1),
    )
    _tables(sessions, kb_id, 2)

    scan()

    after = _read(sessions, kb_id)
    assert after.nl2sql_state == rd.STATE_ENRICHMENT_PARTIAL
    assert rd.evaluate(after).code == "partial"


def test_dead_run_with_no_metadata_and_paused_status_is_reconciled(sessions, scan):
    """Pass 2 with nothing catalogued: usable is impossible, so say so."""
    kb_id = _kb(
        sessions,
        catalog_status="ready",  # an earlier scan reset it, no metadata exists
        nl2sql_state=rd.STATE_ENRICHED,
        nl2sql_state_at=_ago(hours=3),
        updated_date=_ago(hours=3),
    )

    scan()

    after = _read(sessions, kb_id)
    assert after.catalog_status == "error"
    assert after.nl2sql_state == rd.STATE_DISCOVERY_FAILED


# ── 2. live work and terminal sources are never touched ────────────────────


def test_live_run_is_never_killed(sessions, scan):
    """A run owns its row: indexing + fresh heartbeat must survive untouched."""
    kb_id = _kb(
        sessions,
        catalog_status="indexing",
        nl2sql_state=rd.STATE_DISCOVERING,
        nl2sql_state_at=datetime.now(timezone.utc),
        updated_date=datetime.now(timezone.utc),
    )

    scan()

    after = _read(sessions, kb_id)
    assert after.catalog_status == "indexing"
    assert after.nl2sql_state == rd.STATE_DISCOVERING
    assert after.nl2sql_state_reason is None


def test_staged_state_inside_the_staleness_window_is_not_touched(sessions, scan):
    """The indexer's own terminal writes straddle catalog_status; never race it."""
    kb_id = _kb(
        sessions,
        catalog_status="ready",
        nl2sql_state=rd.STATE_ENRICHED,
        nl2sql_state_at=datetime.now(timezone.utc),
        updated_date=datetime.now(timezone.utc),
    )

    scan()

    after = _read(sessions, kb_id)
    assert after.nl2sql_state == rd.STATE_ENRICHED


def test_stuck_indexing_without_a_staged_state_keeps_historical_behaviour(
    sessions, scan
):
    """A source never staged on this build: reset the status, invent nothing."""
    kb_id = _kb(
        sessions,
        catalog_status="indexing",
        nl2sql_state="",
        updated_date=_ago(hours=1),
    )
    _tables(sessions, kb_id, 5)

    scan()

    after = _read(sessions, kb_id)
    assert after.catalog_status == "ready"
    assert after.item_count == 5
    assert after.nl2sql_state == "", "no lifecycle may be invented for it"
    assert rd.evaluate(after).code == "ready", "derived state still applies"


def test_terminal_sources_are_not_candidates(sessions, scan):
    """A finished source (ready/partial/failed) is not a stuck source."""
    ids = {
        state: _kb(
            sessions,
            catalog_status="ready",
            nl2sql_state=state,
            nl2sql_state_at=_ago(hours=5),
            updated_date=_ago(hours=5),
        )
        for state in (
            rd.STATE_READY,
            rd.STATE_ENRICHMENT_PARTIAL,
            rd.STATE_VALIDATION_FAILED,
        )
    }
    for kb_id in ids.values():
        _tables(sessions, kb_id, 1)

    scan()

    for state, kb_id in ids.items():
        after = _read(sessions, kb_id)
        assert after.nl2sql_state == state
        assert after.nl2sql_state_reason is None


def test_document_sources_are_out_of_scope(sessions, scan):
    """Documents have their own lifecycle; the catalog watchdog must not touch it."""
    kb_id = _kb(
        sessions,
        source_kind="file",
        db_type=None,
        catalog_status="indexing",
        nl2sql_state=rd.STATE_DISCOVERING,
        updated_date=_ago(hours=2),
    )

    scan()

    after = _read(sessions, kb_id)
    assert after.catalog_status == "indexing"
    assert after.nl2sql_state == rd.STATE_DISCOVERING


# ── 3. the invariant the reconciliation rule rests on ──────────────────────


def test_terminal_writer_never_returns_a_mid_lifecycle_state():
    """If a terminal run could end mid-lifecycle, the rule would be unsound."""
    from app.services.knowledge_graph.catalog_indexer import _classify_completion

    described = {"description_zh": "订单", "columns": [{"description_zh": "编号"}]}
    worlds = [
        ([], {}),
        ([{"columns": []}], {}),
        ([{"columns": [{"description_zh": "x"}]}], {}),
        ([described], {}),
        ([described], {"unreadable": ["t"]}),
        ([described], {"caps_error": "no dialect capability profile for 'nope'"}),
    ]
    seen = set()
    for tables, kwargs in worlds:
        state, reason = _classify_completion(tables, **kwargs)
        assert state not in wd._MID_LIFECYCLE, (
            f"_classify_completion returned mid-lifecycle state {state!r} — "
            "the watchdog's dead-run rule would then be unsound"
        )
        assert state in rd.ALL_STATES
        assert reason
        seen.add(state)
    assert rd.STATE_ENRICHMENT_PARTIAL in seen
    assert rd.STATE_VALIDATION_FAILED in seen
    assert rd.STATE_READY in seen
