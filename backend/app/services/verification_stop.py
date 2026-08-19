"""Turn-end verification guard for coding edits.

When the agent tries to finish a turn immediately after editing code files
without running any verification (tests, build, lint), this injects a nudge
to verify before claiming done.

Policy-only: it never runs checks itself. It detects "wrote code -> tried
to finish without verifying" and turns that into a synthetic follow-up
message. Filters out non-code files (.md, .txt, etc.) to avoid false
positives on documentation edits.

Inspired by Hermes' ``agent/verification_stop.py``, simplified for Zhanlu's
tool set (write_file is the only file-mutating tool).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.services.coding_context import ProjectFacts

logger = logging.getLogger(__name__)

_MAX_CHANGED_PATHS_IN_NUDGE = 8
# Fix 1c: nudge cap is config-driven (VERIFY_NUDGE_MAX), so it can share a
# single force budget with the goal contract instead of a hardcoded constant.
_DEFAULT_MAX_ATTEMPTS = settings.VERIFY_NUDGE_MAX

# Non-code file extensions whose edits carry no verifiable runtime behavior.
# When a turn touches ONLY these, verify-on-stop has nothing to check.
_NON_CODE_VERIFY_EXTENSIONS = frozenset({
    ".md", ".markdown", ".mdx", ".rst", ".txt", ".text",
    ".adoc", ".asciidoc", ".org", ".log", ".csv", ".tsv",
})

# Filenames (case-insensitive) that are pure prose even without a doc extension.
_NON_CODE_VERIFY_FILENAMES = frozenset({
    "license", "licence", "notice", "authors",
    "contributors", "changelog", "codeowners",
})

# Tools that count as "verification" — if any was called after the last
# write_file, the turn is considered verified.
_VERIFICATION_TOOL_NAMES = frozenset({"execute_code"})


def _is_non_code_path(raw: str) -> bool:
    """Return True when a changed path is documentation/prose with nothing to verify."""
    try:
        p = Path(str(raw))
    except Exception:
        return False
    suffix = p.suffix.lower()
    if suffix in _NON_CODE_VERIFY_EXTENSIONS:
        return True
    if not suffix and p.name.lower() in _NON_CODE_VERIFY_FILENAMES:
        return True
    return False


def _filter_verifiable_paths(paths: Iterable[str]) -> list[str]:
    """Drop documentation/prose paths; keep paths that could have verifiable behavior."""
    return [p for p in paths if p and not _is_non_code_path(p)]


def _format_changed_paths(paths: list[str]) -> str:
    shown = paths[:_MAX_CHANGED_PATHS_IN_NUDGE]
    lines = [f"- `{path}`" for path in shown]
    remaining = len(paths) - len(shown)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more")
    return "\n".join(lines)


def extract_written_file_paths(messages: list[dict[str, Any]]) -> list[str]:
    """Extract file paths from write_file tool calls in the conversation.

    Scans assistant tool_calls for write_file calls and extracts the ``path``
    argument from each.
    """
    paths: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            if not isinstance(fn, dict):
                continue
            if fn.get("name") != "write_file":
                continue
            args_str = fn.get("arguments", "{}")
            try:
                import json
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(args, dict):
                path = args.get("path")
                if isinstance(path, str) and path:
                    paths.append(path)
    return paths


def has_verification_after_last_write(messages: list[dict[str, Any]]) -> bool:
    """Check if a verification tool was called after the last write_file.

    Scans the message list for the last write_file tool_call, then checks
    if any verification tool (execute_code) was called after it.
    """
    last_write_index = -1
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            if isinstance(fn, dict) and fn.get("name") == "write_file":
                last_write_index = i

    if last_write_index == -1:
        return True  # no writes at all — nothing to verify

    # Check for verification tools after the last write
    for msg in messages[last_write_index + 1:]:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {})
            if isinstance(fn, dict) and fn.get("name") in _VERIFICATION_TOOL_NAMES:
                return True

    return False


def build_verify_on_stop_nudge(
    messages: list[dict[str, Any]],
    *,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    project_facts=None,
) -> str | None:
    """Return a synthetic follow-up when edited code lacks fresh verification.

    Args:
        messages: The conversation message list.
        attempts: How many times the nudge has already been issued this turn.
        max_attempts: Maximum times to nudge before giving up.
        project_facts: Optional ProjectFacts from coding_context for specific
            test command suggestions.

    Returns:
        A nudge string to inject as a user message, or None if no nudge needed.
    """
    if attempts >= max_attempts:
        return None

    # Extract all written file paths from the conversation
    all_paths = extract_written_file_paths(messages)
    if not all_paths:
        return None

    # Filter to verifiable (code) paths only
    verifiable_paths = _filter_verifiable_paths(all_paths)
    if not verifiable_paths:
        return None  # only docs/prose edited — nothing to verify

    # Check if verification was run after the last write
    if has_verification_after_last_write(messages):
        return None  # already verified

    # Build the nudge
    unique_paths = sorted(set(verifiable_paths))

    # P5: Include specific test command if project facts are available
    test_cmd = None
    if project_facts is not None:
        from app.services.coding_context import get_test_command_for_files
        test_cmd = get_test_command_for_files(unique_paths, project_facts)
    elif unique_paths:
        # Auto-detect project facts from the written file paths
        from app.services.coding_context import detect_project_facts, get_test_command_for_files
        # Infer workspace root from the first file path
        first_path = Path(unique_paths[0])
        workspace = first_path.parent if first_path.parent.exists() else Path.cwd()
        facts = detect_project_facts(workspace)
        test_cmd = get_test_command_for_files(unique_paths, facts)

    verify_instruction = "Run a verification check now — execute a test command, run a syntax check, or verify the build."
    if test_cmd:
        verify_instruction = (
            f"Run a verification check now — for example, execute `{test_cmd}` "
            f"to run the project's test suite. Read any failure output and fix "
            f"the code before claiming the work is complete."
        )

    nudge = (
        "[System: You edited code files in this turn but have not run any "
        "verification (tests, build, lint) since the last edit.\n\n"
        f"Changed code paths:\n{_format_changed_paths(unique_paths)}\n\n"
        f"{verify_instruction} If verification is not possible, "
        "explain the concrete blocker instead of claiming the work is verified.]"
    )
    return nudge


__all__ = [
    "build_verify_on_stop_nudge",
    "extract_written_file_paths",
    "has_verification_after_last_write",
]
