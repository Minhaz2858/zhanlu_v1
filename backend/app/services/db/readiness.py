"""Source readiness gate — the only thing that decides whether a source may answer.

The spec this implements: a newly connected database moves through

    CONNECTED → DISCOVERING → INDEXED → ENRICHED → VALIDATED → NL2SQL_READY

and "must not be used for unrestricted NL2SQL while it is not NL2SQL_READY".
Partial sources may be used *only* when the limitation is disclosed.

Measured state before this module (live deployment, 9 sources):

    db_type     catalog_status   count
    postgresql  ready             3
    postgresql  pending           2      <-- offered to the agent, queried anyway
    mariadb     ready             1
    mysql       ready             1

``catalog_status`` was read ONLY by the indexer, the watchdog and the UI
(``routers/knowledge_bases.py``) — never on an answering path. A source whose
structure was never catalogued, or whose catalog failed, was still offered as
a queryable source and answered from. That is the "fabricate an answer rather
than disclose a limitation" failure mode, and nothing could prevent it.

Design rules
------------
1. **Every answer path asks this module.** ``db_tools`` (list / describe /
   execute / answer / profile), ``NLAnswerService.answer`` and the prompt
   builder all call ``gate()``; the gate is not advisory.
2. **Refuse, clarify or disclose — never guess.** A source that is not
   ``nl2sql_ready`` returns a refusal payload whose ``error`` is a
   business-language sentence the agent can pass to the user, plus machine
   fields (``source_state`` / ``error_kind``) for logs and UI. Nothing is
   invented and no query is attempted.
3. **Legacy rows are derived, not assumed ready.** ``nl2sql_state`` is "" for
   every source indexed before this build; the gate derives an honest state
   from ``catalog_status``: ``ready`` → ready, ``indexing`` → discovering,
   ``error`` → discovery_failed, ``pending`` → connected (never prepared).
4. **Unknown dialect is a refusal, not a default.** If no dialect capability
   profile exists for the source, the source cannot be executed against and
   the gate says so instead of emitting MySQL/PostgreSQL-flavoured SQL.
5. **Feature-flagged but ON by default** (``NL2SQL_READINESS_GATE_ENABLED``).
   Setting it False restores the old permissive behaviour; the verdict is still
   computed and attached to the result as disclosure, so the limitation is
   visible either way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── lifecycle vocabulary ───────────────────────────────────────────────────

STATE_CONNECTED = "connected"
STATE_DISCOVERING = "discovering"
STATE_INDEXED = "indexed"
STATE_ENRICHED = "enriched"
STATE_VALIDATED = "validated"
STATE_READY = "nl2sql_ready"
STATE_DISCOVERY_FAILED = "discovery_failed"
STATE_ENRICHMENT_PARTIAL = "enrichment_partial"
STATE_VALIDATION_FAILED = "validation_failed"

# Ordered happy path — the indexer walks it, the tests pin it.
LIFECYCLE: tuple[str, ...] = (
    STATE_CONNECTED,
    STATE_DISCOVERING,
    STATE_INDEXED,
    STATE_ENRICHED,
    STATE_VALIDATED,
    STATE_READY,
)

FAILURE_STATES: tuple[str, ...] = (
    STATE_DISCOVERY_FAILED,
    STATE_ENRICHMENT_PARTIAL,
    STATE_VALIDATION_FAILED,
)

ALL_STATES: tuple[str, ...] = LIFECYCLE + FAILURE_STATES

# States whose structure is being read ("in flight" — they will move on their own).
_IN_FLIGHT = (STATE_DISCOVERING,)

# Mid-lifecycle states that are NOT ready and will not advance without help:
# preparation stopped there, so only part of the structure is known.
_STALLED = (STATE_INDEXED, STATE_ENRICHED, STATE_VALIDATED)

# Every state only a LIVE preparation run may hold: in flight, or part-way
# through. A database source that is not indexing while holding one of these
# has had its run die on it (killed process, OOM, container restart), and the
# staged state is then a claim nobody can honour — the watchdog
# (knowledge_graph/catalog_watchdog.py) reconciles exactly this pair. The
# terminal writers (catalog_indexer._classify_completion) never return a
# mid-lifecycle state, so a healthy source can never hold this pair.
# Exported so the vocabulary keeps one home.
MID_LIFECYCLE: tuple[str, ...] = _IN_FLIGHT + _STALLED

# Legacy catalog_status → derived lifecycle state (only used when
# nl2sql_state is empty, i.e. the source predates the staged lifecycle).
_LEGACY_MAP = {
    "ready": STATE_READY,
    "indexing": STATE_DISCOVERING,
    "error": STATE_DISCOVERY_FAILED,
    "failed": STATE_DISCOVERY_FAILED,
    "pending": STATE_CONNECTED,
}

_FILE_KINDS = ("file", "document", "documents", "upload", "url", "website", "web")

# ── business-language disclosures ──────────────────────────────────────────
# No plumbing: no table names, no endpoint names, no state-machine jargon.
# The agent passes these through in the user's own language.

_DISCLOSURE = {
    STATE_CONNECTED: (
        "This data source has not been prepared for questions yet — its tables "
        "and columns have not been catalogued, so nothing is known about its "
        "contents. It cannot be queried until preparation has run."
    ),
    STATE_DISCOVERING: (
        "This data source is still being prepared: its structure is being "
        "catalogued right now. Please try again in a moment."
    ),
    STATE_INDEXED: (
        "Preparation of this data source stopped before it finished, so only "
        "part of its structure is known. Preparation needs to be re-run before "
        "questions can be answered reliably from it."
    ),
    STATE_ENRICHED: (
        "Preparation of this data source stopped before its final check, so its "
        "descriptions are incomplete. Preparation needs to be re-run before "
        "questions can be answered reliably from it."
    ),
    STATE_VALIDATED: (
        "This data source passed its structure check but its final preparation "
        "step did not complete. Re-run preparation before relying on its answers."
    ),
    STATE_DISCOVERY_FAILED: (
        "This data source could not be prepared: its structure could not be "
        "read, so nothing is known about its tables and columns. It cannot be "
        "queried until preparation succeeds."
    ),
    STATE_VALIDATION_FAILED: (
        "This data source did not pass its final preparation check and is not "
        "yet safe to query. Please re-run preparation or ask an administrator "
        "to review the connection."
    ),
    STATE_ENRICHMENT_PARTIAL: (
        "Only part of this data source is described — its structure is known "
        "but some descriptions or examples are missing. Answers may be less "
        "precise than usual, and any gap will be stated explicitly."
    ),
    "unknown_dialect": (
        "Questions cannot be answered from this data source yet: it is a type of "
        "database this platform does not know how to read. No query will be "
        "attempted against it."
    ),
    "no_such_source": "That data source does not exist or is no longer available.",
}


@dataclass(frozen=True)
class SourceReadiness:
    """Verdict for one source. ``allowed`` is the only thing callers branch on."""

    allowed: bool
    limited: bool
    state: str
    code: str
    disclosure: str = ""
    detail: str = ""
    source_id: str = ""
    source_name: str = ""
    # Set when the verdict was derived from catalog_status rather than the
    # staged lifecycle — a source that has not been re-prepared since the
    # readiness feature shipped.
    derived: bool = False

    @property
    def ready(self) -> bool:
        return self.state == STATE_READY

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "state": self.state,
            "code": self.code,
            "allowed": self.allowed,
            "limited": self.limited,
            "disclosure": self.disclosure,
            "detail": self.detail,
            "derived": self.derived,
        }


def _flag(name: str, default: bool) -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, name, default))
    except Exception:  # pragma: no cover - settings always importable in prod
        return default


def gate_enabled() -> bool:
    """Whether a not-ready source is actually refused (vs. disclosed only)."""
    return _flag("NL2SQL_READINESS_GATE_ENABLED", True)


def partial_allowed() -> bool:
    """Whether a partially enriched source may answer with a disclosure."""
    return _flag("NL2SQL_READINESS_ALLOW_PARTIAL", True)


# ── state derivation ───────────────────────────────────────────────────────


def derive_state(catalog_status: str | None) -> tuple[str, bool]:
    """Honest state for a source that predates the staged lifecycle.

    Returns ``(state, derived)``. ``derived`` is True when we had to infer,
    which the verdict surfaces so an operator can tell "never prepared on this
    build" apart from "genuinely ready".
    """
    status = (catalog_status or "").strip().lower()
    if status in _LEGACY_MAP:
        return _LEGACY_MAP[status], True
    return STATE_CONNECTED, True


def effective_state(kb: Any) -> tuple[str, bool]:
    """(state, derived) for a KnowledgeBase row or dict."""
    if kb is None:
        return "", False
    if isinstance(kb, dict):
        state = (kb.get("nl2sql_state") or "").strip().lower()
        catalog_status = kb.get("catalog_status")
    else:
        state = (getattr(kb, "nl2sql_state", "") or "").strip().lower()
        catalog_status = getattr(kb, "catalog_status", None)
    if state:
        return state, False
    return derive_state(catalog_status)


def _source_kind(kb: Any) -> str:
    if isinstance(kb, dict):
        return (kb.get("source_kind") or "").strip().lower()
    return (getattr(kb, "source_kind", "") or "").strip().lower()


def _get(kb: Any, name: str, default: Any = None) -> Any:
    if isinstance(kb, dict):
        return kb.get(name, default)
    return getattr(kb, name, default)


# ── the gate ───────────────────────────────────────────────────────────────


def evaluate(kb: Any, *, capability: Any = None) -> SourceReadiness:
    """Verdict for one source row.

    ``kb`` may be a KnowledgeBase ORM row or its ``to_dict()`` payload.
    ``capability`` is an optional KBCapabilityProfile (or dict) so a caller
    that already loaded it doesn't trigger another query.
    """
    if kb is None:
        return SourceReadiness(
            allowed=not gate_enabled(),
            limited=False,
            state="",
            code="no_such_source",
            disclosure=_DISCLOSURE["no_such_source"],
            detail="knowledge base row not found",
        )

    source_id = str(_get(kb, "id", "") or "")
    source_name = str(_get(kb, "name", "") or "")
    kind = _source_kind(kb)

    # Document sources have their own lifecycle (indexing_status) — this gate
    # is about database sources and must not block them.
    if kind and any(kind.startswith(k) for k in _FILE_KINDS):
        return SourceReadiness(
            allowed=True,
            limited=False,
            state="not_applicable",
            code="not_applicable",
            source_id=source_id,
            source_name=source_name,
            detail="document source — readiness gate does not apply",
        )

    state, derived = effective_state(kb)
    db_type = _get(kb, "db_type", "") or ""
    catalog_status = _get(kb, "catalog_status", "") or ""
    reason = _get(kb, "nl2sql_state_reason", "") or ""
    detail = (
        f"state={state or 'unset'} catalog_status={catalog_status or 'unset'} "
        f"db_type={db_type or 'unset'}"
        + (f" reason={reason}" if reason else "")
    )

    def verdict(allowed: bool, limited: bool, code: str, disclosure: str) -> SourceReadiness:
        return SourceReadiness(
            allowed=allowed,
            limited=limited,
            state=state,
            code=code,
            disclosure=disclosure,
            detail=detail,
            source_id=source_id,
            source_name=source_name,
            derived=derived,
        )

    # 1. dialect must be identifiable — otherwise no SQL can be authored or
    #    executed for this source, and borrowing another dialect is forbidden.
    from app.services.db.dialect import maybe_profile

    prof = maybe_profile(db_type)
    if prof is None and capability is not None:
        cap_dialect = (
            capability.get("dialect") if isinstance(capability, dict)
            else getattr(capability, "dialect", None)
        )
        prof = maybe_profile(cap_dialect)
    if prof is None:
        return verdict(
            allowed=False,
            limited=False,
            code="unknown_dialect",
            disclosure=_DISCLOSURE["unknown_dialect"],
        )

    # 2. happy path
    if state == STATE_READY:
        return verdict(True, False, "ready", "")

    # 3. partially enriched — allowed, but the limitation travels with the answer
    if state == STATE_ENRICHMENT_PARTIAL:
        allow = partial_allowed()
        return verdict(
            allowed=allow,
            limited=True,
            code="partial",
            disclosure=_DISCLOSURE[STATE_ENRICHMENT_PARTIAL],
        )

    # 4. in flight — a wait, not a failure
    if state in _IN_FLIGHT:
        return verdict(
            allowed=False,
            limited=False,
            code="discovering",
            disclosure=_DISCLOSURE[STATE_DISCOVERING],
        )

    # 5. stalled mid-lifecycle — preparation stopped, structure incomplete
    if state in _STALLED:
        return verdict(
            allowed=False,
            limited=False,
            code="stalled",
            disclosure=_DISCLOSURE[state],
        )

    # 6. terminal failures
    if state == STATE_DISCOVERY_FAILED:
        return verdict(
            allowed=False,
            limited=False,
            code="discovery_failed",
            disclosure=_DISCLOSURE[STATE_DISCOVERY_FAILED],
        )
    if state == STATE_VALIDATION_FAILED:
        return verdict(
            allowed=False,
            limited=False,
            code="validation_failed",
            disclosure=_DISCLOSURE[STATE_VALIDATION_FAILED],
        )

    # 7. connected / never prepared
    return verdict(
        allowed=False,
        limited=False,
        code="not_prepared",
        disclosure=_DISCLOSURE[STATE_CONNECTED],
    )


def evaluate_source(db: Session, kb_id: str) -> SourceReadiness:
    """Verdict for a source id — loads the row (and its capability profile)."""
    try:
        from app.models.knowledge_base import KnowledgeBase

        kb = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
            .first()
        )
    except Exception:
        logger.exception("readiness: could not load source %s", kb_id)
        return SourceReadiness(
            allowed=False,
            limited=False,
            state="",
            code="no_such_source",
            disclosure=_DISCLOSURE["no_such_source"],
            detail="source lookup failed",
            source_id=str(kb_id or ""),
        )
    if kb is None:
        return SourceReadiness(
            allowed=False,
            limited=False,
            state="",
            code="no_such_source",
            disclosure=_DISCLOSURE["no_such_source"],
            detail="no row for id",
            source_id=str(kb_id or ""),
        )
    return evaluate(kb, capability=load_capability(db, kb_id))


def load_capability(db: Session, kb_id: str) -> Any:
    """Persisted capability profile for a source, or None."""
    try:
        from app.models.kb_capability import KBCapabilityProfile

        return (
            db.query(KBCapabilityProfile)
            .filter(
                KBCapabilityProfile.kb_id == kb_id,
                KBCapabilityProfile.is_deleted == False,  # noqa: E712
            )
            .first()
        )
    except Exception:
        logger.debug("readiness: capability profile unavailable for %s", kb_id, exc_info=True)
        return None


def refusal_payload(readiness: SourceReadiness) -> dict[str, Any]:
    """Tool-result payload for a refused source.

    ``error`` is the business sentence (so an agent that echoes the error still
    says something a user can act on); the machine fields are plumbing and are
    never surfaced verbatim.
    """
    return {
        "success": False,
        "error": readiness.disclosure or "This data source is not ready to be queried yet.",
        "error_kind": "source_not_ready",
        "source_state": readiness.state,
        "source_ready": False,
        "readiness_code": readiness.code,
        "data_source_id": readiness.source_id or None,
        "disclosure": readiness.disclosure,
        "internal_detail": readiness.detail,
    }


def check_source(db: Session, kb_id: str) -> tuple[SourceReadiness, dict[str, Any] | None]:
    """``(readiness, refusal_or_None)`` for an answering path.

    ``refusal`` is non-None exactly when the source may not answer (or when the
    gate is disabled and the caller should still attach the disclosure).
    """
    readiness = evaluate_source(db, kb_id)
    if readiness.allowed and not readiness.limited:
        return readiness, None
    if readiness.allowed and readiness.limited:
        # Allowed with a limitation: no refusal, but the caller must disclose.
        return readiness, None
    if not gate_enabled():
        # Gate off: preserve the old behaviour but keep the verdict visible.
        logger.warning(
            "readiness: gate disabled — allowing not-ready source %s (%s)",
            readiness.source_id, readiness.detail,
        )
        return readiness, None
    logger.warning(
        "readiness: refusing source %s — %s", readiness.source_id, readiness.detail
    )
    return readiness, refusal_payload(readiness)


def list_verdicts(db: Session, kb_ids: list[str]) -> dict[str, SourceReadiness]:
    """Verdicts for several sources in one pass (list/prompt paths)."""
    out: dict[str, SourceReadiness] = {}
    for kb_id in kb_ids or []:
        try:
            out[kb_id] = evaluate_source(db, kb_id)
        except Exception:
            logger.exception("readiness: verdict failed for %s", kb_id)
    return out


def note(readiness: SourceReadiness) -> str:
    """One-line prompt annotation for a source the agent may be about to pick."""
    if readiness.code == "ready" or readiness.code == "not_applicable":
        return ""
    if readiness.limited:
        return "partially prepared — answers from it must state the limitation"
    if readiness.allowed:
        return "not fully prepared — disclose the limitation if used"
    return "not queryable yet — do not attempt to answer from it"


def set_state(
    db: Session,
    kb: Any,
    state: str,
    *,
    reason: str | None = None,
    commit: bool = True,
) -> None:
    """Record a lifecycle transition. Unknown states are rejected loudly."""
    if state not in ALL_STATES:
        raise ValueError(f"Unknown NL2SQL source state: {state!r}")
    if kb is None:
        return
    try:
        kb.nl2sql_state = state
        kb.nl2sql_state_reason = (reason or "")[:2000] or None
        kb.nl2sql_state_at = datetime.utcnow()
        if commit:
            db.commit()
    except Exception:
        logger.exception("readiness: could not set state %s", state)
        try:
            db.rollback()
        except Exception:
            pass


def set_state_by_id(
    db: Session, kb_id: str, state: str, *, reason: str | None = None
) -> None:
    """Transition a source inside an index job that only has the id."""
    from app.models.knowledge_base import KnowledgeBase

    try:
        (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.id == kb_id)
            .update(
                {
                    "nl2sql_state": state,
                    "nl2sql_state_reason": (reason or "")[:2000] or None,
                    "nl2sql_state_at": datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )
        db.commit()
    except Exception:
        logger.exception("readiness: could not set state %s for %s", state, kb_id)
        try:
            db.rollback()
        except Exception:
            pass
