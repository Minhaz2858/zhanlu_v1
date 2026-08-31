"""Tool execution functions for agent runtime.

Each function corresponds to a tool definition in agent_prompts.py.
Functions receive the tool arguments and a database session, execute
the operation, and return a result dict.

execute_tool() is async — sync handlers are wrapped with asyncio.to_thread()
for backward compatibility. New tools register async handlers in the
ToolRegistry and are dispatched transparently.
"""

import asyncio
import hashlib
import json
import logging
import re
import time as _time
from sqlalchemy.orm import Session

from app.config import settings
from app.models.agent_app import AgentApp
from app.models.automation_task import AutomationTask
from app.models.knowledge_base import KnowledgeBase
from app.services.tracing import get_tracer as _get_tracer
from app.models.tool import Tool
from app.models.market_agent import MarketAgent

logger = logging.getLogger(__name__)

# UUID-shaped string matcher used to detect LLM-emitted placeholder values
# (e.g. "TOOL_CONTEXT.project_id") passed in tool args. The create_automation
# path observed on 2026-08-25 that LLMs will happily emit such placeholders
# verbatim, bypassing naive `args.get(...) or TOOL_CONTEXT.get(...)` fallbacks
# and triggering FK violations downstream. See _create_automation, lines below.
_UUID_SHAPE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value) -> bool:
    """True when ``value`` is a UUID-shaped string (36 chars, 4 dashes)."""
    if not isinstance(value, str):
        return False
    return bool(_UUID_SHAPE.match(value.strip()))


def _looks_like_llm_placeholder(value) -> bool:
    """True when ``value`` is a truthy-but-placeholder string the LLM might emit.

    Examples caught: ``"TOOL_CONTEXT.project_id"``, ``"$org_id"``, ``"<id>"``,
    ``"None"``, ``"undefined"``. Used to override the LLM's passed arg with the
    runtime-injected TOOL_CONTEXT value before the row hits the FK constraint.
    """
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    lower = s.lower()
    if lower in {"null", "none", "undefined", "n/a", "na", "nan"}:
        return True
    # Dotted identifiers (TOOL_CONTEXT.x), template tokens ($x, %x%, {{x}}),
    # or angle-bracket placeholders (<x>) are uniformly unsafe to persist.
    if "." in s:
        return True
    if s.startswith("$") or s.endswith("$"):
        return True
    if "{{" in s or "}}" in s or "%" in s:
        return True
    if s.startswith("<") or s.endswith(">"):
        return True
    return False

# System meta-agents whose tool calls never pause for user approval.
# These are the "builder"/"agent-of-agents" roles that orchestrate work
# (creating new agents, skills, automations). Pausing them on every
# create_* call would make them useless — they are *expected* to write
# to the AgentApp / Tool / AutomationTask tables as part of their job.
# The "real" user agent is the one that *uses* the result of those
# create_* calls; the user is the one who clicks "Build" in the UI, so
# the implicit consent is already in place.
SYSTEM_META_AGENTS = frozenset({
    "agent_builder",
    "skill_agent",
    "automation_agent",
    "general_assistant",
    "power_user",
})

# Context dict passed to tool handlers that need extra runtime info
# (conversation_id, agent_app_id, etc.). Populated by agents.py before
# calling execute_tool.
TOOL_CONTEXT: dict = {}


# Reverse alias map: dotted display names -> canonical registry names.
# The source of truth for the dotted display names is TOOL_DISPLAY_NAMES
# in app/routers/agents.py. When the LLM hallucinates the dotted form
# (e.g. "skills.hub" instead of "skills_hub"), execute_tool() resolves
# it to the canonical underscore name before dispatching.
#
# This is the inverse of TOOL_DISPLAY_NAMES; keep them in sync when adding
# new tools with dotted display names.
#
# IMPORTANT: A pair missing from this map will cause execute_tool() to
# return 'Unknown tool: <dotted>' whenever the LLM hallucinates the new
# dotted form. The alias-map-only tests in
# tests/test_tool_alias_resolution.py do not detect missing pairs — you
# must add a matching entry to EXPECTED_ALIAS_PAIRS in that test file too.
TOOL_NAME_ALIASES: dict[str, str] = {
    "skills.hub": "skills_hub",
    "skills.sync": "skills_sync",
    "skills.guard": "skills_guard",
    "skill.provenance": "skill_provenance",
    "skill.usage": "skill_usage",
    "mcp.oauth": "mcp_oauth",
    "mcp.oauth_manager": "mcp_oauth_manager",
    "process_registry.list": "process_registry_list",
    "process_registry.tail": "process_registry_tail",
    "process_registry.kill": "process_registry_kill",
}


def _is_arg_fixable_failure(result: dict) -> bool:
    """Heuristic: is a permanent tool failure plausibly fixable by changing args?

    Permission denials, hook blocks, and unknown-tool errors are structural —
    no argument tweak will satisfy them, so we skip the LLM reformulation
    call (saves a round-trip and avoids suggesting nonsense args).
    """
    err = (result.get("error") or "").lower()
    for marker in ("permission denied", "blocked by hook", "unknown tool"):
        if marker in err:
            return False
    return True


class _ToolResultFailure(Exception):
    """Wraps a tool-handler failure dict as an exception.

    ``run_tool_with_reliability`` uses an exception-based retry loop, but
    zhanlu's tool handlers RETURN failure dicts (``{"success": False, ...}``)
    rather than raising. This wrapper bridges the two: ``call_fn`` raises a
    ``_ToolResultFailure`` when the handler returns a non-success dict, so
    the reliability layer's retry/reformulate machinery can act on it. The
    original dict is carried on ``.result`` so it can be recovered after
    retries are exhausted.
    """

    def __init__(self, result: dict):
        self.result = result
        super().__init__(result.get("error", "tool call failed"))


async def _execute_with_reliability(
    tool_name: str,
    arguments: dict,
    handler,
    db: Session,
    user_id: str | None,
    context: dict | None,
    *,
    use_context: bool,
) -> dict:
    """Unified tool dispatch: upfront schema validation + run_tool_with_reliability.

    Replaces the retired ``_execute_with_self_heal``. The reliability layer
    (``run_tool_with_reliability``) owns retry+backoff, one-shot LLM arg-
    reformulation (via the ``llm_repair`` callback, migrated from the old
    self-heal), output verification, and the loop guard. The inline self-heal
    is retired — its ``reformulate_tool_args`` logic lives in ``llm_repair``.

    Upfront JSON-schema validation runs first: a malformed call is rejected
    immediately (no handler call, no LLM round-trip) as a ``permanent`` failure.

    Never raises — failures are encoded in the returned dict. Degrades
    gracefully: if the reliability layer itself raises, falls back to one
    direct handler invocation so a run is never broken by infra code.
    """
    from app.services.reliability import (
        run_tool_with_reliability, LoopState, get_conversation_loop_state,
    )
    from app.services.tool_arg_validator import get_tool_schema, validate_tool_args
    from app.services.tool_retry import reformulate_tool_args, is_retryable as _is_exc_retryable
    from app.config import settings

    # ── 1. Upfront schema validation ──
    schema = get_tool_schema(tool_name)
    if schema is not None:
        verr = validate_tool_args(arguments, schema)
        if verr is not None:
            logger.info("Tool '%s' rejected by upfront validation: %s", tool_name, verr)
            return {
                "success": False,
                "error": f"Invalid arguments: {verr}",
                "failure_kind": "permanent",
            }

    # ── 2. Build the reliability wrapper ──
    ctx = context or {}
    last_failure: dict | None = None

    async def call_fn(current_args: dict) -> dict:
        """Invoke the handler directly (NO inner retry — the reliability
        layer owns retry). Convert failure dicts to exceptions so the
        reliability retry/reformulate loop can act on them."""
        nonlocal last_failure
        if asyncio.iscoroutinefunction(handler):
            raw = await handler(current_args, db, user_id, context=ctx)
        else:
            raw = await asyncio.to_thread(handler, current_args, db, user_id)
        if isinstance(raw, dict) and not raw.get("success") and not raw.get("requires_approval"):
            last_failure = raw
            raise _ToolResultFailure(raw)
        last_failure = None
        return raw

    async def llm_repair(tn: str, args: dict, error_str: str):
        """Migrated self-heal: skip structural (non-arg-fixable) failures,
        then ask the LLM to reformulate args. Returns corrected dict or None."""
        if not _is_arg_fixable_failure({"error": error_str}):
            return None
        if not getattr(settings, "TOOL_ARG_REFORMULATION_ENABLED", True):
            return None
        try:
            corrected = await reformulate_tool_args(tn, args, error_str)
        except Exception as e:
            logger.debug("llm_repair: reformulate_tool_args raised (non-fatal): %s", e)
            return None
        return corrected if corrected != args else None

    def is_retryable_fn(exc) -> bool:
        """Classify: result-dict failures via _is_retryable, real exceptions
        via tool_retry.is_retryable."""
        if isinstance(exc, _ToolResultFailure):
            return _is_retryable(exc.result)
        return _is_exc_retryable(exc)

    loop_state = get_conversation_loop_state()
    if loop_state is None:
        loop_state = LoopState()  # transient — no cross-call history (chat path)

    # ── 3. Run with reliability ──
    try:
        rr = await run_tool_with_reliability(
            tool_name, arguments,
            call_fn=call_fn,
            loop_state=loop_state,
            llm_repair=llm_repair,
            is_retryable=is_retryable_fn,
        )
    except Exception as e:
        # Graceful degradation: never break a run because the reliability
        # layer itself blew up. Fall back to one direct handler call.
        logger.warning(
            "run_tool_with_reliability failed for '%s' (falling back to direct): %s",
            tool_name, e,
        )
        try:
            if asyncio.iscoroutinefunction(handler):
                return await handler(arguments, db, user_id, context=ctx)
            return await asyncio.to_thread(handler, arguments, db, user_id)
        except Exception as e2:
            return {
                "success": False,
                "error": f"Tool '{tool_name}' failed: {e2}",
                "failure_kind": "permanent",
            }

    # ── 4. Unwrap the reliability result ──
    if rr.get("success"):
        inner = rr.get("result")
        if isinstance(inner, dict):
            if rr.get("reformulated"):
                inner["reformulated"] = True
            return inner
        return {"success": True, "result": inner}

    # Failure: recover the original handler failure dict if available.
    if last_failure is not None:
        failure = dict(last_failure)
    else:
        failure = {"success": False, "error": rr.get("error", "tool call failed")}
    if rr.get("reformulated"):
        failure["reformulated"] = True
    # Mark failure_kind so the outer execute_tool_with_retry layer doesn't
    # double-retry (it skips results that already carry failure_kind).
    if "failure_kind" not in failure:
        failure["failure_kind"] = "permanent"
    return failure


