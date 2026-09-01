"""Tests for tool result classification."""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_result_classification import (
    FILE_MUTATING_TOOL_NAMES,
    NO_EFFECT_TOOL_NAMES,
    tool_may_have_side_effect,
    is_file_mutating_tool,
    file_mutation_result_landed,
    is_safe_to_discard_on_interrupt,
)


def test_read_file_has_no_side_effect():
    assert tool_may_have_side_effect("read_file") is False


def test_web_search_has_no_side_effect():
    assert tool_may_have_side_effect("web_search") is False


def test_write_file_has_side_effect():
    assert tool_may_have_side_effect("write_file") is True


def test_execute_code_has_side_effect():
    assert tool_may_have_side_effect("execute_code") is True


def test_is_file_mutating_tool():
    assert is_file_mutating_tool("write_file") is True
    assert is_file_mutating_tool("read_file") is False
    assert is_file_mutating_tool("execute_code") is False


def test_file_mutation_landed_success():
    result = {"success": True, "path": "/a.txt"}
    assert file_mutation_result_landed("write_file", result) is True


def test_file_mutation_not_landed_failure():
    result = {"success": False, "error": "permission denied"}
    assert file_mutation_result_landed("write_file", result) is False


def test_file_mutation_not_landed_non_file_tool():
    result = {"success": True}
    assert file_mutation_result_landed("read_file", result) is False


def test_file_mutation_landed_json_string():
    result_str = json.dumps({"success": True, "path": "/a.txt"})
    assert file_mutation_result_landed("write_file", result_str) is True


def test_file_mutation_landed_invalid_json():
    assert file_mutation_result_landed("write_file", "not json") is False


def test_safe_to_discard_read_file():
    assert is_safe_to_discard_on_interrupt("read_file") is True


def test_safe_to_discard_write_file():
    assert is_safe_to_discard_on_interrupt("write_file") is False


def test_safe_to_discard_execute_code():
    assert is_safe_to_discard_on_interrupt("execute_code") is False


def test_no_effect_tools_set():
    assert "read_file" in NO_EFFECT_TOOL_NAMES
    assert "web_search" in NO_EFFECT_TOOL_NAMES
    assert "interrupt" in NO_EFFECT_TOOL_NAMES
    assert "write_file" not in NO_EFFECT_TOOL_NAMES


def test_safe_to_discard_interrupt():
    """The LLM-facing interrupt tool is pure no-effect (the flag is not
    polled by the v3 loop), so its result is safe to discard on interrupt."""
    assert is_safe_to_discard_on_interrupt("interrupt") is True


def test_file_mutating_tools_set():
    assert "write_file" in FILE_MUTATING_TOOL_NAMES
    assert "read_file" not in FILE_MUTATING_TOOL_NAMES
