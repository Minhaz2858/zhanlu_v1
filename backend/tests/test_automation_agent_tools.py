"""Tool-config tests for the expanded automation_agent.

Verifies:
  - The automation_agent config includes ``clarify``,
    ``execute_automation``, and ``list_data_sources`` in enabled_tools.
  - The manifest boundaries ALLOW ``execute_automation`` (previously
    forbidden) and reflect the create+fix+run scope.
  - The automation_agent stays ``is_system=True`` (hidden from the
    agent picker).
"""
import os, sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _configs():
    from app.services.system_agents import _build_system_agent_configs
    return _build_system_agent_configs(registry=None)


def _automation_config():
    for c in _configs():
        if c.get("name") == "automation_agent":
            return c
    raise AssertionError("automation_agent config not found")


def test_automation_agent_has_clarify_tool():
    cfg = _automation_config()
    tools = cfg["tool_config"]["enabled_tools"]
    assert "clarify" in tools, f"clarify not in automation_agent tools: {tools}"


def test_automation_agent_has_execute_automation_tool():
    cfg = _automation_config()
    tools = cfg["tool_config"]["enabled_tools"]
    assert "execute_automation" in tools, (
        f"execute_automation not in automation_agent tools: {tools}"
    )


def test_automation_agent_has_list_data_sources_tool():
    cfg = _automation_config()
    tools = cfg["tool_config"]["enabled_tools"]
    assert "list_data_sources" in tools, (
        f"list_data_sources not in automation_agent tools: {tools}"
    )


def test_automation_agent_keeps_core_tools():
    cfg = _automation_config()
    tools = cfg["tool_config"]["enabled_tools"]
    for required in ("create_automation", "update_automation", "list_knowledge_bases"):
        assert required in tools, f"{required} missing from automation_agent tools"


def test_manifest_allows_execute_automation():
    cfg = _automation_config()
    manifest = cfg["manifest_json"]
    allowed = manifest["boundaries"]["allowed"]
    assert "execute_automation" in allowed, (
        f"execute_automation not in manifest allowed: {allowed}"
    )
    forbidden = manifest["boundaries"]["forbidden"]
    assert "execute_automation" not in forbidden, (
        f"execute_automation still in manifest forbidden: {forbidden}"
    )


def test_manifest_forbids_report_generation():
    """The no-report boundary must be reflected in the manifest:
    report_generation and data_analysis are forbidden."""
    cfg = _automation_config()
    manifest = cfg["manifest_json"]
    forbidden = manifest["boundaries"]["forbidden"]
    assert "report_generation" in forbidden
    assert "data_analysis" in forbidden


def test_manifest_task_scope_includes_fix_and_run():
    cfg = _automation_config()
    manifest = cfg["manifest_json"]
    scope = manifest["task_scope"]
    assert "automation_creation" in scope
    assert "automation_fix" in scope
    assert "automation_run" in scope


def test_automation_agent_is_system():
    cfg = _automation_config()
    assert cfg.get("is_system") is True, (
        "automation_agent must be is_system=True (hidden from picker)"
    )
