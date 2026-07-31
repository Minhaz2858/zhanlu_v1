"""Structured missing-config error helper.

The user wants every tool to be visible to the LLM by default, and have the
agent handle missing configuration conversationally (ask the user for the
values, then optionally write them to ``/root/zhanlu/.env`` and restart
the backend via ``update_env_config`` + ``docker_compose_restart``).

This module is the single source of truth for the missing-config response
shape — every handler that needs env vars / binary deps / infra should call
:meth:`missing_config_response` and return its result.

Response shape (stable across all tools — agents and tests depend on it)::

    {
        "success": False,
        "error": "Missing required configuration: ELEVENLABS_API_KEY, ...",
        "missing_config": ["ELEVENLABS_API_KEY", ...],
        "tool_name": "tts",
        "user_action_required": (
            "Ask the user to provide the missing values, then call "
            "update_env_config to write them to /root/zhanlu/.env and "
            "docker_compose_restart to apply."
        ),
    }
"""

from __future__ import annotations

import os
from typing import Iterable, List, Optional


# Fields the agent understands for the user-actionable instruction
_USER_ACTION_PROMPT = (
    "Please ask the user to provide the missing values, then call "
    "update_env_config to write them to /root/zhanlu/.env and "
    "docker_compose_restart to apply. Some tools also need a binary "
    "installed (e.g. playwright, ffmpeg) or external infrastructure "
    "(e.g. an MCP server, a Home Assistant instance) — those cannot be "
    "resolved by update_env_config alone."
)


def missing_config_response(
    tool_name: str,
    missing_env: Optional[Iterable[str]] = None,
    missing_binaries: Optional[Iterable[str]] = None,
    missing_infra: Optional[Iterable[str]] = None,
) -> dict:
    """Build a structured missing-config response for a tool.

    Args:
        tool_name: The tool's registered name (e.g. "tts").
        missing_env: Env var names that are not set (e.g. ["ELEVENLABS_API_KEY"]).
        missing_binaries: CLI binary names that are not on $PATH
            (e.g. ["playwright", "ffmpeg"]).
        missing_infra: External infrastructure that's not reachable
            (e.g. ["Home Assistant at http://homeassistant.local:8123"]).

    Returns:
        A dict matching the documented response shape.
    """
    parts: List[str] = []
    env_list = list(missing_env or [])
    bin_list = list(missing_binaries or [])
    infra_list = list(missing_infra or [])

    if env_list:
        parts.append(
            f"environment variable(s): {', '.join(env_list)}"
        )
    if bin_list:
        parts.append(
            f"required binary on PATH: {', '.join(bin_list)}"
        )
    if infra_list:
        parts.append(
            f"external infrastructure: {', '.join(infra_list)}"
        )

    if not parts:
        # Degenerate call — return a generic shape so the agent can still
        # surface something useful.
        return {
            "success": False,
            "error": f"Tool '{tool_name}' is not currently configured.",
            "missing_config": [],
            "tool_name": tool_name,
            "user_action_required": _USER_ACTION_PROMPT,
        }

    error_text = (
        f"Tool '{tool_name}' is not configured. Missing: " + "; ".join(parts) + "."
    )
    return {
        "success": False,
        "error": error_text,
        "missing_config": env_list,                # primary list the agent uses
        "missing_env": env_list,                   # explicit sub-categories
        "missing_binaries": bin_list,
        "missing_infra": infra_list,
        "tool_name": tool_name,
        "user_action_required": _USER_ACTION_PROMPT,
    }


def check_env_vars(env_names: Iterable[str]) -> List[str]:
    """Return the subset of env_names that are unset or empty.

    Pure helper so handlers can call::

        missing = check_env_vars(["ELEVENLABS_API_KEY", "MISTRAL_API_KEY"])
        if missing:
            return missing_config_response("tts", missing_env=missing)

    Returns:
        Sorted list of env var names whose value is "" or None.
    """
    missing: List[str] = []
    for name in env_names or []:
        if not os.environ.get(name):
            missing.append(name)
    return sorted(missing)


def check_binaries(binary_names: Iterable[str]) -> List[str]:
    """Return the subset of binary names that are not on $PATH.

    Uses ``shutil.which`` which is cross-platform (returns None on Windows
    for PATH lookups if the binary has no extension; we accept that).
    """
    import shutil
    missing: List[str] = []
    for name in binary_names or []:
        if not shutil.which(name):
            missing.append(name)
    return missing
