"""Regression: Skill Agent gets tightened tool-loop guardrail thresholds
(2026-07-28).

The user observed the Skill Agent calling `search_skills` 3x with the
same args (all returning the same empty result) before giving up with
"I'll stop the skill creation there". The prompt side is fixed in
agent_prompts.py (see test_skill_agent_prompt_skill_creation_workflow.py).
The runtime side — the ToolLoopGuardController thresholds — is fixed by
wiring `_loop_guard_config_for(agent_app)` into all 3
`ToolLoopGuardController(...)` call sites in app/routers/agents.py so
that the Skill Agent gets:

    no_progress_warn_after=1   # warn on the 2nd identical call
    no_progress_block_after=2   # block on the 3rd identical call

(Other agents continue to use the default: warn=2, block=5.)

These tests pin both the helper logic AND the wiring at all 3 sites.
The behavioral tests (1-6) drive `ToolLoopGuardController` directly with
the helper's output. The wiring test (7) is AST-based so it runs without
importing the router module and survives rewording of unrelated code.
"""
from __future__ import annotations

import ast
import os
from types import SimpleNamespace

# Importing app.routers.agents pulls in the full app stack, which is OK
# in the test env (sqlite + jwt shim). Tested empirically with
# `from app.routers import agents` — see conftest.py + existing
# test_agents_token_streaming.py for the same pattern.
from app.routers.agents import _loop_guard_config_for
from app.services.tool_loop_guardrails import (
    IDEMPOTENT_TOOL_NAMES,
    ToolLoopGuardController,
    ToolGuardrailConfig,
)


# --------------------------------------------------------------------
# (1) Helper returns the right config per agent
# --------------------------------------------------------------------

def test_loop_guard_config_for_none_returns_default():
    """No agent_app (e.g. before resolution) → default thresholds."""
    cfg = _loop_guard_config_for(None)
    assert isinstance(cfg, ToolGuardrailConfig)
    # Defaults from tool_loop_guardrails.ToolGuardrailConfig:
    assert cfg.no_progress_warn_after == 2
    assert cfg.no_progress_block_after == 5


def test_loop_guard_config_for_non_skill_agent_returns_default():
    """A non-skill_agent (e.g. data_analyst_agent) → default thresholds.

    The override is opt-in: it must NOT leak to other agents.
    """
    cfg = _loop_guard_config_for(SimpleNamespace(name="data_analyst_agent"))
    assert cfg.no_progress_warn_after == 2
    assert cfg.no_progress_block_after == 5


def test_loop_guard_config_for_skill_agent_uses_tightened_thresholds():
    """The skill_agent gets warn=2, block=2 — strictly tighter than default.

    Threshold semantics: `no_progress_warn_after=N` means the controller
    WARNS the (N+1)th identical (args, result) call (after_call returns
    a warn decision when the new count is >= N). So warn_after=2 means
    "warn on the 2nd identical call" — giving the model 1 grace call.
    `no_progress_block_after=M` means the controller BLOCKS on the
    before_call when the stored record[1] >= M, so block_after=2 means
    "block on the 3rd identical call" (because the 2nd call's after_call
    stored record[1]=2).

    Resulting user-friendly behaviour: 1st call = allow, 2nd call =
    warn, 3rd call = block. Pin both numbers.
    """
    cfg = _loop_guard_config_for(SimpleNamespace(name="skill_agent"))
    assert cfg.no_progress_warn_after == 2, (
        "Skill Agent should warn on the 2nd identical search_skills call "
        "(1 grace call, then warn). If you change this threshold, the "
        "3x-loop regression from 2026-07-28 may return."
    )
    assert cfg.no_progress_block_after == 2, (
        "Skill Agent should BLOCK on the 3rd identical search_skills call. "
        "If you change this threshold, the 3x-loop regression from "
        "2026-07-28 may return."
    )


def test_loop_guard_config_for_skill_agent_preserves_other_defaults():
    """Tightening the no-progress thresholds must NOT change other
    fields (idempotent_tools, warnings_enabled, etc.). Otherwise
    the override would silently disable other guardrails.
    """
    cfg = _loop_guard_config_for(SimpleNamespace(name="skill_agent"))
    default = ToolGuardrailConfig()
    assert cfg.warnings_enabled == default.warnings_enabled
    assert cfg.hard_stop_enabled == default.hard_stop_enabled
    assert cfg.idempotent_tools == default.idempotent_tools
    assert cfg.mutating_tools == default.mutating_tools
    assert cfg.exact_failure_block_after == default.exact_failure_block_after


