"""In-flight request deduplication for data queries (approved design A4).

PROBLEM (observed live, 2026-10-27)
-----------------------------------
One turn issued ``query_database`` THREE times concurrently ("(1/3), (2/3),
(3/3)"), each a variant of the same question. Wall-clock cost scales with
every duplicate because each one drives a full NL2SQL pipeline (schema
selection → source verification → LLM SQL generation → execution →
self-check) — 15-30 s and several LLM calls apiece.

DESIGN CONSTRAINTS (from the approved review)
---------------------------------------------
* Dedup must happen BEFORE the expensive work. A SQL fingerprint does not
  exist until after generation, so two concurrent calls would both generate
  SQL before noticing they duplicate each other. Hence a REQUEST
  fingerprint is computed up front, and the SQL fingerprint is used only to
  VERIFY equivalence afterwards.
* Share a result only when the *execution context* is equivalent: two
  requests for the same question in different orgs, apps or conversations
  must never share rows. The fingerprint therefore includes org, app and
  the bound datasource ids.
* In-flight and same-turn only. Equivalent SQL is NOT an indefinite cache —
  future turns must re-read the database (a cached figure is a stale figure).
* Never share a DB session across concurrent tasks: followers await the
  leader's completed result object, they do not touch its session.

This module is deliberately dependency-free (no locks held across awaits
beyond the shared future) so it is trivially testable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def _canonical(value: Any) -> str:
    """Stable JSON for hashing (order-independent, never raises)."""
    try:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — fall back to repr
        return repr(value)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def request_fingerprint(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    org_id: str | None = None,
    app_id: str | None = None,
    conversation_id: str | None = None,
    bound_kb_ids: list[str] | None = None,
) -> str:
    """Fingerprint computed BEFORE any expensive work (A4).

    Includes the authorization/execution context so two requests can only
    ever be considered equivalent inside the same tenant, app, conversation
    and datasource scope.
    """
    payload = {
        "tool": tool_name,
        "args": arguments or {},
        "org": org_id,
        "app": app_id,
        "conv": conversation_id,
        "kbs": sorted(bound_kb_ids or []),
    }
    return _sha(_canonical(payload))


def sql_fingerprint(sql: str | None) -> str | None:
    """Fingerprint of the generated SQL, for VERIFYING equivalence post-hoc.

    Whitespace-collapsed and case-folded: the same statement formatted two
    ways is the same statement.
    """
    if not sql:
        return None
    normalized = " ".join(str(sql).split()).lower()
    return _sha(normalized) if normalized else None


@dataclass
class _Entry:
    future: asyncio.Future
    sql_fp: str | None = None
    #: Count of callers that shared this in-flight result (for telemetry).
    followers: int = 0


@dataclass
class DedupResult:
    """What a caller learns about its participation."""

    value: Any
    shared: bool
    role: str  # "leader" | "follower"
    fingerprint: str
    followers: int = 0


class InFlightDeduplicator:
    """Per-turn concurrent-request coalescer.

    Keys are request fingerprints. The first caller for a key becomes the
    leader and runs the work; concurrent callers for the same key await the
    leader's future and receive the identical result object. The entry is
    removed when the leader finishes, so this NEVER becomes a cross-turn
    cache (A4 / test 10: "no unconditional cross-turn cache reuse").
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    # ── introspection (tests + telemetry) ───────────────────────────────
    def in_flight(self) -> int:
        return len(self._entries)

    def followers_for(self, fp: str) -> int:
        entry = self._entries.get(fp)
        return entry.followers if entry else 0

    @staticmethod
    def _loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    async def run(
        self,
        fingerprint: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        enabled: bool = True,
    ) -> DedupResult:
        """Run ``factory`` once per in-flight ``fingerprint``.

        ``enabled=False`` runs the factory directly (escape hatch for
        callers/flags that must not coalesce).
        """
        if not enabled:
            value = await factory()
            return DedupResult(
                value=value, shared=False, role="leader", fingerprint=fingerprint
            )

        existing = self._entries.get(fingerprint)
        if existing is not None:
            existing.followers += 1
            logger.info(
                "dedup: coalescing duplicate request %s (%d follower(s))",
                fingerprint[:12],
                existing.followers,
            )
            # shield: a follower cancelling must not cancel the leader's work
            # for everyone else. Followers copy the outcome, they never touch
            # the leader's DB session.
            value = await asyncio.shield(existing.future)
            return DedupResult(
                value=value,
                shared=True,
                role="follower",
                fingerprint=fingerprint,
                followers=existing.followers,
            )

        entry = _Entry(future=self._loop().create_future())
        self._entries[fingerprint] = entry
        try:
            value = await factory()
            if not entry.future.done():
                entry.future.set_result(value)
            return DedupResult(
                value=value,
                shared=False,
                role="leader",
                fingerprint=fingerprint,
                followers=entry.followers,
            )
        except BaseException as exc:
            if not entry.future.done():
                entry.future.set_exception(exc)
                # Prevent "exception was never retrieved" warnings when no
                # follower happens to await it.
                entry.future.exception()
            raise
        finally:
            # Same-turn only: the key disappears the moment the leader is
            # done, so an identical request in a LATER turn re-reads the DB.
            self._entries.pop(fingerprint, None)

    def reset(self) -> None:
        self._entries.clear()


# ── Per-turn scope (contextvar, mirrors reliability's LoopState pattern) ──
import contextvars  # noqa: E402  (kept next to its consumer for clarity)

_CURRENT_DEDUP: contextvars.ContextVar["InFlightDeduplicator | None"] = (
    contextvars.ContextVar("query_inflight_dedup", default=None)
)


def get_inflight_dedup() -> InFlightDeduplicator | None:
    return _CURRENT_DEDUP.get()


def set_inflight_dedup(dedup: InFlightDeduplicator | None):
    return _CURRENT_DEDUP.set(dedup)


def reset_inflight_dedup(token) -> None:
    _CURRENT_DEDUP.reset(token)
