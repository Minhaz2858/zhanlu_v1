"""Data grounding + user context for deck generation.

The deck pipeline historically builds slides from ``payload.chart.data`` — the
LLM-authored rows in the ``ReportCardPayload``.  If the agent never queried the
user's data (or put a thin summary in the payload), the deck is built on
fabricated/empty numbers.  This module recovers the REAL query rows the agent
fetched during the conversation:

* primary — ``ObservationRecord`` rows on the ``Execution`` (the live FSM path
  stores them under ``result_data["data"]``; also handled: ``rows`` and a
  nested ``result`` dict), and
* fallback — tool results embedded in the persisted conversation history
  (``_mine_historical_answer_rows`` / ``_mine_historical_ask_data_result``).

It also assembles the user/brand context the planner is blind to today: the
workspace brand kit, the user's business-role description, their learned
profile, and project memory.  The chat agent already sees all of this; the deck
planner should too, so decks stop reading like a template for anyone.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.models.agent_conversation import AgentConversation
from app.models.execution import Execution, ObservationRecord

logger = logging.getLogger(__name__)

# Observation types that can carry real query rows.  ``nl2sql`` is the live
# data-agent path; ``tool_call`` covers data tools that surface their rows in
# the recorded result (e.g. ERP KPI queries).
_DATA_OBSERVATION_TYPES = ("nl2sql", "tool_call")
# How many executions (most recent first) to scan for a conversation.
_MAX_EXECUTIONS = 3
# Default cap on recovered rows (keeps the planner/rendering sane).
DEFAULT_ROW_LIMIT = 2000


# ---------------------------------------------------------------------------
# Row extraction
# ---------------------------------------------------------------------------


def _extract_rows_from_result_data(result_data: Optional[dict]) -> list[dict]:
    """Pull row dicts out of an ObservationRecord ``result_data`` defensively.

    Rows may live under ``data`` (the live FSM path), ``rows``, or nested under
    ``result`` (a dict with its own ``data``/``rows``).  Returns ``[]`` when
    nothing row-like is found — callers fall back to the payload rows.
    """
    if not isinstance(result_data, dict):
        return []
    candidate = result_data.get("data")
    if not isinstance(candidate, list):
        candidate = result_data.get("rows")
    if not isinstance(candidate, list):
        nested = result_data.get("result")
        if isinstance(nested, dict):
            candidate = nested.get("data")
            if not isinstance(candidate, list):
                candidate = nested.get("rows")
    rows: list[dict] = []
    if isinstance(candidate, list):
        for r in candidate:
            if isinstance(r, dict):
                rows.append(r)
    return rows


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    """Dedupe by JSON identity, preserving first-seen order."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        key = json.dumps(r, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _rows_from_observations(
    observations: list[ObservationRecord], limit: int,
) -> list[dict]:
    """Return the row set from the observation carrying the most rows.

    Failed observations are skipped.  When several records carry data (e.g. a
    multi-step retrieval), the richest wins so the deck isn't planned off a
    single partial query.
    """
    best: list[dict] = []
    for obs in observations:
        if not getattr(obs, "success", True):
            continue
        rows = _extract_rows_from_result_data(getattr(obs, "result_data", None))
        if len(rows) > len(best):
            best = rows
        if len(best) >= limit:
            break
    return best[:limit]


# ---------------------------------------------------------------------------
# collect_grounded_rows
# ---------------------------------------------------------------------------


def collect_grounded_rows(
    db,
    *,
    artifact=None,
    conversation_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    user_message: str = "",
    limit: int = DEFAULT_ROW_LIMIT,
) -> list[dict]:
    """Recover the REAL query rows for a deck, highest-fidelity-first.

    Order of attempts:

    1. ``execution_id`` given → scan that ``Execution``'s data observations.
    2. Else ``conversation_id`` → scan the most recent executions for the
       conversation.
    3. Else mine the persisted conversation tool results.

    Returns ``[]`` when nothing is found — the caller then falls back to
    ``payload.chart.data`` (the historical behavior).
    """
    executions: list[Execution] = []
    if execution_id:
        ex = (
            db.query(Execution)
            .filter(Execution.id == execution_id)
            .first()
        )
        if ex is not None:
            executions = [ex]
    elif conversation_id:
        executions = (
            db.query(Execution)
            .filter(Execution.conversation_id == conversation_id)
            .order_by(Execution.created_date.desc())
            .limit(_MAX_EXECUTIONS)
            .all()
        )

    for ex in executions:
        observations = (
            db.query(ObservationRecord)
            .filter(
                ObservationRecord.execution_id == ex.id,
                ObservationRecord.observation_type.in_(_DATA_OBSERVATION_TYPES),
            )
            .order_by(ObservationRecord.seq.asc())
            .all()
        )
        rows = _rows_from_observations(observations, limit)
        if rows:
            return _dedupe_rows(rows)[:limit]

    if conversation_id:
        conv = (
            db.query(AgentConversation)
            .filter(AgentConversation.id == conversation_id)
            .first()
        )
        if conv is not None and getattr(conv, "messages", None):
            from app.services.generation_orchestrator import (
                _mine_historical_answer_rows,
                _mine_historical_ask_data_result,
            )

            mined = _mine_historical_answer_rows(conv.messages)
            if mined:
                return _dedupe_rows(mined)[:limit]
            best = _mine_historical_ask_data_result(conv.messages)
            if isinstance(best, dict):
                candidate = best.get("rows")
                if not isinstance(candidate, list):
                    nested = best.get("data") or best.get("result")
                    if isinstance(nested, dict):
                        candidate = nested.get("rows")
                if isinstance(candidate, list):
                    return _dedupe_rows(
                        [r for r in candidate if isinstance(r, dict)]
                    )[:limit]
    return []


