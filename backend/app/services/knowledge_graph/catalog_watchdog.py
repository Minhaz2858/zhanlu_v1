"""Catalog indexer watchdog — recovers sources whose preparation run died.

If the catalog indexer background task dies silently (container restart, OOM,
exception in a daemon thread, ``kill -9``), the KB's ``catalog_status`` stays at
``"indexing"`` forever and the UI shows "索引中" indefinitely.

This watchdog scans for KBs stuck at ``"indexing"`` for longer than
``STUCK_AFTER_SECONDS`` (default 10 minutes) and resets them:

* If the KB already has table metadata (``kb_table_meta`` rows) → reset to
  ``"ready"`` (the previous run completed enough to be usable).
* Otherwise → reset to ``"error"`` so the user can retry.

TWO COLUMNS DECIDE QUERYABILITY, SO TWO COLUMNS ARE RESET
--------------------------------------------------------

Resetting only ``catalog_status`` left the source unqueryable all the same. The
readiness gate (``app/services/db/readiness.py``) trusts a non-empty
``nl2sql_state`` and only *derives* a state from ``catalog_status`` when that
column is empty (``readiness.effective_state``), so the staged state written by
the dead run outlived the run that owned it. Measured consequences for a source
this watchdog had already "recovered":

* ``nl2sql_state="discovering"`` → refused as *in flight*: the user is told
  "still being prepared, please try again in a moment" — for ever, while the
  source's badge says ready. No error, no retry, no path forward.
* ``nl2sql_state="indexed"|"enriched"|"validated"`` → refused as *stalled*
  ("preparation needs to be re-run"), which is honest, but nothing re-runs it
  and the UI still reports ready.

A run owns its staged state only while it is alive, and a live run is exactly
the one whose ``catalog_status`` is ``"indexing"``. So a mid-lifecycle staged
state on a source that is *not* indexing is, by construction, the fingerprint of
a run that died:

    MID_LIFECYCLE state  and  catalog_status != "indexing"  →  dead run

``catalog_indexer._classify_completion`` (the only terminal writer) never returns
a mid-lifecycle state, and the indexer overwrites ``nl2sql_state`` before it
returns, so no healthy source can hold that pair. Both passes of the scan
therefore go through one rule — ``_reconcile_dead_run`` — and both columns are
written in the same transaction.

A source with an empty ``nl2sql_state`` (never staged on this build) keeps the
historical behaviour exactly: ``catalog_status`` alone is reset, and nothing is
invented about a lifecycle it was never part of.

Runs as an asyncio task started from ``main.py`` lifespan.  Polls every
``POLL_INTERVAL_SECONDS`` (default 60s).  Self-disables when
``SEMANTIC_CATALOG_ENABLED`` is False.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_

from app.config import settings
from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta
from app.services.db import readiness as src_readiness

logger = logging.getLogger(__name__)
# Ensure INFO logs are visible even when the root logger is at WARNING.
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

POLL_INTERVAL_SECONDS = 60
STUCK_AFTER_SECONDS = 600  # 10 minutes

# States only a live preparation run may hold (one vocabulary, see
# db/readiness.py). Held by a source that is not indexing = dead run.
_MID_LIFECYCLE = frozenset(src_readiness.MID_LIFECYCLE)


async def run_catalog_watchdog(stop_event: asyncio.Event) -> None:
    """Run the watchdog loop until ``stop_event`` is set.

    Should be launched as ``asyncio.create_task(run_catalog_watchdog(...))``
    from the FastAPI lifespan.
    """
    logger.info("catalog_watchdog: starting (poll=%ds, stuck_after=%ds)",
                POLL_INTERVAL_SECONDS, STUCK_AFTER_SECONDS)
    while not stop_event.is_set():
        try:
            if settings.SEMANTIC_CATALOG_ENABLED:
                await asyncio.to_thread(_scan_and_reset_stuck)
        except Exception:
            logger.exception("catalog_watchdog: scan iteration failed")
        # Wait, but wake up immediately on stop
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            break  # stop requested
        except asyncio.TimeoutError:
            continue
    logger.info("catalog_watchdog: stopped")


def _age_seconds(stamp: datetime | None, now: datetime) -> float | None:
    """Seconds since *stamp*, treating a naive stamp as UTC. None if unset."""
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp).total_seconds()


def _naive_utc() -> datetime:
    """Naive UTC, matching how ``readiness.set_state`` stamps this column."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scan_and_reset_stuck() -> None:
    """One-shot: find sources whose preparation run died, and reconcile them.

    Two candidate classes, one reconciliation rule:

    a) ``catalog_status == "indexing"`` beyond ``STUCK_AFTER_SECONDS`` — the
       original stall: no run is alive to finish the job.
    b) a ``MID_LIFECYCLE`` staged state on a source that is not indexing at
       all — the run died so long ago that even its ``catalog_status`` was
       reset, but the staged claim it left behind still decides queryability.
       The staleness guard is what makes this safe: a live run holds
       ``catalog_status == "indexing"`` for its whole duration, the only window
       where it does not is the indexer's own terminal writes (two commits,
       milliseconds apart), and no source legitimately sits mid-lifecycle for
       ten minutes.
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        candidates = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.source_kind == "database",
                or_(
                    KnowledgeBase.catalog_status == "indexing",
                    KnowledgeBase.nl2sql_state.in_(sorted(_MID_LIFECYCLE)),
                ),
            )
            .all()
        )
        if not candidates:
            return

        for kb in candidates:
            indexing = (kb.catalog_status or "").strip().lower() == "indexing"
            staged_state = (getattr(kb, "nl2sql_state", "") or "").strip().lower()
            if indexing:
                # (a) the run's own heartbeat is the row's last write.
                age = _age_seconds(kb.updated_date, now)
                trigger = "stuck indexing"
            elif staged_state in _MID_LIFECYCLE:
                # (b) the staged claim's age is what must be stale.
                age = _age_seconds(kb.nl2sql_state_at or kb.updated_date, now)
                trigger = "dead-run staged state"
            else:
                continue  # not a candidate after all

            if age is None or age < STUCK_AFTER_SECONDS:
                continue  # a run may still own this row — never kill it early

            n_tables = (
                db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).count()
            )
            _reconcile_dead_run(
                db, kb, n_tables=n_tables, age=age, trigger=trigger,
            )
        db.commit()
    finally:
        db.close()


def _reconcile_dead_run(
    db,
    kb: KnowledgeBase,
    *,
    n_tables: int,
    age: float,
    trigger: str,
) -> None:
    """Write the honest state for a preparation run that will never finish.

    Caller commits. ``catalog_status`` keeps the policy this watchdog has always
    had (metadata present ⇒ usable, otherwise retryable). The staged state is
    reconciled only when the row actually holds one — a dead run's claim is
    replaced by a state that describes what the source has *now*:

    * tables catalogued → ``enrichment_partial``: the source answers, and every
      answer states the limitation. This is the same verdict
      ``catalog_indexer._classify_completion`` reaches for a source whose
      descriptions are incomplete, so no new vocabulary is introduced.
    * nothing catalogued → ``discovery_failed``: a refusal that names the
      failure and the retry, instead of an in-flight wait that never ends.
    """
    staged_state = (getattr(kb, "nl2sql_state", "") or "").strip().lower()
    owns_staged_state = staged_state in _MID_LIFECYCLE
    reason = (
        f"preparation run died before it finished — reported {staged_state or 'unknown'} "
        f"for {age / 60:.0f} min ({trigger})"
    )

    if n_tables > 0:
        kb.catalog_status = "ready"
        kb.item_count = n_tables
        if owns_staged_state:
            kb.nl2sql_state = src_readiness.STATE_ENRICHMENT_PARTIAL
            kb.nl2sql_state_reason = reason[:2000]
            kb.nl2sql_state_at = _naive_utc()
            logger.warning(
                "catalog_watchdog: kb=%s (%s) reconciled %s → %s "
                "(%s, %.0fs, %d tables indexed)",
                kb.id, kb.name, staged_state,
                src_readiness.STATE_ENRICHMENT_PARTIAL, trigger, age, n_tables,
            )
        else:
            logger.warning(
                "catalog_watchdog: reset kb=%s (%s) from indexing → ready "
                "(stuck %.0fs, %d tables already indexed)",
                kb.id, kb.name, age, n_tables,
            )
    else:
        kb.catalog_status = "error"
        if owns_staged_state:
            kb.nl2sql_state = src_readiness.STATE_DISCOVERY_FAILED
            kb.nl2sql_state_reason = reason[:2000]
            kb.nl2sql_state_at = _naive_utc()
            logger.warning(
                "catalog_watchdog: kb=%s (%s) reconciled %s → %s "
                "(%s, %.0fs, no metadata)",
                kb.id, kb.name, staged_state,
                src_readiness.STATE_DISCOVERY_FAILED, trigger, age,
            )
        else:
            logger.warning(
                "catalog_watchdog: reset kb=%s (%s) from indexing → error "
                "(stuck %.0fs, no metadata)",
                kb.id, kb.name, age,
            )
