"""Tests for the dynamic prompt builder."""
import os
import sys
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.dynamic_prompt_builder import build_system_prompt


def test_base_prompt_only():
    """With all injections disabled, returns just the base prompt."""
    result = build_system_prompt(
        base_prompt="You are a helpful assistant.",
        db=MagicMock(),
        agent_app_id="test",
        conversation_id="conv",
        inject_memory=False,
        inject_todos=False,
        inject_coding_context=False,
        inject_learning_graph=False,
    )
    assert result == "You are a helpful assistant."


def test_memory_injection():
    """Memory snapshot is appended to the prompt."""
    db = MagicMock()
    with patch("app.services.tool_handlers.memory_tool.load_memory_snapshot",
               return_value={"memory": "[Memory] User likes Python", "user": ""}):
        result = build_system_prompt(
            base_prompt="Base.",
            db=db,
            agent_app_id="test",
            conversation_id="conv",
            inject_todos=False,
            inject_coding_context=False,
            inject_learning_graph=False,
        )
    assert "Base." in result
    assert "[Memory] User likes Python" in result


def test_todo_injection():
    """Todo list is appended to the prompt."""
    db = MagicMock()
    with patch("app.services.tool_handlers.todo_tool.load_todo_snapshot",
               return_value="[Todo] 1. Write tests"):
        result = build_system_prompt(
            base_prompt="Base.",
            db=db,
            agent_app_id="test",
            conversation_id="conv",
            inject_memory=False,
            inject_coding_context=False,
            inject_learning_graph=False,
        )
    assert "Base." in result
    assert "[Todo] 1. Write tests" in result


def test_coding_context_injection():
    """Coding context is appended to the prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from pathlib import Path
        Path(tmpdir, "pyproject.toml").write_text("[tool.pytest]")
        result = build_system_prompt(
            base_prompt="Base.",
            db=MagicMock(),
            agent_app_id="test",
            conversation_id="conv",
            inject_memory=False,
            inject_todos=False,
            workspace_path=tmpdir,
            inject_learning_graph=False,
        )
    assert "Base." in result
    assert "python" in result.lower()
    assert "pytest" in result.lower()


def test_learning_graph_injection():
    """Learning graph is appended to the prompt."""
    with tempfile.TemporaryDirectory() as tmpdir, \
         patch("app.services.learning_graph._DEFAULT_STORAGE_DIR", tmpdir):
        from app.services.learning_graph import record_learning
        record_learning("test-agent", "use pytest -v", "success",
                        confidence_boost=0.5, storage_dir=tmpdir)

        result = build_system_prompt(
            base_prompt="Base.",
            db=MagicMock(),
            agent_app_id="test-agent",
            conversation_id="conv",
            inject_memory=False,
            inject_todos=False,
            inject_coding_context=False,
        )
    assert "Base." in result
    assert "pytest" in result
    assert "Learned from past sessions" in result


def test_all_injections_combined():
    """All injections work together."""
    import tempfile
    db = MagicMock()
    with patch("app.services.tool_handlers.memory_tool.load_memory_snapshot",
               return_value={"memory": "[Memory] test", "user": ""}), \
         patch("app.services.tool_handlers.todo_tool.load_todo_snapshot",
               return_value="[Todo] test"), \
         tempfile.TemporaryDirectory() as tmpdir:
        from pathlib import Path
        Path(tmpdir, "pyproject.toml").write_text("[tool.pytest]")

        result = build_system_prompt(
            base_prompt="Base prompt.",
            db=db,
            agent_app_id="test",
            conversation_id="conv",
            workspace_path=tmpdir,
            inject_memory=True,
            inject_todos=True,
            inject_coding_context=True,
            inject_learning_graph=False,  # no learnings stored
        )
    assert "Base prompt." in result
    assert "[Memory] test" in result
    assert "[Todo] test" in result
    assert "python" in result.lower()


def test_injection_failure_is_non_fatal():
    """A failure in any injection doesn't crash the builder."""
    with patch("app.services.tool_handlers.memory_tool.load_memory_snapshot",
               side_effect=Exception("DB error")):
        result = build_system_prompt(
            base_prompt="Base.",
            db=MagicMock(),
            agent_app_id="test",
            conversation_id="conv",
            inject_memory=True,
            inject_todos=False,
            inject_coding_context=False,
            inject_learning_graph=False,
        )
    # Should still return the base prompt
    assert result == "Base."


# Need tempfile import at module level for test_coding_context_injection
import tempfile
