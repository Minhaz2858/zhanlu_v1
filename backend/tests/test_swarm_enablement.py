"""Tests for multi-agent swarm enablement (2026-08-29).

The swarm subsystem (SwarmRuntime + SwarmOrchestrator + mailbox) was built
but dormant: the main chat agent's tool_config did not include the swarm_*
tools and AGENT_HARNESS_ENABLED was False.  Enablement = 3 parts:
1. swarm_* tools registered in the tool registry (toolset "swarm")
2. the system-agent seed + fallback list expose them to general_assistant
3. AGENT_HARNESS_ENABLED gates the harness execution path
"""

import pytest

SWARM_TOOLS = [
    "swarm_create_team", "swarm_spawn_agent", "swarm_send_message",
    "swarm_get_messages", "swarm_list_teams", "swarm_scratch_set",
    "swarm_scratch_get", "swarm_orchestrate",
]


def test_swarm_tools_registered_in_registry() -> None:
    from app.services.tool_handlers import swarm_tools  # noqa: F401  (self-registers)
    from app.services.tool_registry import registry

    for name in SWARM_TOOLS:
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} not registered"
        assert entry.toolset == "swarm", f"{name} wrong toolset: {entry.toolset}"
    # The orchestrator re-dispatch handler is wired.
    assert registry.get_handler("swarm_orchestrate") is not None


def test_system_agent_seed_exposes_swarm_tools() -> None:
    from app.services.system_agents import _build_system_agent_configs

    configs = _build_system_agent_configs()
    ga = next(c for c in configs if c["name"] == "general_assistant")
    enabled = ga["tool_config"]["enabled_tools"]
    for name in SWARM_TOOLS:
        assert name in enabled, f"general_assistant seed missing {name}"
    # power_user = all tools → swarm included via ALL_TOOL_NAMES.
    pu = next(c for c in configs if c["name"] == "power_user")
    pu_enabled = pu["tool_config"]["enabled_tools"]
    for name in SWARM_TOOLS:
        assert name in pu_enabled, f"power_user seed missing {name}"


def test_default_tools_fallback_includes_swarm() -> None:
    from app.services.tool_registry import DEFAULT_TOOLS_BY_AGENT

    enabled = DEFAULT_TOOLS_BY_AGENT["general_assistant"]
    for name in SWARM_TOOLS:
        assert name in enabled, f"DEFAULT_TOOLS_BY_AGENT missing {name}"


def test_resolve_tools_for_agent_returns_swarm_when_enabled() -> None:
    from app.services.tool_registry import resolve_tools_for_agent

    tools = resolve_tools_for_agent("general_assistant", {
        "enabled_tools": ["web_search", "swarm_create_team", "swarm_orchestrate"],
    })
    assert "swarm_create_team" in tools
    assert "swarm_orchestrate" in tools
    assert "web_search" in tools
    # Disabled filter still applies.
    tools2 = resolve_tools_for_agent("general_assistant", {
        "enabled_tools": SWARM_TOOLS,
        "disabled_tools": ["swarm_orchestrate"],
    })
    assert "swarm_orchestrate" not in tools2
    assert "swarm_create_team" in tools2


def test_harness_flag_defaults_and_env() -> None:
    """AGENT_HARNESS_ENABLED gates the SwarmRuntime harness path."""
    from app.config import settings

    # The runtime reads the flag (or accepts an explicit override).
    from app.services.swarm.runtime import SwarmRuntime
    import inspect

    src = inspect.getsource(SwarmRuntime.run)
    assert "AGENT_HARNESS_ENABLED" in src
    # .env enables it (dockerized setup).
    assert settings.AGENT_HARNESS_ENABLED is True


def test_team_registry_inprocess_state() -> None:
    """Teams, members, and scratch work in-process (messages go to DB)."""
    from app.services.swarm.team_registry import TeamRegistry

    reg = TeamRegistry()
    team = reg.create_team("unit-team", "desc")
    assert team.id and team.name == "unit-team"
    assert reg.get_team(team.id) is team
    assert "main" in team.members  # lead auto-added
    assert reg.add_member(team.id, "w1", role="worker")
    assert reg.get_team(team.id).members["w1"] == "worker"
    assert reg.list_teams() == [team]
    # Scratch space.
    assert reg.set_scratch(team.id, "k", "v") is True
    assert reg.get_scratch(team.id, "k") == "v"
    # Unknown team → no-ops.
    assert reg.send_message("nope", "a", "b", "c") is False
    assert reg.set_scratch("nope", "k", "v") is False