# ── Result-level retry (Phase B) ─────────────────────────────────────────
#
# ``_execute_with_reliability`` (replacing the retired ``_execute_with_self_heal``)
# wraps the handler with ``run_tool_with_reliability`` (retry + reformulation +
# loop guard). Many handlers instead RETURN failure dicts ({"success": False,
# "error": ...}) — those are bridged to exceptions inside ``call_fn`` so the
# reliability layer can retry them. The public ``execute_tool_with_retry``
# wrapper closes the result-dict gap: it classifies the RESULT dict via
# ``_is_retryable`` and retries with exponential backoff, then optionally asks
# the LLM to reformulate arguments once retries are exhausted.

_PERMANENT_ERROR_MARKERS = (
    "permission denied",
    "unknown tool",
    "not found",
    "already exists",
    "blocked by hook",
)
_TRANSIENT_ERROR_MARKERS = (
    "connect",
    "timeout",
    "timed out",
    "reset",
    "refused",
    "unavailable",
    "temporar",
)


def _is_retryable(result: dict) -> bool:
    """Classify a tool RESULT dict as retryable (transient) or permanent.

    - ``success: True`` and ``requires_approval: True`` are never retryable
      (approval is a pending user action, not a failure).
    - An explicit ``retryable: True`` flag (set by handlers that know the
      failure is transient, e.g. DB OperationalError) wins.
    - Known permanent markers (permission denied, unknown tool, not found,
      already exists) are never retryable.
    - Known transient markers (connection/timeout/reset/refused) are retryable.
    - Unknown errors default to retryable — they might be transient.
    """
    if result.get("success"):
        return False
    if result.get("requires_approval"):
        return False
    if result.get("retryable") is True:
        return True
    err = (result.get("error") or "").lower()
    for marker in _PERMANENT_ERROR_MARKERS:
        if marker in err:
            return False
    for marker in _TRANSIENT_ERROR_MARKERS:
        if marker in err:
            return True
    return True


async def _reformulate_tool_args(tool_name: str, arguments: dict, error: str) -> dict:
    """Module-level indirection over tool_retry.reformulate_tool_args.

    Kept separate so tests (and operators) can patch the reformulation step
    without touching the underlying LLM helper. Never raises.
    """
    from app.services.tool_retry import reformulate_tool_args

    try:
        return await reformulate_tool_args(tool_name, arguments, error)
    except Exception as e:  # noqa: BLE001 — reformulation is best-effort
        logger.debug("_reformulate_tool_args failed (non-fatal): %s", e)
        return arguments


async def execute_tool_with_retry(
    tool_name: str,
    arguments: dict,
    db: Session,
    user_id: str | None = None,
    context: dict | None = None,
    *,
    max_attempts: int | None = None,
    base_delay: float | None = None,
) -> dict:
    """Execute a tool with result-level retry + one-shot argument self-heal.

    Wraps :func:`execute_tool`. When the result is a FAILURE DICT classified
    as transient by :func:`_is_retryable`, retries with exponential backoff
    (``base_delay * 2**n``, capped at ``TOOL_RETRY_MAX_DELAY``). When retries
    are exhausted and the failure looks arg-fixable, asks the LLM for
    corrected arguments and re-executes exactly once.

    Results carrying ``failure_kind`` (emitted by the handler-level
    ``_execute_with_reliability`` after ITS retries are exhausted) are NOT
    retried again here — the two layers never double-retry the same call.

    Never raises; the last failure dict is returned on exhaustion.
    """
    from app.config import settings

    _tool_start = _time.monotonic()

    retries_left = max_attempts if max_attempts is not None else settings.TOOL_RETRY_MAX_ATTEMPTS
    delay = base_delay if base_delay is not None else settings.TOOL_RETRY_BASE_DELAY

    result = await execute_tool(tool_name, arguments, db, user_id, context=context)

    retries_done = 0
    while (
        not result.get("success")
        and not result.get("requires_approval")
        and "failure_kind" not in result
        and _is_retryable(result)
        and retries_done < retries_left
    ):
        sleep_s = min(delay * (2 ** retries_done), settings.TOOL_RETRY_MAX_DELAY)
        logger.info(
            "Tool '%s' transient failure dict (retry %d/%d) in %.1fs: %s",
            tool_name, retries_done + 1, retries_left, sleep_s,
            result.get("error", "?"),
        )
        await asyncio.sleep(sleep_s)
        retries_done += 1
        result = await execute_tool(tool_name, arguments, db, user_id, context=context)

    if (
        not result.get("success")
        and not result.get("requires_approval")
        and "failure_kind" not in result
        and settings.TOOL_REFORMULATE_MAX_ATTEMPTS > 0
        and getattr(settings, "TOOL_ARG_REFORMULATION_ENABLED", True)
        and _is_arg_fixable_failure(result)
    ):
        corrected = await _reformulate_tool_args(
            tool_name, arguments, result.get("error", "")
        )
        if corrected != arguments:
            logger.info(
                "Tool '%s' retrying with reformulated args (result-level)",
                tool_name,
            )
            healed = await execute_tool(tool_name, corrected, db, user_id, context=context)
            if healed.get("success"):
                healed["reformulated"] = True
            _get_tracer().record_tool_call(
                tool_name=tool_name,
                args_hash=hashlib.sha256(str(arguments).encode()).hexdigest()[:16],
                duration_ms=(_time.monotonic() - _tool_start) * 1000,
                success=bool(healed.get("success")),
                error=healed.get("error") if not healed.get("success") else None,
            )
            return healed

    _get_tracer().record_tool_call(
        tool_name=tool_name,
        args_hash=hashlib.sha256(str(arguments).encode()).hexdigest()[:16],
        duration_ms=(_time.monotonic() - _tool_start) * 1000,
        success=bool(result.get("success")),
        error=result.get("error") if not result.get("success") else None,
    )
    return result


