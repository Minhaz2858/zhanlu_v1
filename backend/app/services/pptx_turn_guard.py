"""PPTX turn-guard: enforce that a requested PPT deliverable is produced.

Mirrors ``dashboard_turn_guard`` for the artifact pipeline.  Two
complementary mechanisms:

1. **Synthesis-boundary nudge (primary)** — when the model ends its turn
   with text only (no tool calls) but the user asked for a PPT and no
   pptx artifact was created yet, inject one nudge message (cap 1/turn)
   instructing it to call ``create_artifact(type="pptx")`` now.
2. **T-3 budget forcing (secondary)** — when the tool-loop iteration count
   is within ``PPTX_FORCE_WINDOW`` of the iteration cap and the pptx still
   does not exist, force ``tool_choice`` to ``create_artifact`` so the
   model must emit the call.

When the remaining budget is too short to run the tool and write final
text, a disclosure sentence is appended instead so the user is never
silently left without the file.

All helpers are pure (no LLM calls, no DB) and gated by the
``PPT_TURN_GUARD_ENABLED`` flag — when the flag is off they are inert.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.services.synexia.intent_router import detect_file_intent

# Tool names that can produce a pptx deliverable.  ``create_artifact`` is
# the primary path; ``run_sandbox_skill`` / ``Skill`` cover the html2pptx
# skill route — counted as "created" so the guard does not nag after a
# skill-path build.
_PPTX_BUILD_TOOLS = frozenset(
    {"create_artifact", "run_sandbox_skill", "Skill", "skill"}
)

# Force the create_artifact call when this many iterations remain.
PPTX_FORCE_WINDOW = 4

# Default cap for synthesis-boundary nudges per turn (configurable via
# settings.PPTX_NUDGE_MAX). After the last allowed nudge the loop MUST force
# create_artifact via tool_choice (force_next=True) — prose deflection can
# never end the turn without a deck.
_PPTX_NUDGE_CAP = 2


def is_pptx_request(user_content: str | None) -> bool:
    """True when the user asked for a PPT/PPTX deliverable.

    Goal-Contract mode delegates to the typo-tolerant normalizer (the single
    source of truth). Legacy reuses ``detect_file_intent`` (EN + ZH aware):
    'make a sales overview PPT', 'PowerPoint', '做一份销售总览PPT' → True.
    """
    if not user_content:
        return False
    if getattr(settings, "GOAL_CONTRACT_ENABLED", False):
        from app.services.goal_contract import normalize_deliverable_intent

        return normalize_deliverable_intent(user_content) == "pptx"
    return detect_file_intent(user_content) == "pptx"


def pptx_artifact_created(tool_calls_for_frontend: list[dict] | None) -> bool:
    """True when some call this turn already produced the pptx deck.

    Only build-tool calls (``_PPTX_BUILD_TOOLS``) are inspected, and only
    'pptx' substring presence in the arguments (or result blob) counts —
    so unrelated query results that merely mention 'pptx' do not.
    """
    if not tool_calls_for_frontend:
        return False
    for call in tool_calls_for_frontend:
        name = str(call.get("name") or "")
        if name not in _PPTX_BUILD_TOOLS:
            continue
        args = str(call.get("arguments_string") or call.get("arguments") or "")
        results = str(call.get("results") or "")
        if "pptx" in args or "pptx" in results:
            return True
    return False


def should_force_create_pptx(
    user_content: str | None,
    tool_calls_for_frontend: list[dict] | None,
    *,
    iteration: int,
    max_iterations: int,
    has_artifact_tool: bool = True,
    dashboard_forced: bool = False,
) -> bool:
    """T-3 window forcing decision.

    True when the flag is on + the user asked for a PPT + no pptx artifact
    exists yet + ``create_artifact`` is available + the dashboard guard is
    NOT forcing + the loop is within ``PPTX_FORCE_WINDOW`` of the cap.
    """
    if not getattr(settings, "PPT_TURN_GUARD_ENABLED", False):
        return False
    if dashboard_forced or not has_artifact_tool:
        return False
    if not is_pptx_request(user_content):
        return False
    if pptx_artifact_created(tool_calls_for_frontend):
        return False
    return iteration >= max_iterations - PPTX_FORCE_WINDOW


def build_pptx_nudge() -> str:
    """Synthesis-boundary nudge message (injected as a synthetic user turn)."""
    return (
        "The user asked you to produce a PowerPoint/PPTX deck and you have not "
        "created it yet. The .pptx file IS the deliverable — do NOT end the turn "
        "with a promise, summary, or plan. Call create_artifact(type='pptx', "
        "title=<deck title>, payload=<the deck content from the query results "
        "already in this conversation>) NOW. If the data is insufficient, call "
        "create_artifact with what you have and state the limitation in the deck, "
        "or explain exactly what blocked you."
    )


def build_pptx_disclosure() -> str:
    """Fallback sentence appended to final text when budget < 2."""
    return (
        "(The requested PPTX deck could not be generated within this turn's "
        "tool budget. Please ask again and I will build it.)"
    )


@dataclass
class PptxTurnGuardResult:
    """Outcome of the synthesis-boundary check.

    ``action`` is one of:

    - ``"nudge"`` — inject ``message`` as a synthetic user turn and continue
    - ``"disclose"`` — append ``message`` to the final assistant text
    - ``"none"`` — nothing to do (flag off / not a pptx request / already
      created / nudge cap reached)

    ``force_next`` is True when this is the LAST allowed nudge: the loop must
    then force ``tool_choice=create_artifact`` on the next iteration instead
    of accepting another prose deflection.
    """

    action: str
    message: str = ""
    force_next: bool = False


def pptx_turn_guard(
    user_content: str | None,
    tool_calls_for_frontend: list[dict] | None,
    *,
    budget_remaining: int,
    attempts: int,
) -> PptxTurnGuardResult:
    """Synthesis-boundary check: nudge (cap 1/turn) or disclose.

    ``budget_remaining`` is the number of tool-loop iterations left in this
    turn; ``attempts`` is how many nudges were already injected this turn.
    """
    if not getattr(settings, "PPT_TURN_GUARD_ENABLED", False):
        return PptxTurnGuardResult("none")
    cap = int(getattr(settings, "PPTX_NUDGE_MAX", _PPTX_NUDGE_CAP) or _PPTX_NUDGE_CAP)
    if attempts >= cap:
        return PptxTurnGuardResult("none")
    if not is_pptx_request(user_content):
        return PptxTurnGuardResult("none")
    if pptx_artifact_created(tool_calls_for_frontend):
        return PptxTurnGuardResult("none")
    if budget_remaining >= 2:
        return PptxTurnGuardResult(
            "nudge", build_pptx_nudge(), force_next=(attempts == cap - 1)
        )
    return PptxTurnGuardResult("disclose", build_pptx_disclosure())