# ---------------------------------------------------------------------------
# build_deck_user_context
# ---------------------------------------------------------------------------


def _safe(fn, default=None):
    """Run ``fn`` best-effort; log at debug and return ``default`` on failure."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — context assembly must never break rendering
        logger.debug("deck_data: %s failed: %s", getattr(fn, "__name__", "step"), exc)
        return default


def build_deck_user_context(
    db,
    *,
    artifact=None,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    user_message: str = "",
    agent_app_id: Optional[str] = None,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> Optional[dict]:
    """Assemble the user/brand context the deck planner should see.

    Returns a plain dict (JSON-serializable) or ``None`` when every source is
    empty.  Brand theming is always applied when a kit exists; role / profile /
    memory are flag-gated (same gates the chat system-prompt personalization
    uses) and best-effort.
    """
    from app.config import settings  # read at call time — never a stale singleton
    from app.services.artifacts.brand_kit import (
        brand_kit_to_theme_tokens,
        get_brand_kit,
    )

    out: dict[str, Any] = {}

    owner_user_id = user_id
    if not owner_user_id and artifact is not None:
        meta = getattr(artifact, "metadata_json", None) or {}
        owner_user_id = meta.get("owner_user_id")

    # ── Brand kit (always applied when present) ──
    kit = _safe(lambda: get_brand_kit(db, org_id=org_id, app_id=app_id))
    if isinstance(kit, dict) and kit:
        out["brand_name"] = (kit.get("name") or "").strip() or None
        out["brand_tokens"] = brand_kit_to_theme_tokens(kit)

    # ── Role description (flag-gated) ──
    if owner_user_id and getattr(settings, "ROLE_PERSONALIZATION_ENABLED", False):
        from app.models.user import User

        u = _safe(
            lambda: (
                db.query(User).filter(User.id == owner_user_id).first()
            )
        )
        if u is not None:
            role_parts: list[str] = []
            if getattr(u, "role_description_text", None):
                role_parts.append(u.role_description_text)
            if getattr(u, "role_descriptions", None):
                labels = [
                    str(x) for x in u.role_descriptions if isinstance(x, str)
                ]
                if labels:
                    role_parts.append("Roles: " + ", ".join(labels))
            if role_parts:
                out["role_text"] = " ".join(role_parts)

    # ── Learned user profile (flag-gated, filesystem-backed) ──
    if owner_user_id and getattr(
        settings, "USER_PROFILE_ENABLED", False
    ):
        def _profile():
            from app.services.user_profile import get_profile_prompt

            return get_profile_prompt(
                agent_app_id or "", owner_user_id, storage_dir=None
            )

        profile_text = _safe(_profile, default="")
        if profile_text:
            out["profile_text"] = profile_text

    # ── Project memory (best-effort; needs agent + user to scope) ──
    if agent_app_id and owner_user_id:
        project_id = None
        if conversation_id:
            conv = _safe(
                lambda: (
                    db.query(AgentConversation)
                    .filter(AgentConversation.id == conversation_id)
                    .first()
                )
            )
            if conv is not None:
                project_id = getattr(conv, "project_id", None)

        def _memory():
            from app.services.tool_handlers.memory_tool import (
                load_memory_snapshot,
            )

            snap = load_memory_snapshot(
                db, agent_app_id, owner_user_id, project_id=project_id
            )
            return (snap or {}).get("user") or ""

        memory_text = _safe(_memory, default="")
        if memory_text:
            out["memory_text"] = memory_text

    return out or None