async def execute_tool(
    tool_name: str,
    arguments: dict,
    db: Session,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Execute a tool call and return the result.

    Checks the ToolRegistry first (for new capability tools registered by
    tool_handlers). Falls back to the local _CRUD_DISPATCH dict (for existing
    CRUD handlers). Sync handlers are wrapped with asyncio.to_thread().

    Args:
        tool_name: The name of the tool to execute (e.g. "create_agent").
        arguments: The parsed JSON arguments from the LLM's tool_call.
        db: Database session.
        user_id: Optional user ID for created_by tracking.
        context: Optional runtime context (conversation_id, agent_app_id, etc.)

    Returns:
        A dict with at least {"success": bool} and the created/updated
        record's data (including "id" for create operations).
    """
    # Merge context into the global TOOL_CONTEXT for handlers that need it
    if context:
        TOOL_CONTEXT.clear()
        TOOL_CONTEXT.update(context)

    # Resolve dotted-name hallucinations to canonical underscore names
    # before any permission/registry lookups. See TOOL_NAME_ALIASES.
    tool_name = TOOL_NAME_ALIASES.get(tool_name, tool_name)

    # 0. Permission check — block tools that are not allowed, or pause for
    #    confirmation when requires_confirmation=True (default mode write tools).
    agent_name = context.get("agent_name") if context else None
    conversation_metadata = context.get("conversation_metadata") if context else None
    try:
        from app.services.permissions import check_permission
        perm_result = check_permission(
            tool_name, arguments, agent_name,
            conversation_metadata=conversation_metadata,
        )
        if not perm_result.allowed:
            logger.warning("Tool '%s' blocked by permission check: %s", tool_name, perm_result.reason)
            return {"success": False, "error": f"Permission denied: {perm_result.reason}"}
        # System meta-agents (agent_builder, skill_agent, automation_agent,
        # general_assistant, power_user) are orchestrators whose job is to
        # call create_*/update_* tools. Bypassing requires_confirmation for
        # them removes the per-call approval gate that previously made the
        # agent_builder unusable. The actual user consent is the click in
        # the UI that started the conversation.
        if perm_result.requires_confirmation and agent_name in SYSTEM_META_AGENTS:
            logger.info(
                "Tool '%s' bypassed requires_confirmation for system meta-agent '%s'",
                tool_name, agent_name,
            )
            # Fall through to actual tool execution below.
        elif perm_result.requires_confirmation:
            # Create an ApprovalRequest so the user can approve/reject via the
            # governance API. The chat loop will detect requires_approval and
            # pause, persisting resume state until the user responds.
            try:
                from app.services.governance.approval_service import ApprovalService
                approval_svc = ApprovalService(db)
                # Build a human-readable description of the action
                arg_preview = json.dumps(arguments, default=str)[:500]
                description = f"Tool '{tool_name}' (agent: {agent_name or 'unknown'}) — args: {arg_preview}"
                approval = approval_svc.create_request(
                    action_type="tool_call",
                    action_description=description,
                    risk_tier="medium",
                    context_json={
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "agent_name": agent_name,
                        "conversation_id": context.get("conversation_id") if context else None,
                        "agent_app_id": context.get("agent_app_id") if context else None,
                    },
                    ttl_hours=1,  # Short TTL for interactive chat
                )
                logger.info(
                    "Tool '%s' requires confirmation — created approval %s (agent=%s)",
                    tool_name, approval.id, agent_name,
                )
                return {
                    "success": False,
                    "requires_approval": True,
                    "approval_id": approval.id,
                    "reason": perm_result.reason,
                    "tool_name": tool_name,
                    "arguments": arguments,
                }
            except Exception as ae:
                # Fail-closed: if the approval gate itself is unavailable we
                # must NOT silently allow the tool — that would widen access
                # on an error path. Deny and surface the failure.
                logger.warning(
                    "Failed to create ApprovalRequest for tool '%s' (denying — fail-closed): %s",
                    tool_name, ae,
                )
                return {
                    "success": False,
                    "error": f"Approval gate unavailable — tool call denied (fail-closed): {ae}",
                    "tool_name": tool_name,
                }
    except Exception as e:
        # Fail-closed: an exception in the permission check itself must deny
        # the tool call, never widen access. See SP2 reliability-surface plan.
        logger.warning("Permission check failed for tool '%s' (denying — fail-closed): %s", tool_name, e)
        return {
            "success": False,
            "error": f"Permission check failed — tool call denied (fail-closed): {e}",
            "tool_name": tool_name,
        }

    # 0.5 Pre-tool hook execution
    try:
        from app.services.hooks import get_hook_executor, HookEvent
        hook_executor = get_hook_executor()
        hook_result = await hook_executor.execute(
            HookEvent.PRE_TOOL_USE,
            {"tool_name": tool_name, "arguments": arguments, "agent_name": agent_name},
        )
        if hook_result.blocked:
            logger.info("Tool '%s' blocked by pre_tool_use hook: %s", tool_name, hook_result.reason)
            return {"success": False, "error": f"Blocked by hook: {hook_result.reason}"}
    except Exception as e:
        logger.debug("Hook execution failed (non-fatal): %s", e)

    # 1. Check the registry first (new tools)
    from app.services.tool_registry import registry

    handler = registry.get_handler(tool_name)

    if handler is not None:
        result = await _execute_with_reliability(
            tool_name, arguments, handler, db, user_id, context,
            use_context=True,
        )
    else:
        # 2. Fall back to local CRUD dispatch (existing tools)
        crud_handler = _CRUD_DISPATCH.get(tool_name)
        if crud_handler is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        result = await _execute_with_reliability(
            tool_name, arguments, crud_handler, db, user_id, context,
            use_context=False,
        )

    # 3. Post-tool hook execution
    try:
        from app.services.hooks import get_hook_executor, HookEvent
        hook_executor = get_hook_executor()
        await hook_executor.execute(
            HookEvent.POST_TOOL_USE,
            {"tool_name": tool_name, "arguments": arguments, "result": result, "agent_name": agent_name},
        )
    except Exception as e:
        logger.debug("Post-tool hook failed (non-fatal): %s", e)

    return result


# ---------------------------------------------------------------------------
# Argument normalization helpers
# ---------------------------------------------------------------------------

def _resolve_update_id(args: dict, *keys: str) -> str | None:
    """Extract the target ID for an update_* tool call.

    LLMs (notably DeepSeek) sometimes nest the ID inside ``fields`` instead of
    placing it as a top-level sibling, because they read "Same field names as
    create_agent" in the schema description and reuse the flat create shape.
    This helper accepts the ID from any of the given keys, looking at the
    top level first and then falling back to inside ``fields`` (where it
    removes the key so the value is not applied twice by the update loop).

    Args:
        args: The raw tool arguments dict from the LLM.
        *keys: Candidate key names to look for, in priority order. The first
            key found wins. Use a short ``"id"`` alias last so plain ``id``
            is also accepted as a defensive fallback.

    Returns:
        The resolved ID string, or ``None`` if none of the keys are present.
    """
    if not isinstance(args, dict):
        return None
    for key in keys:
        value = args.get(key)
        if value:
            return value
    fields = args.get("fields")
    if isinstance(fields, dict):
        for key in keys:
            value = fields.get(key)
            if value:
                # Pop so the update loop doesn't try to apply the id as a
                # record field (it wouldn't be in allowed_fields anyway, but
                # removing it keeps intent explicit and prevents surprises
                # if allowed_fields is ever widened).
                fields.pop(key, None)
                return value
    return None


# ---------------------------------------------------------------------------
# prompt_tools normalization — ensure bound KB agents reference ask_data_agent
# ---------------------------------------------------------------------------

_DB_TOOLS_BLOCK_MARKER_START = "<!-- DB_TOOLS_AUTO_INJECTED -->"
_DB_TOOLS_BLOCK_MARKER_END = "<!-- /DB_TOOLS_AUTO_INJECTED -->"

_DB_TOOLS_BLOCK = (
    f"{_DB_TOOLS_BLOCK_MARKER_START}\n"
    "MANDATORY DATABASE ACCESS — call the tool whose function name is "
    "exactly `ask_data_agent` (case-sensitive). This is the ONLY way to "
    "query the bound data sources. You cannot introspect tables, run "
    "queries, or fetch rows without invoking this tool.\n\n"
    "Function signature (use the exact `name` field when calling):\n"
    "```\n"
    "ask_data_agent(\n"
    "    question: str,                # required — the natural-language question\n"
    "    data_source_id: str = None,   # optional — id of a bound source\n"
    "    max_iterations: int = 6,      # optional — cap on subagent rounds (max 10)\n"
    ")\n"
    "```\n\n"
    "Workflow: call `ask_data_agent` → read the returned payload "
    "(answer, rows, sql, source_id, citations) → compose your reply.\n"
    "If the payload indicates an error or empty rows, say so — do not "
    "invent data.\n\n"
    "Anti-patterns: do NOT pretend to query the database or narrate "
    "workflow steps without actually calling `ask_data_agent`. Do NOT "
    "call list_data_sources / describe_schema / execute_query / "
    "answer_from_database — those are internal to the Data Agent.\n"
    f"{_DB_TOOLS_BLOCK_MARKER_END}"
)


def _normalize_prompt_tools_for_bound_kbs(
    prompt_tools: str | None,
    knowledge_bases: list | None,
) -> str | None:
    """Normalize the L4 prompt_tools so bound-KB agents always reference
    `ask_data_agent` by its literal function name.

    When `knowledge_bases` is non-empty, append the mandatory DB-tools
    block (with unique markers for safe removal). When `knowledge_bases`
    is empty, strip any previously-injected block.

    Idempotent: checks for the marker before injecting; checks for
    `ask_data_agent` in the text to avoid double-injection on agents
    that were patched by the migration script (which lacks markers).

    Args:
        prompt_tools: The L4 Tools prompt text (may be None or "").
        knowledge_bases: The list of bound KB IDs.

    Returns:
        The normalized prompt_tools string (or None if input was None).
    """
    if prompt_tools is None:
        return None

    text = prompt_tools

    # Strip any previously auto-injected block (marker-based)
    if _DB_TOOLS_BLOCK_MARKER_START in text:
        start_idx = text.index(_DB_TOOLS_BLOCK_MARKER_START)
        end_idx = text.index(_DB_TOOLS_BLOCK_MARKER_END) + len(_DB_TOOLS_BLOCK_MARKER_END)
        text = text[:start_idx].rstrip() + "\n\n" + text[end_idx:].lstrip("\n")
        text = text.strip()

    kbs = knowledge_bases or []
    if isinstance(kbs, list) and len(kbs) > 0:
        # Inject the block if `ask_data_agent` is not already mentioned
        # (covers both marker-based and migration-script patches)
        if "ask_data_agent" not in text:
            text = text.rstrip() + "\n\n" + _DB_TOOLS_BLOCK

    return text


def normalize_all_agent_prompts(db: Session) -> int:
    """Normalize prompt_tools for all agents with bound KnowledgeBases.

    This is the startup-time migration that ensures agents created BEFORE
    the _normalize_prompt_tools_for_bound_kbs fix get their prompts updated
    to reference `ask_data_agent` by its literal function name. Without this,
    stale agents reference the "Database Query" display name and the LLM
    hallucinates data instead of calling the tool.

    Idempotent: agents whose prompt_tools already mentions ask_data_agent
    are not modified.

    Args:
        db: A SQLAlchemy session.

    Returns:
        The number of agents whose prompt_tools was modified.
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        agents = (
            db.query(AgentApp)
            .filter(AgentApp.is_deleted == False)  # noqa: E712
            .all()
        )
    except Exception as e:
        logger.warning("Failed to query agents for prompt normalization: %s", e)
        return 0

    patched = 0
    for agent in agents:
        kbs = agent.knowledge_bases or []
        if not (isinstance(kbs, list) and len(kbs) > 0):
            continue

        original = agent.prompt_tools or ""
        normalized = _normalize_prompt_tools_for_bound_kbs(original, kbs)
        if normalized != original:
            agent.prompt_tools = normalized
            db.add(agent)
            patched += 1
            logger.info(
                "Startup normalization: updated prompt_tools for agent '%s' (id=%s)",
                agent.name, agent.id,
            )

    if patched > 0:
        try:
            db.commit()
            logger.info("Startup normalization: patched %d agent(s)", patched)
        except Exception as e:
            logger.warning("Startup normalization commit failed: %s", e)
            db.rollback()
            return 0

    return patched


_MISSING = object()  # sentinel for "key not in args dict"

_FIVE_LAYER_TEMPLATES = {
    "prompt_identity": (
        "## Identity\n"
        "You are a domain expert in {domain}. You serve {user}, "
        "with the mission to {mission}. Success means {success_criteria}."
    ),
    "prompt_boundary": (
        "## Boundary\n"
        "Allowed: read-only data analysis and concise recommendations within your domain. "
        "Forbidden: destructive operations, sending messages on the user's behalf, "
        "or modifying production state. When uncertain, ask for human confirmation "
        "and label the uncertainty explicitly in the output."
    ),
    "prompt_reasoning": (
        "## Process\n"
        "Analyze the request → Plan the smallest viable approach → Execute the "
        "relevant tools → Verify the result → Respond. Keep private reasoning "
        "internal; surface only the concise rationale the user needs."
    ),
    "prompt_tools": (
        "## Tools\n"
        "Select the narrowest tool that answers the question. "
        "For data, database, or knowledge-base questions use `ask_data_agent`. "
        "For current or external facts use `web_search` or `web_extract`. "
        "Verify each tool's return before reporting. On failure, explain briefly "
        "and fall back rather than inventing."
    ),
    "prompt_output": (
        "## Output\n"
        "Lead with the direct answer. Use concise Markdown with sections, "
        "bullets, and tables. Cite sources (URL, table name, or memory key) "
        "when grounding a claim. Label uncertainty explicitly."
    ),
}


def _derive_domain(description: str) -> str:
    """Tiny keyword-based domain hint for the L1 template."""
    if not description:
        return "the user's stated domain"
    text = description.lower()
    if any(k in text for k in ("sales", "deal", "pipeline", "crm")):
        return "sales analytics"
    if any(k in text for k in ("defect", "spc", "quality", "manufacturing")):
        return "quality / SPC analysis"
    if any(k in text for k in ("data", "database", "knowledge base", "sql")):
        return "data analysis"
    if any(k in text for k in ("customer", "support", "ticket")):
        return "customer support"
    return "the user's stated domain"


def _autofill_missing_fields(args: dict, description: str) -> None:
    """Fill only fields the LLM omitted. Mutates args in place. Never overrides an
    explicit LLM value. Logs one INFO line per auto-filled field so operators can
    see what was derived."""
    # --- Five-layer constitutional prompt ---
    domain = _derive_domain(description)
    mission = (description or "").strip().split(".")[0] or "the user's stated goal"
    for field, template in _FIVE_LAYER_TEMPLATES.items():
        val = args.get(field, _MISSING)
        if val is _MISSING or not str(val or "").strip():
            args[field] = template.format(
                domain=domain, user="the requester",
                mission=mission, success_criteria="the user's stated goal is met",
            )
            logger.info("auto-filled %s from description", field)

    # --- Capabilities ---
    caps = args.get("capabilities", _MISSING)
    if caps is _MISSING or not caps:
        candidates = [
            p.strip() for p in re.split(r"[,;]", description or "")
            if 2 <= len(p.strip()) <= 40
        ]
        if candidates:
            deduped = list(dict.fromkeys(candidates))[:5]
            args["capabilities"] = deduped
            logger.info("auto-filled capabilities from description: %r", deduped)

    # --- Access flags (only when LLM omitted the key entirely) ---
    for key, default in (("data_read", False), ("data_write", False),
                         ("human_fallback", True), ("trace_enabled", True)):
        if args.get(key, _MISSING) is _MISSING:
            args[key] = default
            logger.info("auto-filled %s=%s (default)", key, default)


def _autofill_harness_profile(args: dict, description: str) -> None:
    """Fill the 7 Layer-3 Harness Agent fields from existing agent config.

    Reads the already-populated 5-layer prompt, skills, capabilities, and
    access flags to derive sensible defaults for manifest_json, data_bindings,
    skill_bindings, memory_scope, policy_profile, output_contract, and
    evaluation_profile.

    Only fills fields the LLM omitted (truly missing or empty); never
    overrides an explicit value. Mirrors the same _MISSING sentinel pattern
    used by _autofill_missing_fields.

    Mutates args in place. Logs one INFO line per auto-filled field.
    """
    description = description or ""
    mission = description.strip().split(".")[0] or "Assist the user with their task"
    name = args.get("name", "Agent")
    risk_tier = (
        "high" if args.get("data_write") is True
        else "medium" if args.get("data_read") is True
        else "low"
    )

    # --- manifest_json ---
    if not args.get("manifest_json"):
        args["manifest_json"] = {
            "agent_name": name,
            "version": "1.0.0",
            "mission": mission,
            "task_scope": ["analysis", "recommendation", "reporting"],
            "boundaries": {
                "allowed": ["read_data", "web_search", "reasoning"],
                "forbidden": ["destructive_ops", "impersonation", "production_mutation"],
            },
            "risk_tier": risk_tier,
            "created_by": "agent_builder",
        }
        logger.info("auto-filled manifest_json for agent=%r", name)

    # --- data_bindings ---
    if not args.get("data_bindings"):
        kbs = args.get("knowledge_bases", [])
        args["data_bindings"] = (
            [{"knowledge_base_id": kb, "access_mode": "read_only"} for kb in kbs]
            if kbs else []
        )
        logger.info("auto-filled data_bindings (%d kb(s))", len(args["data_bindings"]))

    # --- skill_bindings ---
    if not args.get("skill_bindings"):
        skills = args.get("skills", [])
        args["skill_bindings"] = [
            {"skill_name": s, "version": "latest", "allowed": True}
            for s in (skills or [])
        ]
        logger.info("auto-filled skill_bindings (%d skill(s))", len(args["skill_bindings"]))

    # --- memory_scope ---
    if not args.get("memory_scope"):
        args["memory_scope"] = "app_shared"
        logger.info("auto-filled memory_scope=app_shared")

    # --- policy_profile ---
    if not args.get("policy_profile"):
        args["policy_profile"] = {
            "risk_tier": risk_tier,
            "requires_confirmation": args.get("human_fallback", True),
            "max_concurrent_calls": 3,
            "rate_limit_per_minute": 30,
            "allowed_domains": [],
            "retention_days": 30,
        }
        logger.info("auto-filled policy_profile risk_tier=%s", risk_tier)

    # --- output_contract ---
    if not args.get("output_contract"):
        args["output_contract"] = {
            "allowed_artifact_types": ["markdown", "json", "csv", "text"],
            "must_include_sources": True,
            "citation_format": "inline",
            "max_response_length": 8192,
        }
        logger.info("auto-filled output_contract")

    # --- evaluation_profile ---
    if not args.get("evaluation_profile"):
        args["evaluation_profile"] = {
            "test_cases": [],
            "trace_replay_enabled": True,
            "grounding_checks": ["source_citation", "hallucination_check"],
            "expected_accuracy": 0.85,
        }
        logger.info("auto-filled evaluation_profile")


def _create_agent(args: dict, db: Session, user_id: str | None) -> dict:
    """Create a new AgentApp record."""
    # Auto-derive tool_config from skills if the LLM didn't provide one.
    # This ensures newly created agents have working tools from the start,
    # instead of getting an empty tool list at chat time.
    tool_config = args.get("tool_config")
    if not tool_config:
        from app.services.tool_registry import (
            resolve_tools_from_skills,
            DEFAULT_USER_AGENT_TOOLS,
        )
        skill_names = args.get("skills", [])
        mapped = resolve_tools_from_skills(skill_names)
        # Merge with baseline defaults, deduplicating
        enabled = list(dict.fromkeys(mapped + DEFAULT_USER_AGENT_TOOLS))
        tool_config = {"enabled_tools": enabled}
        # Surface the fallback so operators and the agent-builder UI can
        # see which baseline tools were pre-populated for this agent.
        # Pinned by tests/test_user_agent_tool_fallback.py.
        agent_name = args.get("name", "Untitled Agent")
        logger.debug(
            "create_agent fallback: agent=%r (no tool_config from LLM) "
            "received baseline tools enabled=%r from skills=%r",
            agent_name, enabled, skill_names,
        )

    # Safety net: auto-fill any fields the LLM omitted (five-layer prompt,
    # capabilities, access flags) from the user's description. Only acts on
    # truly missing fields; never overrides an explicit LLM value.
    _autofill_missing_fields(args, args.get("description", ""))

    # Layer 3 Enterprise Harness Agent profile: auto-fill the 7 governance
    # fields (manifest, data_bindings, skill_bindings, memory_scope,
    # policy_profile, output_contract, evaluation_profile) from the
    # already-populated 5-layer prompt, skills, and access flags.
    _autofill_harness_profile(args, args.get("description", ""))

    agent = AgentApp(
        name=args.get("name", "Untitled Agent"),
        description=args.get("description", ""),
        project=args.get("project", "global"),
        capabilities=args.get("capabilities", []),
        model=args.get("model", "automatic"),
        agent_type=args.get("agent_type", "sequential"),
        prompt_identity=args.get("prompt_identity", ""),
        prompt_boundary=args.get("prompt_boundary", ""),
        prompt_reasoning=args.get("prompt_reasoning", ""),
        prompt_tools=_normalize_prompt_tools_for_bound_kbs(
            args.get("prompt_tools", ""),
            args.get("knowledge_bases", []),
        ),
        prompt_output=args.get("prompt_output", ""),
        skills=args.get("skills", []),
        knowledge_bases=args.get("knowledge_bases", []),
        topology=args.get("topology", "standalone"),
        sub_agents=args.get("sub_agents", []),
        max_call_count=args.get("max_call_count", 50),
        max_retries=args.get("max_retries", 3),
        max_iterations=args.get("max_iterations", 5),
        data_read=args.get("data_read", False),
        data_write=args.get("data_write", False),
        human_fallback=args.get("human_fallback", True),
        trace_enabled=args.get("trace_enabled", True),
        log_level=args.get("log_level", "info"),
        temperature=args.get("temperature", 0.7),
        top_p=args.get("top_p", 1.0),
        max_tokens=args.get("max_tokens", 4096),
        status=args.get("status", "active"),
        tool_config=tool_config,
        # Layer 3 Enterprise Harness Agent fields
        manifest_json=args.get("manifest_json"),
        data_bindings=args.get("data_bindings"),
        skill_bindings=args.get("skill_bindings"),
        memory_scope=args.get("memory_scope", "app_shared"),
        policy_profile=args.get("policy_profile"),
        output_contract=args.get("output_contract"),
        evaluation_profile=args.get("evaluation_profile"),
        created_by_id=user_id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    result = agent.to_dict()
    result["success"] = True
    return result


def _update_agent(args: dict, db: Session, user_id: str | None) -> dict:
    """Update an existing AgentApp record."""
    # Accept agent_id from top level OR from inside fields. Some LLMs nest the
    # id inside fields (treating the update shape like create_agent), so this
    # is a defense-in-depth check on top of the schema description.
    agent_id = _resolve_update_id(args, "agent_id", "id", "app_id")
    if not agent_id:
        logger.warning(
            "update_agent called without agent_id (args keys=%s). "
            "Reminder: agent_id MUST be a top-level sibling of fields.",
            list(args.keys()) if isinstance(args, dict) else None,
        )
        return {
            "success": False,
            "error": (
                "agent_id is required and must be a top-level sibling of "
                "'fields' (not nested inside fields)."
            ),
        }

    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        return {"success": False, "error": "'fields' must be an object"}

    agent = db.query(AgentApp).filter(
        AgentApp.id == agent_id,
        AgentApp.is_deleted == False,
    ).first()

    if not agent:
        return {"success": False, "error": f"Agent not found: {agent_id}"}

    # Allowed fields for update
    allowed_fields = {
        "name", "description", "project", "capabilities", "model", "agent_type",
        "prompt_identity", "prompt_boundary", "prompt_reasoning", "prompt_tools",
        "prompt_output", "skills", "knowledge_bases", "topology", "sub_agents",
        "max_call_count", "max_retries", "max_iterations",
        "data_read", "data_write", "human_fallback",
        "trace_enabled", "log_level", "temperature", "top_p", "max_tokens",
        "status", "tool_config",
        # Layer 3 Enterprise Harness Agent fields
        "manifest_json", "data_bindings", "skill_bindings", "memory_scope",
        "policy_profile", "output_contract", "evaluation_profile",
    }

    for key, value in fields.items():
        if key in allowed_fields:
            setattr(agent, key, value)

    # If skills were updated but tool_config wasn't, re-derive tool_config
    # from the new skills so the agent's tool list stays in sync.
    if "skills" in fields and "tool_config" not in fields:
        from app.services.tool_registry import (
            resolve_tools_from_skills,
            DEFAULT_USER_AGENT_TOOLS,
        )
        mapped = resolve_tools_from_skills(agent.skills or [])
        enabled = list(dict.fromkeys(mapped + DEFAULT_USER_AGENT_TOOLS))
        agent.tool_config = {"enabled_tools": enabled}
        # Mirror the create_agent fallback log so operators see re-derivations
        # too. Format matches the create_agent log for easy grep / aggregation.
        logger.debug(
            "update_agent fallback: agent=%r (skills changed, no tool_config) "
            "received baseline tools enabled=%r from skills=%r",
            getattr(agent, "name", None), enabled, agent.skills or [],
        )

    # Normalize prompt_tools when knowledge_bases changes (or is set).
    # This ensures every agent with bound DBs always references
    # `ask_data_agent` by its literal function name.
    agent.prompt_tools = _normalize_prompt_tools_for_bound_kbs(
        agent.prompt_tools,
        agent.knowledge_bases,
    )

    from datetime import datetime, timezone
    agent.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(agent)
    result = agent.to_dict()
    result["success"] = True
    return result


def _list_tools(args: dict, db: Session, user_id: str | None) -> dict:
    """List all available tools/skills from the Tool library."""
    tools = db.query(Tool).filter(Tool.is_deleted == False).all()
    return {
        "success": True,
        "tools": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description or "",
                "trigger": getattr(t, "trigger", None) or "",
                "category": getattr(t, "category", None) or "",
                "kind": getattr(t, "kind", None) or "",
                "status": getattr(t, "status", None) or "",
            }
            for t in tools
        ],
    }


