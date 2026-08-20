"""File-deliverable turn-guard: enforce that a requested non-pptx file
deliverable (html, docx, pdf, xlsx, md) is produced via ``create_artifact``.

Mirrors ``pptx_turn_guard`` for the artifact pipeline.  Two complementary
mechanisms:

1. **Synthesis-boundary nudge (primary)** — when the model ends its turn
   with text only (no tool calls) but the user asked for a file deliverable
   and no artifact was created yet, inject one nudge message (cap per turn)
   instructing it to call ``create_artifact(type=<format>)`` now.
2. **T-3 budget forcing (secondary)** — when the tool-loop iteration count
   is within ``FILE_FORCE_WINDOW`` of the iteration cap and the artifact
   still does not exist, force ``tool_choice`` to ``create_artifact`` so
   the model must emit the call.

When the remaining budget is too short to run the tool and write final
text, a disclosure sentence is appended instead so the user is never
silently left without the file.

All helpers are pure (no LLM calls, no DB) and gated by the
``FILE_TURN_GUARD_ENABLED`` flag — when the flag is off they are inert.

This guard covers **all non-pptx file formats** (html, docx, pdf, xlsx,
md).  PPTX is handled by ``pptx_turn_guard.py`` which has its own flag.
When both could fire, pptx wins (more specific).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.services.synexia.intent_router import detect_file_intent, FileFormat

# Formats covered by this guard (excludes pptx — handled by pptx_turn_guard).
_FILE_FORMATS = frozenset({"html", "docx", "pdf", "xlsx", "md"})

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


def file_artifact_created(
    tool_calls_for_frontend: list[dict] | None,
    target_format: str | None = None,
) -> bool:
    """True when some call this turn already produced the file deliverable.

    Only build-tool calls (``_FILE_BUILD_TOOLS``) are inspected.  If
    ``target_format`` is given, checks for that specific format substring
    in arguments/results.  If ``target_format`` is None, checks for ANY
    file format in ``_FILE_FORMATS``.
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
) -> FileTurnGuardResult:
    """Synthesis-boundary check: nudge (cap per turn) or disclose.

    ``budget_remaining`` is the number of tool-loop iterations left in this
    turn; ``attempts`` is how many nudges were already injected this turn.
    ``output_format`` is the explicit format from automation runtime.
    """
    if not getattr(settings, "FILE_TURN_GUARD_ENABLED", False):
        return FileTurnGuardResult("none")
    cap = int(getattr(settings, "FILE_NUDGE_MAX", _FILE_NUDGE_CAP) or _FILE_NUDGE_CAP)
    if attempts >= cap:
        return FileTurnGuardResult("none")
    is_file, fmt = is_file_deliverable_request(user_content, output_format)
    if not is_file or not fmt:
        return FileTurnGuardResult("none")
    if file_artifact_created(tool_calls_for_frontend, fmt):
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
