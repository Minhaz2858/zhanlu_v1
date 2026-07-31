"""Regression test for the broken ``skills(load)`` tool action.

Root cause: ``backend/app/services/tool_handlers/skills_tool.py`` calls
``from app.services.skills_loader import load_skill`` for the ``load``
and ``execute`` actions. The actual function in
``app/services/skills_loader/__init__.py`` is named ``get_skill``
(not ``load_skill``), so the import fails with::

    ImportError: cannot import name 'load_skill' from
    'app.services.skills_loader'

Every call to ``skills(action=load, name=...)`` therefore returns
``{"success": False, "error": "Load failed: ..."}`` regardless of
the skill name. This is what makes the ``agent_builder`` get stuck
investigating skills and never call ``create_agent`` to actually
build the agent — the symptom the user is reporting as "Agent
Builder can ask simple question... not very detail".

This test pins down the fix: a call to
``skills(action=load, name=<known-skill>)`` must return
``{"success": True, "name": ..., "content": ...}`` (or the equivalent
new shape — anything other than ``success=False`` with a backend
import error).
"""
import asyncio
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _run(coro):
    """Run an async coroutine and return its result."""
    return asyncio.run(coro)


def test_skills_load_does_not_raise_import_error():
    """The skills(load) action must not return a backend ImportError.
    Real "skill not found" is fine; the broken-import error string
    is the bug signature."""
    from app.services.tool_handlers.skills_tool import _skills_tool
    result = _run(_skills_tool({"action": "load", "name": "_nonexistent_test_skill_"}, db=None))
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    err = result.get("error") or ""
    assert "load_skill" not in err, (
        f"skills(load) is hitting the broken import: {err!r}. "
        f"The function should use the correct API (get_skill / "
        f"get_skill_prompt_for_agent) from app.services.skills_loader."
    )
    if result.get("success") is False:
        assert "cannot import" not in err.lower(), (
            f"skills(load) returns the import error: {err!r}"
        )


def test_skills_execute_does_not_raise_import_error():
    """Same as above for the ``execute`` action, which also imports
    ``load_skill``."""
    from app.services.tool_handlers.skills_tool import _skills_tool
    result = _run(_skills_tool({"action": "execute", "name": "_nonexistent_test_skill_"}, db=None))
    err = result.get("error") or ""
    assert "load_skill" not in err, (
        f"skills(execute) is hitting the broken import: {err!r}"
    )