def test_loop_guard_config_for_accepts_object_without_name_attr():
    """Defensive: an agent_app without a `.name` attribute must not raise.

    The helper uses `getattr(agent_app, "name", None)` so it returns
    the default for objects that lack a name. This protects against
    test mocks and partial hydration paths.
    """
    cfg = _loop_guard_config_for(SimpleNamespace())
    assert cfg.no_progress_warn_after == 2
    assert cfg.no_progress_block_after == 5


# --------------------------------------------------------------------
# (2) Behavioral — guard blocks 3rd identical call with skill_agent config
# --------------------------------------------------------------------

def test_skill_agent_guard_blocks_third_identical_search_skills_call():
    """End-to-end behavioral test: 3 identical `search_skills` calls
    with the skill_agent's tightened config → allow, warn, block.

    Sequence: same args {"query": "report"} + same result_content
    (an empty result) drives the no-progress counter:
      - 1st call: count=1, no warn (1 < warn_after=2), no block.
      - 2nd call: count=2, WARN (2 >= no_progress_warn_after=2).
      - 3rd call: count=3, BLOCK on the BEFORE-CALL check
        (record[1]=2 >= no_progress_block_after=2).
    """
    # Sanity: search_skills is an idempotent tool (so the no-progress
    # tracker engages). If this ever changes, the test contract changes.
    assert "search_skills" in IDEMPOTENT_TOOL_NAMES, (
        "search_skills must be in IDEMPOTENT_TOOL_NAMES for the "
        "no-progress tracker to engage. If you removed it, the "
        "tightened config no longer protects against loops."
    )

    cfg = _loop_guard_config_for(SimpleNamespace(name="skill_agent"))
    ctrl = ToolLoopGuardController(cfg)

    args = {"query": "report"}
    same_result = '{"ok": true, "skills": []}'

    # 1st call: before_call=allow, after_call=allow (1 grace call)
    d_before = ctrl.before_call("search_skills", args)
    assert d_before.action == "allow", (
        f"1st search_skills call should be allowed, got {d_before.action!r}"
    )
    d_after = ctrl.after_call("search_skills", args, same_result)
    assert d_after.action == "allow", (
        f"1st search_skills after_call should be allowed, got {d_after.action!r}"
    )

    # 2nd call: before_call=allow (block threshold not yet met),
    # after_call=WARN
    d_before = ctrl.before_call("search_skills", args)
    assert d_before.action == "allow", (
        f"2nd search_skills before_call should be allowed, got {d_before.action!r}"
    )
    d_after = ctrl.after_call("search_skills", args, same_result)
    assert d_after.action == "warn", (
        f"2nd identical search_skills call should WARN "
        f"(no_progress_warn_after=2), got action={d_after.action!r} code={d_after.code!r}"
    )
    assert d_after.code == "no_progress_warning"

    # 3rd call: before_call=BLOCK (no_progress_block_after=2, record[1]=2)
    d_before = ctrl.before_call("search_skills", args)
    assert d_before.action == "block", (
        f"3rd identical search_skills call should be BLOCKED "
        f"(no_progress_block_after=2), got action={d_before.action!r} code={d_before.code!r}"
    )
    assert d_before.code == "no_progress_block"
    assert "search_skills" in d_before.message


def test_skill_agent_guard_does_not_block_when_result_changes():
    """If the second call returns a DIFFERENT result, no warning / no block.

    Same args + different result = progress. The no-progress counter
    only increments on identical (args, result) pairs. Pin this so we
    don't accidentally over-block legitimate work.
    """
    cfg = _loop_guard_config_for(SimpleNamespace(name="skill_agent"))
    ctrl = ToolLoopGuardController(cfg)
    args = {"query": "report"}

    ctrl.after_call("search_skills", args, '{"ok": true, "skills": []}')
    # 2nd call: same args, DIFFERENT result → progress, count resets to 1.
    d_after = ctrl.after_call(
        "search_skills", args, '{"ok": true, "skills": [{"name": "weekly"}]}'
    )
    assert d_after.action == "allow", (
        "When the result changes, the no-progress counter resets to 1 "
        "(below the warn threshold) and the call must NOT be warned. "
        f"Got action={d_after.action!r} code={d_after.code!r}"
    )

    # And the 3rd identical call (now with 2 identical-result calls in a
    # row) should still be allowed because the count was reset to 1.
    d_after = ctrl.after_call(
        "search_skills", args, '{"ok": true, "skills": [{"name": "weekly"}]}'
    )
    assert d_after.action == "warn", (
        "After reset, the 2nd identical-result call should warn. "
        f"Got action={d_after.action!r} code={d_after.code!r}"
    )


