#!/usr/bin/env python3
"""Smoke verification for the 2026-07-21 agent-improvement wiring.

Confirms that the three integrations closed in
``docs/plans/2026-07-21-agent-improvement.md`` follow-up plan are actually
reachable at runtime:

  1. The 5 swarm tools (``swarm_create_team``, ``swarm_spawn_agent``,
     ``swarm_send_message``, ``swarm_get_messages``, ``swarm_list_teams``)
     are present in the ToolRegistry after importing
     ``app.services.tool_handlers``.
  2. ``should_trigger_planning`` correctly classifies a multi-step
     message (returns ``should_plan=True``) and stays quiet on a simple
     question (returns ``should_plan=False``).
  3. ``SynexiaFSM`` can be instantiated with a stub database session,
     and ``ExecutionRequest`` exposes the fields used by the chat loop.
  4. The agent router module imports cleanly with all the new wiring
     in place.

Run from the repository root with the backend venv active::

    cd /root/zhanlu/backend
    ./venv/bin/python scripts/smoke_agent_wiring.py

Exit code 0 on all-pass, 1 on any-fail. Stdlib only; no pytest, no
network, no DB writes. The script reads DATABASE_URL from the
environment when present and falls back to a sqlite path so it can run
without a Postgres instance.
"""
from __future__ import annotations

import os
import sys
import traceback
from typing import Callable


# ---------------------------------------------------------------------------
# Bootstrap: make the backend importable. This script lives in
# backend/scripts/ so the backend root is one level up.
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(HERE)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

# Pydantic-settings requires DATABASE_URL to instantiate Settings on
# first import of ``app.*``. Provide a safe default so the smoke test
# can run without a live database.
os.environ.setdefault("DATABASE_URL", "sqlite:///./zhanlu.db")


# ---------------------------------------------------------------------------
# Check harness
# ---------------------------------------------------------------------------
SWARM_TOOL_NAMES = [
    "swarm_create_team",
    "swarm_spawn_agent",
    "swarm_send_message",
    "swarm_get_messages",
    "swarm_list_teams",
]


def _enumerate_registry(registry) -> list:
    """Find every tool name the ToolRegistry exposes.

    Different registry implementations expose the tool list under
    different names (``list_tools``, ``all_tools``, ``tools``) or via
    a private dict (``_tools``, ``_registry``, ``_by_name``). This
    helper tries them all so the smoke test is robust to small
    registry refactors.
    """
    for attr in ("list_tools", "all_tools", "tools"):
        fn = getattr(registry, attr, None)
        if callable(fn):
            try:
                result = fn()
            except Exception:
                continue
            if result is not None:
                return list(result)
    for attr in ("_tools", "_registry", "_by_name"):
        m = getattr(registry, attr, None)
        if m is None:
            continue
        if hasattr(m, "keys"):
            return list(m.keys())
        return list(m)
    return []


def _check_swarm_tools() -> tuple[bool, str]:
    """Confirm the 5 swarm tools are in the ToolRegistry."""
    import app.services.tool_handlers  # noqa: F401  — triggers registration
    from app.services.tool_registry import registry

    names = _enumerate_registry(registry)
    found = [n for n in SWARM_TOOL_NAMES if n in names]
    missing = [n for n in SWARM_TOOL_NAMES if n not in names]
    ok = len(missing) == 0
    detail = f"registry size={len(names)}; found {len(found)}/5 swarm tools"
    if missing:
        detail += f"; missing={missing}"
    return ok, detail


def _check_planning_trigger() -> tuple[bool, str]:
    """Confirm the classifier fires on multi-step and stays quiet on simple."""
    from app.services.planning_trigger import should_trigger_planning

    multi = should_trigger_planning(
        "Plan a 3-step refactor then run the tests and deploy"
    )
    simple = should_trigger_planning("What is 2+2?")
    multi_ok = bool(multi) and multi.should_plan and multi.confidence >= 0.4
    simple_ok = not bool(simple) and not simple.should_plan
    ok = multi_ok and simple_ok
    detail = (
        f"multi_step should_plan={multi.should_plan} "
        f"conf={multi.confidence:.3f} signals={multi.signals} | "
        f"simple should_plan={simple.should_plan} "
        f"conf={simple.confidence:.3f}"
    )
    return ok, detail


def _check_synexia_fsm() -> tuple[bool, str]:
    """Confirm SynexiaFSM is importable, instantiable, and gated by the flag."""
    from app.services.synexia.fsm import (
        ExecutionRequest,
        SynexiaFSM,
        is_fsm_enabled,
    )

    class _StubDB:
        pass

    fsm = SynexiaFSM(_StubDB())
    fields = list(ExecutionRequest.model_fields.keys())
    # The chat loop passes these exact fields; verify they're all there.
    required = {"conversation_id", "agent_name", "user_message"}
    fields_ok = required.issubset(fields)
    type_ok = isinstance(fsm, SynexiaFSM)
    flag_ok = isinstance(is_fsm_enabled(), bool)
    ok = fields_ok and type_ok and flag_ok
    detail = (
        f"SynexiaFSM instantiated={type_ok} "
        f"ExecutionRequest fields_ok={fields_ok} "
        f"is_fsm_enabled={is_fsm_enabled()} (type={type(is_fsm_enabled()).__name__})"
    )
    return ok, detail


def _check_router_import() -> tuple[bool, str]:
    """Confirm the agent router imports cleanly with all wiring in place."""
    import app.routers.agents as agents

    funcs_ok = callable(getattr(agents, "add_message", None)) and callable(
        getattr(agents, "add_message_stream", None)
    )
    # Spot-check that the planning symbols are reachable from the
    # router module (proves the imports were actually added, not just
    # syntactically valid).
    syms = {n for n in dir(agents) if not n.startswith("__")}
    needs = {"should_trigger_planning", "SynexiaFSM"}
    syms_ok = needs.issubset(syms)
    ok = funcs_ok and syms_ok
    detail = (
        f"add_message callable={callable(getattr(agents, 'add_message', None))} "
        f"add_message_stream callable={callable(getattr(agents, 'add_message_stream', None))} "
        f"imports present={syms_ok} "
        f"missing={[n for n in needs if n not in syms]}"
    )
    return ok, detail


CHECKS: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
    ("swarm_tools_registered", _check_swarm_tools),
    ("planning_trigger_classifies", _check_planning_trigger),
    ("synexia_fsm_instantiable", _check_synexia_fsm),
    ("agent_router_imports", _check_router_import),
]


def main() -> int:
    print("=" * 60)
    print("Agent Wiring Smoke Verification (2026-07-21 follow-up)")
    print("=" * 60)
    print()

    failures: list[str] = []
    results: list[tuple[str, bool, str]] = []

    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as exc:  # pragma: no cover - defensive
            ok = False
            detail = f"EXCEPTION: {type(exc).__name__}: {exc}"
            traceback.print_exc()
        results.append((name, ok, detail))
        if not ok:
            failures.append(name)

    # Render the one-page report.
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        print(f"         {detail}")

    print()
    print("-" * 60)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"  {passed}/{total} checks passed")
    if failures:
        print(f"  FAILED: {', '.join(failures)}")
        print()
        print("RESULT: FAIL")
        return 1
    print()
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
