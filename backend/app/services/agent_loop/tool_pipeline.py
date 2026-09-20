"""Tool execution pipeline seam — the Zhanlu equivalent of the harness'
``tools/pre-execute`` → ``tools/execute`` → ``tools/post-execute`` chain.

Two responsibilities, both about giving the agent loop *one* execution path:

1. **Active-pipeline contextvar.** ``execute_tool_batch`` activates the turn's
   pipeline (guard + invoker + db/user/context). Nested callers — today the
   ``execute_code`` PTC bridge — look it up and dispatch their sub-calls
   through the *same* guard/permission/registry path the loop uses, instead of
   re-implementing a second, weaker dispatcher.

2. **Post-execute transform seam.** Ordered transforms registered by any
   module run after a tool body returns and before the loop records the result
   (``register_post_transform``). A transform may inspect and replace a result;
   returning ``None`` means "accept as-is". This is where cross-cutting result
   policy belongs (redaction, trimming, notices) so it does not have to live in
   the router.

Guards enforced for sub-calls (mirrors the harness' PTC rules: a nested call is
never allowed to escape the pipeline):

- ``PTC_ENTRY_TOOLS`` can never be called from inside a sub-call (no recursion,
  no unbounded fan-out through the sandbox).
- A hard cap on sub-calls per pipeline (``sub_call_limit``); exceeding it is an
  error, not a silent no-op.
- ``MAX_SUB_CALL_DEPTH`` bounds nesting.
- A call whose permission check demands interactive confirmation is *denied*
  rather than paused: code cannot answer an approval prompt. The denial is
  raised into the sandbox so the model can see it and adapt (the harness'
  "denials return as binding rejections").
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

# Tools that may not be invoked from inside a sub-call.
PTC_ENTRY_TOOLS: frozenset[str] = frozenset({"execute_code"})

# Bounds. Deliberately small: a single code block is meant to replace a handful
# of model round-trips, not to become a batch engine of its own.
DEFAULT_SUB_CALL_LIMIT: int = 25
MAX_SUB_CALL_DEPTH: int = 2
SUB_CALL_EXCLUDED_TOOLS: frozenset[str] = frozenset(
    {
        # Interactive tools cannot work inside code: there is no user turn
        # to answer inside a single code block.
        "clarify",
        "ask_user",
        "confirm_decision",
    }
)


class PipelineDenied(RuntimeError):
    """A sub-call was denied by the pipeline (guard, permission, or bound)."""

    def __init__(self, tool_name: str, reason: str, *, code: str = "denied") -> None:
        super().__init__(f"{tool_name}: {reason}")
        self.tool_name = tool_name
        self.reason = reason
        self.code = code


@dataclass
class PostTransformResult:
    """Outcome of one post-execute transform."""

    result: dict
    additional_context: list[str] = field(default_factory=list)


PostTransform = Callable[[str, dict, dict], "PostTransformResult | dict | None"]


def guard_verdict(decision: Any) -> tuple[bool, str]:
    """Interpret one guard decision. Direction of failure: DENY.

    ``decision`` comes from a guard registered by the loop
    (:class:`app.services.tool_loop_guardrails.ToolLoopGuardController`) and is
    whatever that callable returned. Three cases:

    - ``None``                    → abstain. The guard has no opinion, so the
      call proceeds (an abstaining guard is not a denial).
    - ``allows_execution is True`` → allow.
    - anything else — ``False``, or a decision shape that does not declare
      ``allows_execution`` at all — → DENY. A guard whose verdict cannot be read
      must not be readable as permission; that is how a policy seam silently
      stops protecting anything.
    """
    if decision is None:
        return True, ""
    allows = getattr(decision, "allows_execution", None)
    if allows is True:
        return True, ""
    if allows is False:
        return False, str(getattr(decision, "reason", None) or "blocked by guardrail")
    return False, (
        "guard returned a decision without 'allows_execution' "
        f"({type(decision).__name__}) — refusing the call rather than assuming permission"
    )


@dataclass
class PipelineState:
    """The tool execution pipeline for one tool batch (one turn step)."""

    invoke: Callable[[str, dict], Awaitable[dict]]
    before_call: Callable[[str, dict], Any] | None = None
    blocked_result_factory: Callable[[Any], str] | None = None
    db: Any = None
    user_id: str | None = None
    context: dict | None = None
    depth: int = 0
    sub_call_limit: int = DEFAULT_SUB_CALL_LIMIT
    sub_calls: list[dict] = field(default_factory=list)
    # Set once a nested code block starts; used only for the report.
    nested: bool = False

    @property
    def sub_call_count(self) -> int:
        return len(self.sub_calls)


_active_pipeline: contextvars.ContextVar[PipelineState | None] = contextvars.ContextVar(
    "zhanlu_active_tool_pipeline", default=None
)

_post_transforms: list[tuple[str, PostTransform, bool]] = []


def register_post_transform(
    transform: PostTransform, *, name: str | None = None, best_effort: bool = False
) -> None:
    """Register a post-execute transform. Transforms run in registration order.

    ``transform(tool_name, args, result)`` may return:

    - ``None``                → accept the result unchanged
    - a ``dict``              → replace the result
    - a ``PostTransformResult`` → replace the result and attach extra context
      lines for the model (rendered by the loop, not here)

    Direction of failure (this seam is a *policy* seam, so it fails closed):

    - ``best_effort=False`` (default) — the transform carries result policy
      (redaction, withheld content, notices). If it raises, the result is
      WITHHELD and replaced by a structured failure. Accepting the untouched
      result would publish content the transform existed to withhold.
    - ``best_effort=True`` — purely decorative. A raise is logged and the
      result passes through unchanged.
    """
    _post_transforms.append(
        (name or getattr(transform, "__name__", "transform"), transform, bool(best_effort))
    )


def clear_post_transforms() -> None:
    """Test hook — drop every registered transform."""
    _post_transforms.clear()


def registered_transform_names() -> list[str]:
    return [name for name, _t, _be in _post_transforms]


def apply_post_transforms(tool_name: str, args: dict, result: dict) -> tuple[dict, list[str]]:
    """Run the post-execute seam.

    Never raises — but it does not silently accept a result a policy transform
    failed to inspect: a raising non-``best_effort`` transform yields a
    structured failure result instead (see ``register_post_transform``).
    """
    current = result
    contexts: list[str] = []
    for name, transform, best_effort in _post_transforms:
        try:
            outcome = transform(tool_name, args or {}, current)
        except Exception as exc:  # noqa: BLE001 — containment is the point
            if best_effort:
                logger.warning("post-transform '%s' raised for %s: %s", name, tool_name, exc)
                continue
            logger.warning(
                "post-transform '%s' raised for %s (withholding result — fail-closed): %s",
                name, tool_name, exc,
            )
            current = {
                "success": False,
                "error": (
                    f"Result withheld — post-transform '{name}' failed (fail-closed): {exc}"
                ),
                "tool_name": tool_name,
            }
            continue
        if outcome is None:
            continue
        if isinstance(outcome, PostTransformResult):
            if isinstance(outcome.result, dict):
                current = outcome.result
            contexts.extend(outcome.additional_context)
        elif isinstance(outcome, dict):
            current = outcome
    return current, contexts


def active_pipeline() -> PipelineState | None:
    return _active_pipeline.get()


@contextlib.contextmanager
def activate_pipeline(state: PipelineState) -> Iterator[PipelineState]:
    """Make ``state`` the active pipeline for the enclosed block.

    Re-activation (a nested tool batch inside an active pipeline) keeps the
    outermost pipeline — sub-calls must not be able to install a weaker guard
    chain — but records that nesting happened.
    """
    existing = _active_pipeline.get()
    if existing is not None:
        existing.depth += 1
        existing.nested = True
        try:
            yield existing
        finally:
            existing.depth -= 1
        return

    token = _active_pipeline.set(state)
    try:
        yield state
    finally:
        _active_pipeline.reset(token)


def _emit_activity(state: PipelineState, label: str, status: str, tool_name: str) -> None:
    """Best-effort activity step for the chat UI (sub-calls become visible)."""
    ctx = state.context or {}
    emitter = ctx.get("progress_emitter")
    counter = ctx.get("step_counter")
    if emitter is None or counter is None:
        return
    try:
        from app.services.agent_loop.sse_builders import _emit_activity_step

        counter[0] += 1
        emitter.append(
            _emit_activity_step(counter[0], label, status, tool_name=tool_name)
        )
    except Exception as exc:  # noqa: BLE001 — UI decoration must never fail a call
        logger.debug("PTC activity emit failed: %s", exc)


def _permission_gate(state: PipelineState, tool_name: str, args: dict) -> str | None:
    """Return a denial reason if the sub-call must not run, else None."""
    agent_name = (state.context or {}).get("agent_name")
    metadata = (state.context or {}).get("conversation_metadata")
    try:
        from app.services.permissions import check_permission

        perm = check_permission(
            tool_name, args, agent_name, conversation_metadata=metadata
        )
    except Exception as exc:  # noqa: BLE001 — fail closed, like the dispatcher
        return f"permission check failed ({exc})"
    if not perm.allowed:
        return f"permission denied: {perm.reason}"
    if getattr(perm, "requires_confirmation", False):
        return (
            "this call needs the user's explicit confirmation, which cannot be "
            "collected inside a code block — call the tool directly in a normal "
            "tool call instead"
        )
    return None


async def dispatch_sub_call(
    tool_name: str,
    args: dict,
    *,
    pipeline: PipelineState | None = None,
) -> dict:
    """Run one tool call through the active pipeline.

    Raises :class:`PipelineDenied` for bound/recursion/permission denials so the
    caller inside the sandbox can see and adapt. Tool bodies that fail return
    their structured ``{"success": False, ...}`` result unchanged.
    """
    state = pipeline or active_pipeline()
    if state is None:
        raise PipelineDenied(
            tool_name,
            "no active agent-turn pipeline — tools can only be called from "
            "inside a chat turn",
            code="no_pipeline",
        )

    if tool_name in PTC_ENTRY_TOOLS:
        raise PipelineDenied(
            tool_name,
            "recursive execution is not allowed inside a code block",
            code="recursion_blocked",
        )
    if tool_name in SUB_CALL_EXCLUDED_TOOLS:
        raise PipelineDenied(
            tool_name,
            "interactive tools cannot be called from inside a code block",
            code="interactive_tool",
        )
    if state.depth > MAX_SUB_CALL_DEPTH:
        raise PipelineDenied(
            tool_name, "sub-call nesting limit reached", code="depth_exceeded"
        )
    if state.sub_call_count >= state.sub_call_limit:
        raise PipelineDenied(
            tool_name,
            f"sub-call limit reached ({state.sub_call_limit} calls in one code block)",
            code="sub_call_limit",
        )

    denial = _permission_gate(state, tool_name, args)
    if denial:
        raise PipelineDenied(tool_name, denial, code="permission")

    # Guard chain (loop-owned policy: phase locks, loop guards, per-agent caps).
    if state.before_call is not None:
        try:
            decision = state.before_call(tool_name, args)
        except Exception as exc:  # noqa: BLE001 — fail closed
            raise PipelineDenied(tool_name, f"guard failed: {exc}", code="guard_error") from exc
        allows, reason = guard_verdict(decision)
        if not allows:
            raise PipelineDenied(tool_name, reason, code="guard_blocked")

    _emit_activity(state, f"call_tool {tool_name}", "running", tool_name)
    try:
        result = await state.invoke(tool_name, args)
    except Exception as exc:  # noqa: BLE001 — surfaced to the code, not the turn
        logger.warning("PTC sub-call %s raised: %s", tool_name, exc)
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    if not isinstance(result, dict):
        result = {"success": True, "result": result}

    if result.get("requires_approval"):
        raise PipelineDenied(
            tool_name,
            "the user must approve this call — run it as a direct tool call, "
            "not from inside a code block",
            code="approval_required",
        )

    result, extra_contexts = apply_post_transforms(tool_name, args, result)
    state.sub_calls.append(
        {
            "tool_name": tool_name,
            "args": args,
            "success": bool(result.get("success")),
            "contexts": extra_contexts,
        }
    )
    _emit_activity(
        state,
        f"call_tool {tool_name}",
        "done" if result.get("success") else "failed",
        tool_name,
    )
    return result


__all__ = [
    "DEFAULT_SUB_CALL_LIMIT",
    "MAX_SUB_CALL_DEPTH",
    "PTC_ENTRY_TOOLS",
    "SUB_CALL_EXCLUDED_TOOLS",
    "PipelineDenied",
    "PipelineState",
    "PostTransformResult",
    "activate_pipeline",
    "active_pipeline",
    "apply_post_transforms",
    "clear_post_transforms",
    "dispatch_sub_call",
    "register_post_transform",
    "registered_transform_names",
]