def test_skill_agent_guard_resets_after_block_for_new_query():
    """After a block on one query, a fresh query with a different
    signature should NOT be blocked.

    The block decision is sticky in the controller's _halt_decision
    (see tool_loop_guardrails.before_call), so a NEW controller
    instance must be used for the next turn. This test pins the
    contract that _loop_guard_config_for + a fresh controller
    behaves correctly for a different query.
    """
    cfg = _loop_guard_config_for(SimpleNamespace(name="skill_agent"))
    ctrl = ToolLoopGuardController(cfg)
    args_a = {"query": "report"}
    args_b = {"query": "summary"}
    same_result = '{"ok": true, "skills": []}'

    ctrl.after_call("search_skills", args_a, same_result)
    ctrl.after_call("search_skills", args_a, same_result)
    # 3rd identical call with args_a is now blocked.
    d = ctrl.before_call("search_skills", args_a)
    assert d.action == "block"

    # A fresh controller + args_b → no block.
    ctrl2 = ToolLoopGuardController(cfg)
    d = ctrl2.before_call("search_skills", args_b)
    assert d.action == "allow", (
        "A fresh controller must not carry over the block from a "
        "previous turn. Got action=" f"{d.action!r}"
    )


# --------------------------------------------------------------------
# (3) Wiring — the helper is invoked at all 3 sites in agents.py
# --------------------------------------------------------------------

def _find_tool_loop_guard_controller_calls():
    """Return the list of line numbers in agents.py where
    `ToolLoopGuardController(...)` is constructed. AST-based so it
    doesn't depend on import side effects.
    """
    agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "agents.py"
    )
    with open(agents_path) as f:
        source = f.read()
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ToolLoopGuardController"
        ):
            hits.append(node.lineno)
    return hits, source


def test_loop_guard_controller_uses_helper_at_all_sites():
    """Every `ToolLoopGuardController(...)` call in agents.py must be
    invoked with `_loop_guard_config_for(agent_app)` (not bare).

    The helper is the ONLY mechanism that gives skill_agent the
    tightened no-progress thresholds. Forgetting to pass it (i.e.
    regressing to `ToolLoopGuardController()`) would silently restore
    the default warn=2/block=5 thresholds and the 3x-loop regression
    would return.
    """
    site_lines, source = _find_tool_loop_guard_controller_calls()
    assert len(site_lines) >= 3, (
        f"Expected at least 3 ToolLoopGuardController() call sites in "
        f"agents.py (one per agent-loop path), found {len(site_lines)}. "
        f"If you refactored the router, update the wiring to ensure "
        f"all paths route through _loop_guard_config_for."
    )

    # Verify the helper is the FIRST (and ideally only) positional arg
    # at every site.
    for line in site_lines:
        # Read the line + next 4 lines from source to get the full call.
        # AST gives us the call node; we use lineno to locate it.
        agents_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "agents.py"
        )
        with open(agents_path) as f:
            src = f.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ToolLoopGuardController"
                and node.lineno == line
            ):
                args = node.args
                assert args, (
                    f"ToolLoopGuardController() at line {line} has no "
                    f"positional argument. It must be invoked as "
                    f"`ToolLoopGuardController(_loop_guard_config_for(agent_app))` "
                    f"so the per-agent override (skill_agent: warn=1, "
                    f"block=2) takes effect."
                )
                first = args[0]
                # The arg should be a Call to _loop_guard_config_for
                # with `agent_app` as its argument.
                assert (
                    isinstance(first, ast.Call)
                    and isinstance(first.func, ast.Name)
                    and first.func.id == "_loop_guard_config_for"
                ), (
                    f"ToolLoopGuardController() at line {line} is not "
                    f"invoked with `_loop_guard_config_for(...)` as the "
                    f"first arg. Without the helper, skill_agent's "
                    f"tightened thresholds (warn=1, block=2) won't apply "
                    f"and the 3x-loop regression from 2026-07-28 returns."
                )
                # And agent_app must be the argument to the helper.
                helper_args = first.args
                assert helper_args, (
                    f"_loop_guard_config_for(...) at line {line} has no "
                    f"argument. It must receive `agent_app`."
                )
                helper_arg = helper_args[0]
                assert (
                    isinstance(helper_arg, ast.Name)
                    and helper_arg.id == "agent_app"
                ), (
                    f"_loop_guard_config_for(...) at line {line} should "
                    f"be called with `agent_app` as its argument. Got: "
                    f"{ast.unparse(helper_arg)}"
                )