def test_swarm_coordinator_spawn_posts_result(monkeypatch) -> None:
    """spawn_agent runs the runtime and posts the final answer to the lead."""
    import asyncio

    from app.services.swarm.team_registry import SwarmCoordinator, TeamRegistry

    class _FakeRuntime:
        async def run(self, agent_name, task, llm_fn, tool_dispatcher,
                      db=None, user_id=None, member_name=None, max_iterations=8,
                      use_harness=None):
            from app.services.swarm.runtime import SwarmAgentResult
            return SwarmAgentResult(
                member_name=member_name or agent_name, agent_name=agent_name,
                task=task, final_response="42 is the answer", success=True,
            )

    reg = TeamRegistry()
    team = reg.create_team("coord-team")
    posted = {}
    monkeypatch.setattr(reg, "send_message", lambda *a, **kw: posted.update(a=a, kw=kw) or True)

    coord = SwarmCoordinator(registry=reg, runtime=_FakeRuntime())
    member = asyncio.run(coord.spawn_agent(team.id, "worker", "what is 2+2?", "w1"))
    assert member == "w1"
    assert reg.get_team(team.id).members["w1"] == "worker"
    assert posted  # final response posted to main
    assert posted["kw"]["recipient"] == "main"
    assert "42 is the answer" in posted["kw"]["content"]


def test_swarm_coordinator_run_task_matches_orchestrator_runner() -> None:
    """run_task returns a SwarmAgentResult (orchestrator Runner contract)."""
    import asyncio

    from app.services.swarm.team_registry import SwarmCoordinator, TeamRegistry

    class _FakeRuntime:
        async def run(self, *args, **kwargs):
            from app.services.swarm.runtime import SwarmAgentResult
            return SwarmAgentResult(
                member_name=kwargs.get("member_name") or "m",
                agent_name=kwargs.get("agent_name", "worker"),
                task=kwargs.get("task", ""), final_response="done", success=True,
            )

    coord = SwarmCoordinator(registry=TeamRegistry(), runtime=_FakeRuntime())
    result = asyncio.run(coord.run_task("t1", "worker", "do it", "w1"))
    assert result.success is True
    assert result.final_response == "done"
    assert result.member_name == "w1"


def test_orchestrator_redispatches_on_failure() -> None:
    """SwarmOrchestrator retries failed subtasks and escalates on the last
    retry — the value of swarm_orchestrate over raw spawn."""
    from app.services.swarm.orchestrator import (
        OrchestrationPolicy, OrchestratedTask, SwarmOrchestrator,
    )

    attempts = {"n": 0}

    async def flaky_runner(team_id, agent_name, task, member_name=None):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return type("R", (), {
                "member_name": member_name or agent_name, "agent_name": agent_name,
                "task": task, "success": False,
                "error": "boom", "final_response": "", "tool_calls": [],
            })()
        return type("R", (), {
            "member_name": member_name or agent_name, "agent_name": agent_name,
            "task": task, "success": True, "error": "",
            "final_response": "done", "tool_calls": [],
        })()

    class _FakeRegistry:
        def get_team(self, team_id):
            return None

        def add_member(self, team_id, member, role=None):
            return None

    class _FakeCoordinator:
        def __init__(self):
            self.registry = _FakeRegistry()  # _post_summary no-ops on missing team

        async def run_task(self, *a, **kw):
            return await flaky_runner(*a, **kw)

    async def go():
        orch = SwarmOrchestrator(coordinator=_FakeCoordinator())
        results = await orch.orchestrate(
            "team-1",
            [OrchestratedTask(agent_name="worker", task="do it", member_name="w1")],
            policy=OrchestrationPolicy(max_retries=1),
            runner=flaky_runner,
        )
        return results[0]

    import asyncio
    result = asyncio.run(go())
    assert result.success is True
    assert attempts["n"] == 2  # initial + one re-dispatch