def _list_market_agents(args: dict, db: Session, user_id: str | None) -> dict:
    """List marketplace agents for reference or cloning."""
    agents = db.query(MarketAgent).filter(MarketAgent.is_deleted == False).all()
    return {
        "success": True,
        "market_agents": [
            {
                "id": a.id,
                "name": a.name,
                "description": a.description or "",
                "category": getattr(a, "category", None) or "",
                "capabilities": getattr(a, "capabilities", None) or [],
                "rating": getattr(a, "rating", None),
                "subscribers": getattr(a, "subscribers", None),
            }
            for a in agents
        ],
    }


# ---------------------------------------------------------------------------
# Skill Agent tools
# ---------------------------------------------------------------------------

def _create_skill(args: dict, db: Session, user_id: str | None) -> dict:
    """Create a new Tool (skill) record — writes to DB AND filesystem."""
    name = args.get("name", "Untitled Skill")
    description = args.get("description", "")
    trigger = args.get("trigger", "")
    category = args.get("category", "custom")
    skill_md = args.get("skill_md", "")

    tool = Tool(
        name=name,
        description=description,
        trigger=trigger,
        category=category,
        skill_md=skill_md,
        kind=args.get("kind", "system_skill"),
        source=args.get("source", "custom"),
        publisher=args.get("publisher", "user"),
        enabled=args.get("enabled", True),
        status=args.get("status", "active"),
        created_by_id=user_id,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)

    # Write-through to filesystem so SkillsRegistry picks it up immediately
    # FIX 2026-08-29: NEVER skip the FS write — a DB-only skill is invisible
    # to list_skills (the chat catalog), load_skill_body and the Skill tool,
    # so the user sees "skill created" but can never use it. When skill_md is
    # empty, write a minimal body so the skill still registers.
    try:
        from app.services.skill_sync import write_skill_md, reload_skills_registry
        _body = skill_md or (
            f"# {name}\n\n## Overview\n{description or name}.\n\n"
            "## Instructions\n1. Follow the description above.\n"
        )
        write_skill_md(
            name=name,
            description=description,
            body=_body,
            category=category,
            trigger=trigger,
            author=args.get("publisher", "user"),
        )
        reload_skills_registry()
    except Exception as e:
        logger.warning("Skill filesystem write-through failed (non-fatal): %s", e)

    result = tool.to_dict()
    result["success"] = True
    return result


