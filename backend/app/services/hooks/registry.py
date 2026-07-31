"""Built-in safety hooks — shipped defaults registered at startup.

These complement the DB-backed ``HookRule`` rules (editable via /api/hooks).
Built-in hooks are ``HookConfig`` instances defined in code so they ship with
the application and don't require configuration to take effect.

Design notes:
  - The active built-in (``builtin_delete_guard``) is scoped via ``matcher``
    to ``delete_*`` tools only — destructive operations are rare, so the
    one extra LLM gate call is acceptable and high-value. It uses
    ``block_on_failure=True`` so a FAIL verdict denies the call.
  - ``builtin_secret_scan`` is disabled by default (opt-in) to avoid adding
    LLM latency to every post-tool result; an admin enables it via the
    registry flag or by creating an equivalent DB rule.
"""
from __future__ import annotations

from app.services.hooks import HookConfig, HookEvent, HookType

# A PRE_TOOL_USE prompt hook that gates destructive delete operations.
# Fires only on tools whose name matches "delete_*" (fnmatch). The LLM is
# asked to FAIL the call if it looks unsafe (no/invalid ID, system resource,
# bulk destroy without explicit confirmation).
builtin_delete_guard = HookConfig(
    id="builtin_delete_guard",
    name="Delete Safety Guard",
    event=HookEvent.PRE_TOOL_USE.value,
    type=HookType.PROMPT.value,
    prompt=(
        "A tool call is about to DELETE a resource.\n"
        "Arguments: $ARGUMENTS\n\n"
        "Respond PASS if the call targets a specific, valid resource ID and "
        "is a normal user-initiated deletion. Respond FAIL if the arguments "
        "are empty, target a system/built-in resource, attempt bulk deletion, "
        "or look destructive without an explicit target ID. "
        "Reply with exactly 'PASS' or 'FAIL' and a one-line reason."
    ),
    timeout=15,
    priority=10,
    matcher="delete_*",
    block_on_failure=True,
    enabled=True,
)

# A POST_TOOL_USE prompt hook that detects secrets/API keys in tool output.
# Disabled by default to avoid per-call LLM latency; enable to audit output.
builtin_secret_scan = HookConfig(
    id="builtin_secret_scan",
    name="Secret Scan (post-tool)",
    event=HookEvent.POST_TOOL_USE.value,
    type=HookType.PROMPT.value,
    prompt=(
        "Inspect the following tool result for leaked secrets, API keys, or "
        "tokens.\nResult: $ARGUMENTS\n\n"
        "Respond PASS if no secrets are present. Respond FAIL and name the "
        "suspected secret type if any are found. "
        "Reply with exactly 'PASS' or 'FAIL' and a one-line reason."
    ),
    timeout=10,
    priority=0,
    matcher="*",
    block_on_failure=False,
    enabled=False,
)

#: All built-in hooks registered at startup (before DB rules).
BUILTIN_HOOKS: list[HookConfig] = [
    builtin_delete_guard,
    builtin_secret_scan,
]
