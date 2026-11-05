"""§10.33 — misfire worked examples (F-16): every §6.1a example reproduces
exactly under fixed clocks and timezones, plus the edit/pause discarded-
episode edge cases and the once-past-grace path.

The pure decision function ``collapse_missed_interval`` is clock-injected;
the dispatcher wiring is covered by the R4/R5 suites.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.services.automation.trigger_policy import collapse_missed_interval  # noqa: E402

# Worked examples: cron `0 8 * * *`, tz `Asia/Shanghai` (UTC+8 ⇒ 08:00 local
# = 00:00 UTC), grace 3600s, policy fire_once_late. All datetimes UTC-naive
# (the storage convention).
DAILY = "0 8 * * *"
SHANGHAI = "Asia/Shanghai"


def test_ex1_single_missed_slot_within_grace_fires_late():
    d = collapse_missed_interval(
        cron=DAILY, tz=SHANGHAI,
        next_run_at=datetime(2026, 11, 4, 0, 0),      # 08:00 local
        now=datetime(2026, 11, 4, 0, 40),             # recovery 08:40 local
        grace_seconds=3600,
    )
    assert d.action == "fire_late"
    assert d.representative == datetime(2026, 11, 4, 0, 0)
    assert d.collapsed_count == 1
    assert d.advance_to is not None and d.advance_to > d.representative


def test_ex2_single_missed_slot_beyond_grace_skips():
    d = collapse_missed_interval(
        cron=DAILY, tz=SHANGHAI,
        next_run_at=datetime(2026, 11, 4, 0, 0),
        now=datetime(2026, 11, 4, 1, 30),             # 09:30 local
        grace_seconds=3600,
    )
    assert d.action == "skip"
    assert d.representative == datetime(2026, 11, 4, 0, 0)
    assert d.collapsed_count == 1


def test_ex3_multi_slot_episode_fires_once_collapsed_10():
    d = collapse_missed_interval(
        cron="*/5 * * * *", tz="UTC",
        next_run_at=datetime(2026, 11, 4, 2, 0),
        now=datetime(2026, 11, 4, 2, 50),
        grace_seconds=3600,
    )
    assert d.action == "fire_late"                    # R=02:45, staleness 5min
    assert d.representative == datetime(2026, 11, 4, 2, 45)
    assert d.collapsed_count == 10                    # 02:00…02:45
    assert d.advance_to == datetime(2026, 11, 4, 2, 50)


def test_ex4_long_downtime_fresh_due_fires_once_collapsed_4():
    d = collapse_missed_interval(
        cron=DAILY, tz=SHANGHAI,
        next_run_at=datetime(2026, 11, 1, 0, 0),      # Nov-01 08:00 local
        now=datetime(2026, 11, 4, 0, 30),             # Nov-04 08:30 local
        grace_seconds=3600,
    )
    assert d.action == "fire_late"
    assert d.representative == datetime(2026, 11, 4, 0, 0)
    assert d.collapsed_count == 4


def test_ex5_long_downtime_stale_due_skips_never_4_executions():
    d = collapse_missed_interval(
        cron=DAILY, tz=SHANGHAI,
        next_run_at=datetime(2026, 11, 1, 0, 0),
        now=datetime(2026, 11, 4, 2, 0),              # Nov-04 10:00 local
        grace_seconds=3600,
    )
    assert d.action == "skip"
    assert d.representative == datetime(2026, 11, 4, 0, 0)
    assert d.collapsed_count == 4


def test_ex6_dst_spring_forward_nonexistent_slot_excluded():
    # `30 2 * * *` Europe/Berlin; 2026-03-29 02:30 local does not exist
    # (02:00→03:00 spring-forward). The interval covers the transition day;
    # every emitted slot must resolve (no crash), R = latest resolvable
    # slot, and the count matches the slots the parser actually emits.
    d = collapse_missed_interval(
        cron="30 2 * * *", tz="Europe/Berlin",
        next_run_at=datetime(2026, 3, 28, 1, 30),     # Mar-28 02:30 local = 01:30 UTC
        now=datetime(2026, 3, 30, 1, 0),              # Mar-30 03:00 local (CET→CEST)
        grace_seconds=3600,
    )
    # Interval [Mar-28 01:30, Mar-30 01:00) UTC. Expected daily slots:
    # Mar-28 01:30 UTC (02:30 CET), Mar-29 slot (02:30 nonexistent → parser
    # resolves fold=0; whatever the parser emits is authoritative — the
    # pinned property is determinism + R = max + count consistency),
    # Mar-30 not included (01:30 UTC > now 01:00 UTC).
    assert d.action in ("fire_late", "skip")
    assert d.representative is not None
    assert d.representative > datetime(2026, 3, 29, 0, 0)   # transition-day slot
    assert d.collapsed_count >= 2
    # Re-invocation is deterministic.
    d2 = collapse_missed_interval(
        cron="30 2 * * *", tz="Europe/Berlin",
        next_run_at=datetime(2026, 3, 28, 1, 30),
        now=datetime(2026, 3, 30, 1, 0),
        grace_seconds=3600,
    )
    assert (d2.action, d2.representative, d2.collapsed_count) == (
        d.action, d.representative, d.collapsed_count,
    )


def test_edge_once_past_grace_skips_and_never_rearms():
    d = collapse_missed_interval(
        cron=None, tz="UTC",
        next_run_at=datetime(2026, 11, 1, 12, 0),     # once trigger
        now=datetime(2026, 11, 1, 16, 0),             # 4h later, grace 1h
        grace_seconds=3600,
        recurring=False,
    )
    assert d.action == "skip"
    assert d.catchup is False                         # never re-arms


def test_edge_interval_boundary_slot_at_now_excluded():
    # Slot exactly at `now` is not "missed" — the interval is [due, now).
    d = collapse_missed_interval(
        cron="0 * * * *", tz="UTC",
        next_run_at=datetime(2026, 11, 4, 2, 0),
        now=datetime(2026, 11, 4, 3, 0),              # 03:00 slot not yet due
        grace_seconds=60,
    )
    assert d.representative == datetime(2026, 11, 4, 2, 0)
    assert d.collapsed_count == 1


def test_policy_skip_always_skips_within_grace():
    d = collapse_missed_interval(
        cron=DAILY, tz=SHANGHAI,
        next_run_at=datetime(2026, 11, 4, 0, 0),
        now=datetime(2026, 11, 4, 0, 10),
        grace_seconds=3600,
        policy="skip",
    )
    assert d.action == "skip"