def _update_skill(args: dict, db: Session, user_id: str | None) -> dict:
    """Update an existing Tool (skill) record — updates DB AND filesystem."""
    # Accept skill_id from top level OR from inside fields.
    skill_id = _resolve_update_id(args, "skill_id", "id", "tool_id")
    if not skill_id:
        logger.warning(
            "update_skill called without skill_id (args keys=%s). "
            "Reminder: skill_id MUST be a top-level sibling of fields.",
            list(args.keys()) if isinstance(args, dict) else None,
        )
        return {
            "success": False,
            "error": (
                "skill_id is required and must be a top-level sibling of "
                "'fields' (not nested inside fields)."
            ),
        }

    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        return {"success": False, "error": "'fields' must be an object"}

    tool = db.query(Tool).filter(
        Tool.id == skill_id,
        Tool.is_deleted == False,
    ).first()

    if not tool:
        return {"success": False, "error": f"Skill not found: {skill_id}"}

    allowed_fields = {
        "name", "description", "trigger", "category", "skill_md",
        "kind", "source", "publisher", "enabled", "status",
    }

    for key, value in fields.items():
        if key in allowed_fields:
            setattr(tool, key, value)

    from datetime import datetime, timezone
    tool.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(tool)

    # Write-through to filesystem
    if tool.skill_md:
        try:
            from app.services.skill_sync import write_skill_md, reload_skills_registry
            write_skill_md(
                name=tool.name,
                description=tool.description or "",
                body=tool.skill_md,
                category=tool.category or "custom",
                trigger=tool.trigger or "",
                author=tool.publisher or "user",
            )
            reload_skills_registry()
        except Exception as e:
            logger.warning("Skill filesystem write-through failed (non-fatal): %s", e)

    result = tool.to_dict()
    result["success"] = True
    return result


