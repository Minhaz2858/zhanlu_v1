"""Durable, idempotent turn claims — Stage 2B Commit 2.

WHY
---
Without a durable claim a retried stream request (a 401 retry re-sends the
identical body) would allocate a *second* turn ordinal, and a newer generation
could not be distinguished from a retry of an older one. The ordinal is the
authority that lets finalization reject a superseded generation regardless of
completion order.

THE CONTRACT
------------
``claim_turn`` is idempotent on ``(conversation_id, request_id,
assistant_message_id)`` and atomic:

  * same triple           -> the SAME stored ordinal is returned
  * same (conv, request) but a DIFFERENT assistant message
                          -> :class:`ClaimMessageMismatch` (nothing is mutated)
  * new request_id        -> a NEW, strictly larger ordinal

Regeneration is therefore a *new request_id* with the *same* assistant message:
it legitimately receives a newer ordinal, which is what supersedes the
in-flight generation.

IMPLEMENTATION NOTES
--------------------
* The allocation is a single guarded upsert (design Rev 4 §1.2). It is NOT
  "ON CONFLICT DO NOTHING + SELECT back": that form cannot distinguish an
  idempotent retry from a mismatched message.
* ``nextval()`` is evaluated even on the conflict path, so sequence values may
  have gaps. Gaps are harmless — every consumer compares stored ordinals, never
  contiguity.
* ``current_turn_seq`` advances with ``GREATEST()`` so it can never regress,
  even if an older-allocated claim commits after a newer one.
* Allocation and pointer advancement share ONE transaction: a rollback leaves
  neither a claim nor a pointer bump behind.
* This module does NOT finalize answers, enforce entity routes, or touch
  message content. It only answers "which ordinal owns this logical request?".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

__all__ = [
    "ClaimMessageMismatch",
    "ClaimError",
    "TurnClaim",
    "claim_turn",
    "lookup_claim",
    "current_ordinal",
    "CLAIM_LOCK_TIMEOUT_MS",
    "CLAIM_STATEMENT_TIMEOUT_MS",
]

#: Bound the claim transaction so a contended conversation row cannot hang a
#: stream. Mirrors the Rev 4 §3 timeout table.
CLAIM_LOCK_TIMEOUT_MS = 1000
CLAIM_STATEMENT_TIMEOUT_MS = 3000


class ClaimError(Exception):
    """Base class for claim failures (never raised for an idempotent retry)."""


class ClaimMessageMismatch(ClaimError):
    """The request_id exists but is bound to a different assistant message.

    Deterministic rejection: no ordinal is issued and the stored claim is left
    untouched. Raising is deliberate — silently reusing the other message's
    ordinal would let a caller believe it owns a turn it does not.
    """


@dataclass(frozen=True)
class TurnClaim:
    """The outcome of a successful claim (fresh or recovered)."""

    conversation_id: str
    request_id: str
    turn_seq: int
    assistant_message_id: str


# The guarded upsert. See the module docstring and design Rev 4 §1.2.
#
#   DO UPDATE ... WHERE agent_turn_claims.assistant_message_id = EXCLUDED.assistant_message_id
#
# returns a row for (a) a fresh insert and (b) an idempotent retry of the same
# message, and NO row when the message differs.
_CLAIM_SQL = text(
    """
    INSERT INTO agent_turn_claims
            (conversation_id, request_id, turn_seq, assistant_message_id,
             org_id, app_id, created_date)
    VALUES  (:conversation_id, :request_id, nextval('agent_turn_seq'),
             :assistant_message_id, :org_id, :app_id, now())
    ON CONFLICT (conversation_id, request_id) DO UPDATE
        SET assistant_message_id = agent_turn_claims.assistant_message_id
        WHERE agent_turn_claims.assistant_message_id
              = EXCLUDED.assistant_message_id
    RETURNING turn_seq
    """
)

_ADVANCE_POINTER_SQL = text(
    """
    UPDATE agent_conversations
       SET current_turn_seq = GREATEST(current_turn_seq, :turn_seq)
     WHERE id = :conversation_id
    """
)


def _bind_timeouts(db: Any) -> None:
    """Apply bounded lock/statement timeouts to THIS transaction only."""
    db.execute(text(f"SET LOCAL lock_timeout = '{CLAIM_LOCK_TIMEOUT_MS}ms'"))
    db.execute(
        text(f"SET LOCAL statement_timeout = '{CLAIM_STATEMENT_TIMEOUT_MS}ms'")
    )


def claim_turn(
    db: Any,
    *,
    conversation_id: str,
    request_id: str,
    assistant_message_id: str,
    org_id: Optional[str] = None,
    app_id: Optional[str] = None,
    commit: bool = True,
) -> TurnClaim:
    """Allocate or recover the turn ordinal for one logical stream request.

    The caller supplies the ALREADY-VALIDATED ``assistant_message_id`` (exact
    message identity + tenant/role/session checks happen before this call; see
    design §2.2 V1–V6). This function does not itself authorize the caller — it
    must never be treated as an authorization check.

    Args:
        db: an open SQLAlchemy session. The caller owns its lifetime.
        conversation_id: the conversation this turn belongs to.
        request_id: stable id for ONE logical send (reused on retries).
        assistant_message_id: the assistant row this turn writes to.
        org_id / app_id: tenant stamp for RLS (defaults applied when omitted).
        commit: commit on success (default). Pass ``False`` to compose with an
            outer transaction — the caller must then commit/rollback.

    Returns:
        :class:`TurnClaim` with the ordinal.

    Raises:
        ClaimMessageMismatch: the request_id is bound to another message.
        ClaimError: the claim could not be attributed (unexpected state).
    """
    if not conversation_id:
        raise ClaimError("conversation_id is required")
    if not request_id:
        raise ClaimError("request_id is required")
    if not assistant_message_id:
        raise ClaimError("assistant_message_id is required")

    params = {
        "conversation_id": conversation_id,
        "request_id": request_id,
        "assistant_message_id": assistant_message_id,
        "org_id": org_id or "default-org",
        "app_id": app_id or "default-app",
    }

    _bind_timeouts(db)

    row = db.execute(_CLAIM_SQL, params).first()
    if row is None:
        # The (conversation_id, request_id) exists with a different message.
        # Nothing was inserted or mutated; report deterministically.
        db.rollback()
        logger.info(
            "turn_claim: message mismatch for request %s on conv %s "
            "(existing claim bound to a different assistant message)",
            request_id, conversation_id,
        )
        raise ClaimMessageMismatch(
            f"request_id {request_id!r} is already bound to a different "
            f"assistant message in conversation {conversation_id!r}"
        )

    turn_seq = int(row[0])

    # Advance the pointer monotonically. GREATEST() means an out-of-order claim
    # commit can never pull the current ordinal backwards.
    db.execute(
        _ADVANCE_POINTER_SQL,
        {"conversation_id": conversation_id, "turn_seq": turn_seq},
    )

    if commit:
        db.commit()

    logger.debug(
        "turn_claim: conv=%s request=%s msg=%s ordinal=%s",
        conversation_id, request_id, assistant_message_id, turn_seq,
    )
    return TurnClaim(
        conversation_id=conversation_id,
        request_id=request_id,
        turn_seq=turn_seq,
        assistant_message_id=assistant_message_id,
    )


def lookup_claim(
    db: Any, *, conversation_id: str, request_id: str,
) -> Optional[TurnClaim]:
    """Read an existing claim without allocating one (diagnostics/retry check)."""
    row = db.execute(
        text(
            "SELECT turn_seq, assistant_message_id FROM agent_turn_claims "
            "WHERE conversation_id = :c AND request_id = :r"
        ),
        {"c": conversation_id, "r": request_id},
    ).first()
    if row is None:
        return None
    return TurnClaim(
        conversation_id=conversation_id,
        request_id=request_id,
        turn_seq=int(row[0]),
        assistant_message_id=row[1],
    )


def current_ordinal(db: Any, conversation_id: str) -> Optional[int]:
    """The conversation's current claimed ordinal, or None if no claim yet."""
    val = db.execute(
        text(
            "SELECT current_turn_seq FROM agent_conversations WHERE id = :c"
        ),
        {"c": conversation_id},
    ).scalar()
    return None if val is None else int(val)
