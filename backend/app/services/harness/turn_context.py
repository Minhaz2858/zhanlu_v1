"""Turn identity and the shadow trace, bound once per turn for EVERY turn path.

Why this module exists
----------------------
Phase 1c wired the chat run spine and Phase 2 the shadow trace, both *inside* the v2
tool loop in ``add_message``. But ``add_message`` has three paths and two of them
return before that wiring is reached:

    agents.py:6570  begin_turn lives here (before any path branches)
    agents.py:6601  workflow / flow_mode path   -> return conv.to_dict()
    agents.py:6712  SynexiaFSM path             -> return conv.to_dict()
    agents.py:7013  the v2 tool loop            -> Phase 1c/2 wiring (was here)

So a turn routed to the FSM — which is what a data-bound agent gets — carried no run,
no trace and no turn identity. Verified live 2026-09-18: a real turn through
``POST /apps/{app}/agents/conversations/v2/{id}/messages`` left
``harness_rule_events`` rows with an empty ``conversation_id`` and
``detail.primitive.trace_len == 0``, and created no AgentRun at all.

Binding therefore belongs at turn ENTRY, before the router branches, not inside one of
its paths. Call ``begin_turn()`` once at the top of a turn and ``end_turn()`` on each
exit that actually completes it.

Fail-open, like every other piece of harness telemetry: a failure here must never
change a turn. Both helpers swallow and log at debug.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def begin_turn(
    *,
    conversation_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    surface: Optional[str] = None,
    user_id: Optional[str] = None,
    **extra: Any,
) -> dict:
    """Bind turn identity and open a fresh shadow trace for this turn.

    Must run before the first tool call of the turn: the shadow layer compares rule
    decisions against the trace, and a trace opened late would attribute the model's
    earlier tool calls to nothing.
    """
    turn: dict = {}
    try:
        from app.services.harness.rule_events import bind_turn
        turn = bind_turn(
            conversation_id=conversation_id,
            agent_name=agent_name,
            surface=surface,
            user_id=user_id,
            **extra,
        )
    except Exception:
        logger.debug("harness bind_turn failed", exc_info=True)
    try:
        from app.services.harness.shadow import start_trace
        start_trace()
    except Exception:
        logger.debug("harness start_trace failed", exc_info=True)
    return turn


def end_turn() -> dict:
    """Resolve this turn's shadow fires against the finished trace.

    Returns ``{"rule_preceded_delivery": [...], "rule_fired_no_delivery": [...]}``, or
    ``{}`` when there is nothing to resolve. Call from every exit that completes a
    turn — a genuinely PAUSED turn (approval, decision-summary) must NOT call this:
    it has delivered nothing yet and counting it as a failure to deliver would be
    wrong.
    """
    try:
        from app.services.harness.shadow import finish_turn
        return finish_turn()
    except Exception:
        logger.debug("harness finish_turn failed", exc_info=True)
        return {}


__all__ = ["begin_turn", "end_turn"]