def _search_skills(args: dict, db: Session, user_id: str | None) -> dict:
    """Search for existing skills across DB and filesystem.

    Lets the skill_agent discover existing skills before creating duplicates.
    """
    from app.services.skills_loader import unified_search
    query = args.get("query", "")
    limit = args.get("limit", 10)
    results = unified_search(query, limit=limit, db=db)
    return {
        "success": True,
        "query": query,
        "count": len(results),
        "skills": results,
    }


# ---------------------------------------------------------------------------
# Automation Agent tools
# ---------------------------------------------------------------------------

def _resolve_user_timezone(db, user_id):
    """Best-effort lookup of the user's preferred IANA timezone from
    UserSetting. Returns None on any issue (caller falls back to UTC)."""
    if not user_id:
        return None
    try:
        from app.models.user_setting import UserSetting
        s = db.query(UserSetting).filter(
            UserSetting.created_by_id == user_id
        ).first()
        tz = getattr(s, "timezone", None) if s else None
        return (tz or "").strip() or None
    except Exception:
        return None


def _normalize_skills(value) -> list | None:
    """Normalize a skills value into an ordered list of skill names (or None).

    Accepts a JSON array, a comma-separated string (the LLM sometimes emits a
    string for a list-typed tool argument), or None/garbage. Returns None for
    empty input so the column stays NULL (matching the agent_apps.skills
    convention) rather than persisting a spurious ``[]``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        skills = [s.strip() for s in value.split(",") if s.strip()]
    elif isinstance(value, (list, tuple)):
        skills = [s.strip() for s in value if isinstance(s, str) and s.strip()]
    else:
        skills = []
    return skills or None


def _resolve_task_executor_agent(db: Session, args: dict) -> str:
    """Resolve the agent_id for a new AutomationTask.

    Priority:
    1. A user-supplied ``agent_id`` that is valid (exists, same org/app,
       not deleted) — honored so power users can pin a custom executor.
    2. The hidden automation_runtime_agent for the task's (org_id, app_id).
    The chat agent (TOOL_CONTEXT['agent_app_id']) is NEVER used as the
    default executor — it creates the task but does not run it.
    """
    from app.services.automation_runtime import ensure_automation_runtime_agent
    from app.models.agent_app import AgentApp as _AA

    org_id = args.get("org_id") or TOOL_CONTEXT.get("org_id") or "default-org"
    app_id = args.get("app_id") or TOOL_CONTEXT.get("app_id") or "default-app"

    supplied = args.get("agent_id")
    if supplied:
        valid = db.query(_AA).filter(
            _AA.id == supplied,
            _AA.is_deleted == False,  # noqa: E712
        ).first()
        if valid is not None:
            return supplied
        logger.warning(
            "_resolve_task_executor_agent: supplied agent_id=%s not found; "
            "falling back to runtime agent", supplied,
        )

    runtime = ensure_automation_runtime_agent(db, org_id, app_id)
    return runtime.id


def _create_automation(args: dict, db: Session, user_id: str | None) -> dict:
    """Create a new AutomationTask record.

    Accepts a free-form ``schedule`` (cron OR natural language) and computes
    ``next_run_at`` so the dispatcher can fire it without further work. If
    the schedule can't be parsed the task is created as paused and the
    caller is told to fix the schedule string.

    ``session_id`` linkage: the LLM does not know which chat session the
    user is in, so it cannot pass ``session_id`` in the tool-call args.
    We fall back to ``TOOL_CONTEXT["chat_session_id"]`` which the agent
    runtime injects before each tool call. Without this fallback, the
    Scheduled button would never appear in the chat header (the frontend
    detects automation per-session via this FK).
    """
    schedule = (args.get("schedule") or "manual").strip()
    # Resolve the timezone the cron should be interpreted in: explicit arg
    # wins, else the user's saved preference, else UTC. Fixes P0-6 so
    # "daily 08:00" fires at 08:00 user-local, not 08:00 UTC.
    tz_name = (args.get("timezone") or "").strip() or _resolve_user_timezone(db, user_id) or "UTC"
    cron_expression = None
    next_run_at = None
    parse_error = None
    if schedule and schedule.lower() != "manual":
        try:
            from app.services.schedule_parser import parse_schedule, next_run_at as _next_run_at
            cron_expression = parse_schedule(schedule)
            next_run_at = _next_run_at(cron_expression, tz_name=tz_name)
        except Exception as e:
            parse_error = str(e)
            # Don't fail the whole call — just leave it un-scheduled.
            cron_expression = None
            next_run_at = None

    # Default status to "active" when a valid cron is supplied so the new
    # task actually runs. The frontend can still toggle to paused. The LLM
    # may pass a non-canonical value (e.g. "running") — coerce it to the
    # canonical default instead of persisting garbage that the dispatcher
    # would silently skip forever (see AutomationTask.VALID_STATUSES).
    status = args.get("status")
    if not status:
        status = "active" if cron_expression else "paused"
    elif status not in AutomationTask.VALID_STATUSES:
        _default_status = "active" if cron_expression else "paused"
        logger.warning(
            "create_automation: invalid status %r -> coerced to %r (task=%r)",
            status, _default_status, args.get("name", "Untitled Automation"),
        )
        status = _default_status

    # Resolve session_id: explicit arg wins (if UUID-shaped) else chat session
    # from context. The fallback is what makes the Manus-style "Scheduled"
    # button appear in the chat header — the task gets linked to the chat it
    # was created from, so the per-session endpoint finds it. The placeholder
    # guard rejects truthy-but-non-UUID strings (e.g. "TOOL_CONTEXT.foo") that
    # would otherwise match the wrong ChatSession row.
    _arg_sid = args.get("session_id")
    if _looks_like_uuid(_arg_sid):
        session_id = _arg_sid.strip()
    else:
        if _arg_sid and _looks_like_llm_placeholder(_arg_sid):
            logger.warning(
                "create_automation: ignoring placeholder session_id %r; "
                "falling back to TOOL_CONTEXT.get('chat_session_id')",
                _arg_sid,
            )
        session_id = TOOL_CONTEXT.get("chat_session_id")

    # Resolve project_id. Prefer a UUID-shaped explicit arg; else fall back to
    # the auto-injected TOOL_CONTEXT (the LLM frequently echoes back
    # placeholder strings like "TOOL_CONTEXT.project_id" verbatim — see the
    # 2026-08-25 FK-violation bug where that literal string blew through the
    # `args.get("project_id") or TOOL_CONTEXT.get(...)` chain and caused a
    # ForeignKeyViolation on insert). After TOOL_CONTEXT, look up the chat
    # session's project; after that, resolve the legacy `project` name. If
    # still unknown, leave it None and fall back to the legacy `project`
    # string for the project label.
    _arg_pid = args.get("project_id")
    if _looks_like_uuid(_arg_pid):
        project_id = _arg_pid.strip()
    else:
        if _arg_pid and _looks_like_llm_placeholder(_arg_pid):
            logger.warning(
                "create_automation: ignoring placeholder project_id %r; "
                "falling back to TOOL_CONTEXT / session / name resolution",
                _arg_pid,
            )
        project_id = TOOL_CONTEXT.get("project_id")
    if not project_id and session_id:
        try:
            from app.models.chat_session import ChatSession
            sess = db.query(ChatSession).filter(
                ChatSession.id == session_id,
                ChatSession.is_deleted == False,  # noqa: E712
            ).first()
            if sess is not None and getattr(sess, "project_id", None):
                project_id = sess.project_id
                # memoize for sibling tool calls in the same iteration
                TOOL_CONTEXT["project_id"] = project_id
        except Exception:
            pass

    # Last resort: resolve the legacy project NAME (e.g. "test") to a
    # real project id so new tasks carry the FK going forward.
    # Deterministic (most recently updated non-deleted match,
    # org/app-scoped, case-insensitive). Tasks created before this
    # fallback existed are covered by the executor's runtime adoption
    # (``_resolve_task_project``).

    # When we resolved a project_id via fallback (placeholders rejected
    # or session-derived) but the LLM did not also pass the legacy
    # ``project`` name string, look it up from the Project row so the
    # stored name matches the FK rather than defaulting to "global".
    # Without this, the row's project_id FK is correct but its
    # ``project`` label is "global", which messes up the executor's
    # runtime project resolution and KB scoping.
    #
    # We write the resolved name back into ``args`` rather than a local
    # variable because line ~1709 reads ``args.get("project", "global")``
    # at SQL construction time.
    if project_id and not (args.get("project") or "").strip():
        try:
            from app.models.project import Project
            _proj_row = db.get(Project, project_id)
            if _proj_row is not None and _proj_row.name:
                args["project"] = _proj_row.name
        except Exception:
            pass

    if not project_id:
        try:
            from sqlalchemy import func as _func
            from app.models.project import Project
            from app.services.data_source_runtime.data_source_runtime import (
                _normalize_project_name,
            )
            _legacy_name = _normalize_project_name(args.get("project"))
            if _legacy_name:
                _org = args.get("org_id") or TOOL_CONTEXT.get("org_id") or "default-org"
                _app = args.get("app_id") or TOOL_CONTEXT.get("app_id") or "default-app"
                _adopted = (
                    db.query(Project)
                    .filter(
                        Project.is_deleted == False,  # noqa: E712
                        _func.lower(Project.name) == _legacy_name.lower(),
                        Project.org_id == _org,
                        Project.app_id == _app,
                    )
                    .order_by(Project.updated_date.desc())
                    .first()
                )
                if _adopted is not None:
                    project_id = _adopted.id
                    TOOL_CONTEXT["project_id"] = project_id
                    logger.info(
                        "create_automation: adopted project_id=%s from "
                        "legacy project name %r",
                        project_id, _legacy_name,
                    )
        except Exception:
            pass

    # ---- Project-persistence guard (2026-08-11) ----------------------------------
    # A task created from a project-scoped chat MUST never silently persist as
    # "global" / NULL project_id — that would orphan the run's data-source
    # bindings (only global-scoped KBs would be visible at runtime).  When the
    # chat carries a project but the resolved project_id is None, re-derive
    # from the linked session row and log a loud warning so the divergence is
    # auditable.
    _ctx_project_id = TOOL_CONTEXT.get("project_id")
    _ctx_project_name = TOOL_CONTEXT.get("project_name", "").strip()
    if (_ctx_project_id or _ctx_project_name) and not project_id:
        # Agent claimed a project context but resolution failed.
        # Try to recover from the linked session's project_id.
        if session_id:
            try:
                from app.models.chat_session import ChatSession
                _sess = db.query(ChatSession).filter(
                    ChatSession.id == session_id,
                    ChatSession.is_deleted == False,  # noqa: E712
                ).first()
                if _sess is not None and _sess.project_id:
                    project_id = _sess.project_id
                    project = _sess.project or project
                    logger.warning(
                        "create_automation: PROJECT GUARD — project_id was "
                        "None despite chat context (%r / %r); recovered "
                        "project_id=%s from linked session %s",
                        _ctx_project_id, _ctx_project_name, project_id,
                        session_id,
                    )
                    # Also correct the TOOL_CONTEXT so future resolution
                    # (e.g. by the caller's next tool call) is consistent.
                    TOOL_CONTEXT["project_id"] = project_id
            except Exception:
                pass
        if not project_id:
            logger.error(
                "create_automation: PROJECT GUARD — task %r claimed project "
                "context (%r / %r) but will persist with project=global / "
                "project_id=NULL. Calls to this task may have wrong data-"
                "source bindings!",
                task_name, _ctx_project_id, _ctx_project_name,
            )

    # P0: Adopt the Manus UX — every automation gets a dedicated chat
    # session, even when the task wasn't created from inside a chat. The
    # dispatcher's ``_notify_chat`` writes the run result to
    # ``task.session_id`` on every execution, so without a session the
    # result has nowhere to land (it falls back to the user's most
    # recent session, which is usually wrong and clutters unrelated
    # conversations). We auto-create a fresh ChatSession +
    # AgentConversation pair here, named after the task, and link it
    # to the same project so KB scoping still works. The existing
    # session-rename block further down is a no-op in this branch
    # (the title already matches ``task.name``).
    if not session_id:
        try:
            from app.models.agent_conversation import AgentConversation
            from app.models.chat_session import ChatSession
            from datetime import datetime, timezone as _dt
            task_name = (args.get("name") or "Untitled Automation").strip()
            # Use the same agent_name resolution that the chat
            # ``create_conversation`` endpoint uses (None → chat page
            # picks a default). The conversation has no agent attached
            # because runs are driven by ``automation_runtime_agent``,
            # not the chat agent — this is just a place to write the
            # result message.
            conv = AgentConversation(
                agent_name=None,
                title=task_name,
                messages=[],
                status="active",
                created_by_id=user_id,
                project_id=project_id,
            )
            conv.metadata_ = {
                "source": "automation_create",
                "auto_session": True,
                # Note the project name for the sidebar's project
                # chip; falls back to "global" if the user didn't
                # pick one.
                "project": args.get("project", "global"),
            }
            db.add(conv)
            db.flush()  # populate conv.id without committing yet
            chat = ChatSession(
                title=task_name,
                project_id=project_id,
                project=args.get("project", "global"),
                conversation_id=conv.id,
                agent_name=None,
                starred=False,
                last_message_at=_dt.utcnow().isoformat(),
            )
            db.add(chat)
            db.flush()
            session_id = chat.id
            # Memoize for sibling tool calls in the same iteration
            TOOL_CONTEXT["chat_session_id"] = session_id
            logger.info(
                "create_automation: auto-adopted session %s (conv %s) for task '%s'",
                session_id, conv.id, task_name,
            )
        except Exception as _auto_err:
            # Non-fatal: the task is still created with session_id=None
            # and the dispatcher's fallback session will pick up the
            # run result. Logged for diagnostics; the rename block
            # below also tolerates a None session_id.
            logger.warning(
                "create_automation: auto-adopt session failed, falling back: %s",
                _auto_err,
            )

    # Auto-detect output_format from the user's natural-language description
    # when the LLM forgot to pass it. Keyword priority is fixed to avoid
    # random matches: explicit (Word) beats "docx" because users sometimes
    # say "Word file"; docx beats pdf so "PDF report" wins over the loose
    # "doc" inside "docx". The agent's tool schema and system prompt now
    # instruct the LLM to set this explicitly, but this is a safety net so
    # a chat user who types "give me in docx" actually gets a docx.
    output_format = (args.get("output_format") or "").strip().lower()
    if not output_format:
        text_blob = " ".join(
            str(args.get(k) or "") for k in ("description", "prompt", "name")
        ).lower()
        format_hints = [
            ("docx",   ["docx", "word file", "word document", ".docx", " ms word"]),
            ("xlsx",   ["xlsx", "excel file", "excel document", "spreadsheet"]),
            ("pptx",   ["pptx", "powerpoint", "power point", "slide deck"]),
            ("pdf",    [" pdf ", ".pdf", "pdf file", "pdf document", "as a pdf", "give me in pdf"]),
            ("md",     ["markdown", " md ", "md file", ".md"]),
        ]
        for fmt, keywords in format_hints:
            if any(k in text_blob or text_blob.endswith(k.lstrip()) or text_blob.startswith(k.lstrip()) for k in keywords):
                output_format = fmt
                break
    if not output_format:
        output_format = "html"

    # Normalize skills (list of skill names) — the LLM may pass a JSON array
    # or a comma-separated string. Stored on automation_tasks.skills and used
    # by the executor for progressive-disclosure metadata injection.
    skills = _normalize_skills(args.get("skills"))

    task = AutomationTask(
        name=args.get("name", "Untitled Automation"),
        type=args.get("type", "custom"),
        description=args.get("description", ""),
        prompt=args.get("prompt") or args.get("description", ""),
        schedule=schedule,
        cron_expression=cron_expression,
        timezone=tz_name,
        next_run_at=next_run_at,
        project=args.get("project", "global"),
        project_id=project_id,
        session_id=session_id,
        data_source_id=args.get("data_source_id"),
        skills=skills or None,
        agent_id=_resolve_task_executor_agent(db, args),
        org_id=args.get("org_id") or TOOL_CONTEXT.get("org_id") or "default-org",
        app_id=args.get("app_id") or TOOL_CONTEXT.get("app_id") or "default-app",
        output_format=output_format,
        notify_chat=str(args.get("notify_chat", True)).lower() in ("1", "true", "yes"),
        # LLM-informed tick (opt-in): when True the executor injects a
        # per-tick LLM briefing ("what to focus on THIS run") into the
        # agent prompt. Parse tolerant of 1/true/yes strings from the LLM.
        llm_informed_tick=str(args.get("llm_informed_tick", False)).lower()
        in ("1", "true", "yes"),
        status=status,
        created_by_id=user_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # Unified Resource Registry sync (best-effort, flag-gated).
    try:
        if getattr(settings, "KG_RESOURCE_REGISTRY_ENABLED", False) and task.project_id:
            from app.services.knowledge_graph.registry_indexer import index_automation

            index_automation(
                db,
                project_id=task.project_id,
                automation_id=task.id,
                name=task.name,
                summary=(task.description or task.prompt or "").strip(),
                owner_user_id=user_id,
                visibility="project",
            )
            db.commit()
    except Exception:
        logger.debug("create_automation: registry sync failed (non-fatal)", exc_info=True)

    # ---- Adopt the chat session: rename + tag as an automation session -----
    # After the task is created we propagate the automation name back to the
    # originating chat session so the user can find it in the sidebar. The
    # session keeps the same id (no message routing changes) — only the
    # title and a few metadata fields change. The sidebar already shows a
    # clock icon for sessions with an automation, so we prefix the title
    # with "🕒" to make the association visually obvious.
    if session_id:
        try:
            from app.models.chat_session import ChatSession
            chat = db.query(ChatSession).filter(
                ChatSession.id == session_id,
                ChatSession.is_deleted == False,  # noqa: E712
            ).first()
            if chat is not None:
                # Use the task name directly as the session title — the
                # sidebar already shows its own clock icon for sessions
                # linked to an automation, so an emoji prefix would be
                # redundant.
                new_title = task.name
                # Don't overwrite a user-customized title with a generic one,
                # but always upgrade a placeholder/auto-title to the task name.
                cur = (chat.title or "").strip()
                placeholder = cur == "" or cur.lower() in {
                    "new task", "new chat", "untitled", "untitled chat",
                } or cur.startswith("new ")
                if placeholder or cur != new_title:
                    chat.title = new_title
                    db.commit()
        except Exception as _rename_err:
            # Non-fatal: the task is created and linked, only the rename
            # is skipped. The session still works as an automation session.
            logger.debug("create_automation: session rename skipped: %s", _rename_err)

    result = task.to_dict()
    if next_run_at:
        result["next_run_at"] = next_run_at.isoformat()
    if cron_expression:
        result["cron_expression"] = cron_expression
    if parse_error:
        result["parse_warning"] = parse_error
    result["success"] = True
    return result


def _update_automation(args: dict, db: Session, user_id: str | None) -> dict:
    """Update an existing AutomationTask record."""
    # Accept task_id from top level OR from inside fields.
    task_id = _resolve_update_id(args, "task_id", "id", "automation_id")
    if not task_id:
        logger.warning(
            "update_automation called without task_id (args keys=%s). "
            "Reminder: task_id MUST be a top-level sibling of fields.",
            list(args.keys()) if isinstance(args, dict) else None,
        )
        return {
            "success": False,
            "error": (
                "task_id is required and must be a top-level sibling of "
                "'fields' (not nested inside fields)."
            ),
        }

    fields = args.get("fields") or {}
    if not isinstance(fields, dict):
        return {"success": False, "error": "'fields' must be an object"}

    task = db.query(AutomationTask).filter(
        AutomationTask.id == task_id,
        AutomationTask.is_deleted == False,
    ).first()

    if not task:
        return {"success": False, "error": f"Automation task not found: {task_id}"}

    allowed_fields = {
        "name", "type", "description", "schedule", "project", "project_id",
        "status", "prompt", "output_format", "notify_chat", "max_retries",
        "skip_confirmation", "agent_id", "session_id", "data_source_id", "skills",
        "llm_informed_tick",
    }

    schedule_changed = False
    for key, value in fields.items():
        if key in allowed_fields:
            if key == "schedule" and value != task.schedule:
                schedule_changed = True
            if key == "skills":
                value = _normalize_skills(value)
            setattr(task, key, value)

    # When the schedule changes, re-parse and recompute next_run_at.
    if schedule_changed and task.schedule and task.schedule.lower() != "manual":
        try:
            from app.services.schedule_parser import parse_schedule, next_run_at as _next_run_at
            task.cron_expression = parse_schedule(task.schedule)
            task.next_run_at = _next_run_at(task.cron_expression)
        except Exception as e:
            logger.warning("_update_automation: schedule parse failed: %s", e)
            task.cron_expression = None
            task.next_run_at = None

    # If the user toggled the task active and we have a valid cron,
    # make sure next_run_at is in the future.
    if task.status == "active" and task.cron_expression and (
        task.next_run_at is None or task.next_run_at < datetime.now(timezone.utc)
    ):
        try:
            from app.services.schedule_parser import next_run_at as _next_run_at
            task.next_run_at = _next_run_at(task.cron_expression)
        except Exception:
            pass

    from datetime import datetime, timezone
    task.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)

    # Keep registry row in sync after updates (best-effort, flag-gated).
    try:
        if getattr(settings, "KG_RESOURCE_REGISTRY_ENABLED", False) and task.project_id:
            from app.services.knowledge_graph.registry_indexer import index_automation

            index_automation(
                db,
                project_id=task.project_id,
                automation_id=task.id,
                name=task.name,
                summary=(task.description or task.prompt or "").strip(),
                owner_user_id=task.created_by_id,
                visibility="project",
            )
            db.commit()
    except Exception:
        logger.debug("update_automation: registry sync failed (non-fatal)", exc_info=True)

    # If the name changed and this task is linked to a chat session,
    # propagate the new name to the session title so the sidebar stays
    # in sync with the task's current identity.
    if "name" in fields and task.session_id:
        try:
            from app.models.chat_session import ChatSession
            chat = db.query(ChatSession).filter(
                ChatSession.id == task.session_id,
                ChatSession.is_deleted == False,  # noqa: E712
            ).first()
            if chat is not None and (chat.title or "").startswith("🕒 "):
                chat.title = task.name
                db.commit()
        except Exception as _rename_err:
            logger.debug("update_automation: session rename skipped: %s", _rename_err)

    result = task.to_dict()
    if task.next_run_at:
        result["next_run_at"] = task.next_run_at.isoformat()
    if task.cron_expression:
        result["cron_expression"] = task.cron_expression
    result["success"] = True
    return result


def _list_knowledge_bases(args: dict, db: Session, user_id: str | None) -> dict:
    """List configured knowledge bases and data connections for data source options.

    Scoped to the current project when one is in TOOL_CONTEXT. Without a
    project context (e.g. global / unset), the full list is returned for
    backward compatibility. ``project_id`` may be supplied either directly in
    TOOL_CONTEXT, or implicitly via ``chat_session_id`` (we look up the
    session's project).
    """
    project_id = TOOL_CONTEXT.get("project_id")
    if not project_id:
        chat_session_id = TOOL_CONTEXT.get("chat_session_id")
        if chat_session_id:
            try:
                from app.models.chat_session import ChatSession
                sess = db.query(ChatSession).filter(
                    ChatSession.id == chat_session_id,
                    ChatSession.is_deleted == False,  # noqa: E712
                ).first()
                if sess is not None:
                    project_id = getattr(sess, "project_id", None)
                    # memoize for sibling tool calls in the same iteration
                    if project_id:
                        TOOL_CONTEXT["project_id"] = project_id
            except Exception:
                pass

    q = db.query(KnowledgeBase).filter(KnowledgeBase.is_deleted == False)
    if project_id:
        # Dual-column parity with the UI and _extend_with_project_kbs:
        # a KB is "bound" to a project via the FK (project_id) OR the
        # legacy `project` name string. Rows created before the FK
        # existed (or bound via name in the Resources panel) carry only
        # the name; filtering by FK alone would hide them from the
        # agent's data-source enumeration and make the runtime report
        # "no databases" even though the UI shows them connected.
        from sqlalchemy import func as _func, or_
        from app.models.project import Project
        name_clauses = []
        proj = db.query(Project).filter(
            Project.id == project_id,
            Project.is_deleted == False,  # noqa: E712
        ).first()
        if proj is not None and proj.name and proj.name.lower() != "global":
            name_clauses.append(_func.lower(KnowledgeBase.project) == proj.name.lower())
        q = q.filter(or_(KnowledgeBase.project_id == project_id, *name_clauses))
    kbs = q.order_by(KnowledgeBase.name.asc()).all()

    return {
        "success": True,
        "knowledge_bases": [
            {
                "id": kb.id,
                "name": kb.name,
                "description": kb.description or "",
                "project": kb.project or "global",
                "project_id": kb.project_id or "",
                "type": kb.type or "",
                "source_kind": kb.source_kind or "",
                "db_type": kb.db_type or "",
                "database_name": kb.database_name or "",
                "status": kb.status or "",
            }
            for kb in kbs
        ],
        "scoped_to_project": bool(project_id),
    }


# ---------------------------------------------------------------------------
# Populate CRUD dispatch dict (after function defs)
# ---------------------------------------------------------------------------

_CRUD_DISPATCH = {
    "create_agent": _create_agent,
    "update_agent": _update_agent,
    "list_tools": _list_tools,
    "list_market_agents": _list_market_agents,
    "create_skill": _create_skill,
    "update_skill": _update_skill,
    "search_skills": _search_skills,
    "create_automation": _create_automation,
    "update_automation": _update_automation,
    "list_knowledge_bases": _list_knowledge_bases,
}
