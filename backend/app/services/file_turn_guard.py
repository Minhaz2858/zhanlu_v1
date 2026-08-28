"""File-deliverable turn-guard: enforce that a requested non-pptx file
deliverable (html, docx, pdf, xlsx, md) is produced via ``create_artifact``.

Mirrors ``pptx_turn_guard`` for the artifact pipeline.  Three complementary
mechanisms:

1. **Synthesis-boundary nudge (primary)** — when the model ends its turn
   with text only (no tool calls) but the user asked for a file deliverable
   and no artifact was created yet, inject one nudge message (cap per turn)
   instructing it to call ``create_artifact(type=<format>)`` now.
2. **T-3 budget forcing (secondary)** — when the tool-loop iteration count
   is within ``FILE_FORCE_WINDOW`` of the iteration cap and the artifact
   still does not exist, force ``tool_choice`` to ``create_artifact`` so
   the model must emit the call.
3. **Give-up re-prompt (2026-08-28 QA hardening)** — when the model closes
   the turn with a graceful-fallback apology ("trouble putting it
   together", "couldn't put together", "try again with a more specific
   request") instead of the deliverable, and the user asked for a file with
   no artifact produced, inject a stronger re-prompt (same per-turn cap)
   telling it to call ``create_artifact`` NOW rather than deflecting.  This
   path also catches **pptx** requests — the QA deck case — even though the
   regular pptx nudge/disclose belongs to ``pptx_turn_guard`` (whose flag
   defaults to OFF), so a graceful-fallback close never ends a deck turn
   unanswered.

When the remaining budget is too short to run the tool and write final
text, a disclosure sentence is appended instead so the user is never
silently left without the file.

All helpers are pure (no LLM calls, no DB) and gated by the
``FILE_TURN_GUARD_ENABLED`` flag — when the flag is off they are inert.

This guard covers **all non-pptx file formats** (html, docx, pdf, xlsx,
md) for the regular nudge/disclose/force paths.  PPTX is handled by
``pptx_turn_guard.py`` which has its own flag.  When both could fire, pptx
wins (more specific) — the give-up re-prompt being the documented
exception above.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.services.pptx_turn_guard import (
    build_pptx_disclosure,
    pptx_artifact_created,
)
from app.services.synexia.intent_router import detect_file_intent, FileFormat

# Formats covered by this guard (excludes pptx — handled by pptx_turn_guard).
_FILE_FORMATS = frozenset({"html", "docx", "pdf", "xlsx", "md"})

# Formats this guard can re-prompt for on the give-up path — includes pptx
# (usually pptx_turn_guard's job, but a graceful-fallback close must not
# go unanswered when that guard is silent or disabled).
_ALL_FILE_FORMATS = frozenset({"html", "docx", "pdf", "xlsx", "md", "pptx"})

# Phrases that indicate the agent gave up on producing the deliverable
# instead of ending the turn with it.  Matched case-insensitively against
# the final assistant text at the synthesis boundary.  Deliberately
# specific: bare "try again" / "could not" are too common in legitimate
# text (and appear in our own disclosure sentence) — the give-up intent
# must be explicit.  Bare "please try again" / "didn't work out" alone are
# NOT enough (false positives on ordinary closes); the "give up"/"gave up"
# family is handled separately below with phrase boundaries and negation
# stripping, so "I won't give up" / "will not give up" / "never give up"
# (commitments to continue) never count as giving up.
_GIVE_UP_PHRASES: tuple[str, ...] = (
    "trouble putting",
    "had trouble",
    "having trouble",
    "couldn't put together",
    "could not put together",
    "can't put together",
    "cannot put together",
    "wasn't able to put",
    "was not able to put",
    "try again with a more specific request",
    "could you try again",
    "unable to produce",
    "couldn't produce",
    "could not produce",
    "unable to create",
    "couldn't create",
    "could not create",
    "failed to create",
)

# The "give up" verb family — matched with phrase boundaries so substrings
# can't fire.  Negated statements ("won't give up", "will not give up",
# "never give up", ...) are stripped BEFORE matching: those are the agent
# committing to continue, the opposite of the give-up pattern.
_GIVE_UP_VERB = re.compile(r"\b(?:give\s+up|gave\s+up)\b", re.IGNORECASE)
_NEGATED_GIVE_UP = re.compile(
    r"(?:won'?t|will not|would not|wouldn'?t|should not|shouldn'?t|"
    r"can'?t|cannot|can not|could not|couldn'?t|do not|don'?t|did not|"
    r"didn'?t|must not|mustn'?t|shall not|shan'?t|never)\s+"
    r"(?:give\s+up|gave\s+up)\b",
    re.IGNORECASE,
)


def detect_give_up(final_assistant_text: str | None) -> bool:
    """True when the final assistant text is a graceful-fallback apology.

    Detects the QA pattern (2026-08-28): the agent ran many tool calls but
    closed the turn with "trouble putting it together / couldn't put
    together / try again with a more specific request" and no artifact.
    """
    if not final_assistant_text:
        return False
    scrubbed = _NEGATED_GIVE_UP.sub(" ", final_assistant_text.lower())
    if _GIVE_UP_VERB.search(scrubbed):
        return True
    return any(phrase in scrubbed for phrase in _GIVE_UP_PHRASES)


# Tool names that can produce a file deliverable.  ``create_artifact`` is
# the primary path; ``run_sandbox_skill`` / ``Skill`` cover skill routes.
_FILE_BUILD_TOOLS = frozenset(
    {"create_artifact", "run_sandbox_skill", "Skill", "skill"}
)

# Force the create_artifact call when this many iterations remain.
FILE_FORCE_WINDOW = 4

# Default cap for synthesis-boundary nudges per turn (configurable via
# settings.FILE_NUDGE_MAX). After the last allowed nudge the loop MUST force
# create_artifact via tool_choice (force_next=True) — prose deflection can
# never end the turn without a file.
_FILE_NUDGE_CAP = 2


def is_file_deliverable_request(
    user_content: str | None,
    output_format: str | None = None,
) -> tuple[bool, Optional[FileFormat]]:
    """Return ``(True, format)`` when the user asked for a non-pptx file deliverable.

    Detection uses two signals:
    1. ``detect_file_intent(user_content)`` — keyword matching in the prompt
    2. ``output_format`` — explicit format from automation runtime / harness

    Returns ``(False, None)`` when the request is for PPTX (handled by
    pptx_turn_guard) or no file intent is detected.
    """
    # Check explicit output_format first (automation runs).
    if output_format:
        fmt = output_format.strip().lower()
        if fmt in _FILE_FORMATS:
            return True, fmt  # type: ignore[return-value]

    if not user_content:
        return False, None

    # Use Goal-Contract normalizer when available, else legacy regex.
    if getattr(settings, "GOAL_CONTRACT_ENABLED", False):
        from app.services.goal_contract import normalize_deliverable_intent
        norm = normalize_deliverable_intent(user_content)
        if norm in _FILE_FORMATS:
            return True, norm  # type: ignore[return-value]
        if norm == "pptx":
            return False, None  # PPTX is handled by pptx_turn_guard
    else:
        detected = detect_file_intent(user_content)
        if detected in _FILE_FORMATS:
            return True, detected

    return False, None


def _requested_file_format(
    user_content: str | None,
    output_format: str | None = None,
) -> Optional[str]:
    """Return the requested deliverable format — INCLUDING pptx — or None.

    Broader than ``is_file_deliverable_request``: the give-up re-prompt
    must also catch pptx requests (the 2026-08-28 QA deck case) even
    though the regular nudge/disclose paths for pptx belong to
    ``pptx_turn_guard``.
    """
    if output_format:
        fmt = output_format.strip().lower()
        if fmt in _ALL_FILE_FORMATS:
            return fmt
    if not user_content:
        return None
    if getattr(settings, "GOAL_CONTRACT_ENABLED", False):
        from app.services.goal_contract import normalize_deliverable_intent

        norm = normalize_deliverable_intent(user_content)
        if norm in _ALL_FILE_FORMATS:
            return norm
    else:
        detected = detect_file_intent(user_content)
        if detected in _ALL_FILE_FORMATS:
            return detected
    return None


# Tools that *schedule* a deliverable for runtime (vs producing it now).
# When such a tool succeeded with ``output_format=fmt``, the file-turn-guard
# must NOT nudge/disclose because the deliverable is satisfied at the next
# cron fire — not in this turn. See 2026-08-25 regression: setting up an
# HTML-output automation asked for "Output format: HTML report" via the
# keyword detector; without this exemption the chat appended
# "(The requested HTML report could not be generated within this turn's
# tool budget…)" despite ``create_automation`` having succeeded moments
# earlier with ``output_format=html``.
_AUTOMATION_SCHEDULING_TOOLS: frozenset[str] = frozenset(
    {
        "create_automation",
        "update_automation",
        "AutomationTask.create",
        "AutomationTask.update",
    }
)


def _automation_deliverable_scheduled(
    tool_calls_for_frontend: list[dict] | None,
    target_format: str | None,
) -> bool:
    """True when this turn scheduled an automation that will produce ``target_format``.

    Looks for any call in ``_AUTOMATION_SCHEDULING_TOOLS`` whose args blob
    contains ``"output_format"`` AND the literal ``target_format`` value.
    The status check is loose (we don't reject failed calls here — those
    will be visible in the activity rail and the user can retry).
    """
    if not tool_calls_for_frontend or not target_format:
        return False
    fmt = target_format.lower()
    for call in tool_calls_for_frontend:
        name = str(call.get("name") or "")
        if name not in _AUTOMATION_SCHEDULING_TOOLS:
            continue
        blob = (
            str(call.get("arguments_string") or "")
            + str(call.get("arguments") or "")
            + str(call.get("results") or "")
        )
        if '"output_format"' in blob or "'output_format'" in blob:
            if f'"{fmt}"' in blob or f"'{fmt}'" in blob:
                return True
    return False


def file_artifact_created(
    tool_calls_for_frontend: list[dict] | None,
    target_format: str | None = None,
) -> bool:
    """True when some call this turn already produced the file deliverable.

    Recognises BOTH:
    - ``_FILE_BUILD_TOOLS`` calls whose args/results mention ``target_format``,
      or any ``_FILE_FORMATS`` entry when ``target_format`` is None.
    - ``_AUTOMATION_SCHEDULING_TOOLS`` calls (e.g. ``create_automation``)
      that scheduled the deliverable for the next runtime via
      ``output_format=target_format``. Such calls satisfy the user's
      intent — the agent set up a cron — but do not produce the file in
      this turn; without this branch, the file-turn-guard would emit a
      misleading "could not be generated within this turn's tool budget"
      disclosure.
    """
    if not tool_calls_for_frontend:
        return False
    for call in tool_calls_for_frontend:
        name = str(call.get("name") or "")
        if name not in _FILE_BUILD_TOOLS:
            continue
        args = str(call.get("arguments_string") or call.get("arguments") or "")
        results = str(call.get("results") or "")
        blob = args + results
        if target_format:
            if target_format in blob:
                return True
        else:
            for fmt in _FILE_FORMATS:
                if fmt in blob:
                    return True
    # Automation-scheduled deliveries count as well (see docstring).
    if _automation_deliverable_scheduled(
        tool_calls_for_frontend, target_format
    ):
        return True
    return False


def should_force_create_file(
    user_content: str | None,
    tool_calls_for_frontend: list[dict] | None,
    *,
    iteration: int,
    max_iterations: int,
    has_artifact_tool: bool = True,
    dashboard_forced: bool = False,
    pptx_forced: bool = False,
    output_format: str | None = None,
) -> bool:
    """T-3 window forcing decision.

    True when the flag is on + the user asked for a file deliverable + no
    file artifact exists yet + ``create_artifact`` is available + the
    dashboard/pptx guard is NOT forcing + the loop is within
    ``FILE_FORCE_WINDOW`` of the cap.
    """
    if not getattr(settings, "FILE_TURN_GUARD_ENABLED", False):
        return False
    if dashboard_forced or pptx_forced or not has_artifact_tool:
        return False
    is_file, fmt = is_file_deliverable_request(user_content, output_format)
    if not is_file:
        return False
    if file_artifact_created(tool_calls_for_frontend, fmt):
        return False
    return iteration >= max_iterations - FILE_FORCE_WINDOW


def build_file_nudge(fmt: str) -> str:
    """Synthesis-boundary nudge message (injected as a synthetic user turn)."""
    format_labels = {
        "html": "HTML report/web page",
        "docx": "Word/DOCX document",
        "pdf": "PDF document",
        "xlsx": "Excel/XLSX workbook",
        "md": "Markdown document",
    }
    label = format_labels.get(fmt, fmt.upper())
    return (
        f"The user asked you to produce a {label} and you have not "
        f"created it yet. The {fmt} file IS the deliverable — do NOT end the turn "
        f"with a promise, summary, or plan. Call create_artifact(type='{fmt}', "
        f"title=<appropriate title>, payload=<the content from the query results "
        f"already in this conversation>) NOW. If the data is insufficient, call "
        f"create_artifact with what you have and state the limitation, "
        f"or explain exactly what blocked you."
    )


def build_file_disclosure(fmt: str) -> str:
    """Fallback sentence appended to final text when budget < 2."""
    format_labels = {
        "html": "HTML report",
        "docx": "Word document",
        "pdf": "PDF document",
        "xlsx": "Excel workbook",
        "md": "Markdown document",
    }
    label = format_labels.get(fmt, fmt.upper())
    return (
        f"(The requested {label} could not be generated within this turn's "
        f"tool budget. Please ask again and I will build it.)"
    )


def build_give_up_reprompt(fmt: str) -> str:
    """Stronger re-prompt for the 'gave up' pattern (2026-08-28 QA).

    The agent already tried to close the turn with a graceful-fallback
    apology instead of the deliverable — this message calls that out and
    orders a ``create_artifact`` call NOW.  Supports pptx too (the QA deck
    case).
    """
    format_labels = {
        "html": "HTML report/web page",
        "docx": "Word/DOCX document",
        "pdf": "PDF document",
        "xlsx": "Excel/XLSX workbook",
        "md": "Markdown document",
        "pptx": "PowerPoint/PPTX deck",
    }
    label = format_labels.get(fmt, fmt.upper())
    return (
        f"You just ended your turn by telling the user you had trouble "
        f"producing the {label} they asked for — but the {fmt} file IS the "
        f"deliverable. Do NOT give up and do NOT ask the user to retry. "
        f"Call create_artifact(type='{fmt}', title=<appropriate title>, "
        f"payload=<the content from the query results already in this "
        f"conversation>) NOW. If the data is insufficient, call "
        f"create_artifact with what you have and state the limitation, "
        f"or explain exactly what blocked you."
    )


@dataclass
class FileTurnGuardResult:
    """Outcome of the synthesis-boundary check.

    ``action`` is one of:

    - ``"nudge"`` — inject ``message`` as a synthetic user turn and continue
    - ``"disclose"`` — append ``message`` to the final assistant text
    - ``"none"`` — nothing to do (flag off / not a file request / already
      created / nudge cap reached)

    ``force_next`` is True when this is the LAST allowed nudge: the loop must
    then force ``tool_choice=create_artifact`` on the next iteration instead
    of accepting another prose deflection.

    ``detected_format`` carries the matched file format (for logging).
    """

    action: str
    message: str = ""
    force_next: bool = False
    detected_format: str = ""


def file_turn_guard(
    user_content: str | None,
    tool_calls_for_frontend: list[dict] | None,
    *,
    budget_remaining: int,
    attempts: int,
    output_format: str | None = None,
    final_assistant_text: str | None = None,
) -> FileTurnGuardResult:
    """Synthesis-boundary check: nudge (cap per turn) or disclose.

    ``budget_remaining`` is the number of tool-loop iterations left in this
    turn; ``attempts`` is how many nudges were already injected this turn.
    ``output_format`` is the explicit format from automation runtime.
    ``final_assistant_text`` is the model's closing text on a text-only
    turn — when it is a graceful-fallback apology (see ``detect_give_up``),
    a stronger give-up re-prompt is emitted instead of the plain nudge, and
    pptx requests are caught here too (2026-08-28 QA deck case).
    """
    if not getattr(settings, "FILE_TURN_GUARD_ENABLED", False):
        return FileTurnGuardResult("none")
    cap = int(getattr(settings, "FILE_NUDGE_MAX", _FILE_NUDGE_CAP) or _FILE_NUDGE_CAP)
    if attempts >= cap:
        return FileTurnGuardResult("none")
    fmt = _requested_file_format(user_content, output_format)
    if not fmt:
        return FileTurnGuardResult("none")
    if fmt == "pptx":
        if pptx_artifact_created(tool_calls_for_frontend):
            return FileTurnGuardResult("none")
    elif file_artifact_created(tool_calls_for_frontend, fmt):
        return FileTurnGuardResult("none")
    gave_up = detect_give_up(final_assistant_text)
    if not gave_up:
        # Regular paths: pptx belongs to pptx_turn_guard (more specific).
        if fmt == "pptx":
            return FileTurnGuardResult("none")
        if budget_remaining >= 2:
            return FileTurnGuardResult(
                "nudge",
                build_file_nudge(fmt),
                force_next=(attempts == cap - 1),
                detected_format=fmt,
            )
        return FileTurnGuardResult(
            "disclose",
            build_file_disclosure(fmt),
            detected_format=fmt,
        )
    # Give-up pattern (2026-08-28 QA): the agent deflected with a graceful
    # fallback ("trouble putting it together") instead of delivering.
    # Re-prompt with the stronger message, reusing the same per-turn cap.
    if budget_remaining >= 2:
        return FileTurnGuardResult(
            "nudge",
            build_give_up_reprompt(fmt),
            force_next=(attempts == cap - 1),
            detected_format=fmt,
        )
    return FileTurnGuardResult(
        "disclose",
        build_pptx_disclosure() if fmt == "pptx" else build_file_disclosure(fmt),
        detected_format=fmt,
    )
