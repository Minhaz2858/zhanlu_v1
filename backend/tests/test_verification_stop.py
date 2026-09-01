"""Tests for verification-on-stop."""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.verification_stop import (
    build_verify_on_stop_nudge,
    extract_written_file_paths,
    has_verification_after_last_write,
    _is_non_code_path,
    _filter_verifiable_paths,
)


def _assistant_with_write(path: str) -> dict:
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "1", "type": "function",
                        "function": {"name": "write_file",
                                     "arguments": json.dumps({"path": path, "content": "x"})}}],
    }


def _assistant_with_exec() -> dict:
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "2", "type": "function",
                        "function": {"name": "execute_code",
                                     "arguments": json.dumps({"language": "python", "code": "1+1"})}}],
    }


def _tool_result(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": '{"success": true}'}


def test_extract_written_file_paths():
    """Extracts file paths from write_file tool calls."""
    messages = [
        {"role": "user", "content": "write a file"},
        _assistant_with_write("/a/test.py"),
        _tool_result("1"),
    ]
    paths = extract_written_file_paths(messages)
    assert paths == ["/a/test.py"]


def test_extract_written_file_paths_multiple():
    """Extracts paths from multiple write_file calls."""
    messages = [
        _assistant_with_write("/a.py"),
        _tool_result("1"),
        _assistant_with_write("/b.py"),
        _tool_result("1"),
    ]
    paths = extract_written_file_paths(messages)
    assert paths == ["/a.py", "/b.py"]


def test_extract_written_file_paths_none():
    """Returns empty list when no write_file calls."""
    messages = [{"role": "user", "content": "hello"}]
    assert extract_written_file_paths(messages) == []


def test_has_verification_after_last_write_true():
    """Returns True when execute_code was called after write_file."""
    messages = [
        _assistant_with_write("/a.py"),
        _tool_result("1"),
        _assistant_with_exec(),
        _tool_result("2"),
    ]
    assert has_verification_after_last_write(messages) is True


def test_has_verification_after_last_write_false():
    """Returns False when no verification after write_file."""
    messages = [
        _assistant_with_write("/a.py"),
        _tool_result("1"),
        {"role": "assistant", "content": "Done!"},
    ]
    assert has_verification_after_last_write(messages) is False


def test_has_verification_no_writes():
    """Returns True when there are no writes at all."""
    messages = [{"role": "user", "content": "hello"}]
    assert has_verification_after_last_write(messages) is True


def test_is_non_code_path_markdown():
    """Markdown files are non-code."""
    assert _is_non_code_path("/a/README.md") is True
    assert _is_non_code_path("/a/docs.text") is True


def test_is_non_code_path_python():
    """Python files are code."""
    assert _is_non_code_path("/a/test.py") is False


def test_is_non_code_path_license():
    """LICENSE file (no extension) is non-code."""
    assert _is_non_code_path("/a/LICENSE") is True


def test_filter_verifiable_paths():
    """Drops non-code paths, keeps code paths."""
    paths = ["/a/test.py", "/b/README.md", "/c/main.ts", "/d/LICENSE"]
    filtered = _filter_verifiable_paths(paths)
    assert "/a/test.py" in filtered
    assert "/c/main.ts" in filtered
    assert "/b/README.md" not in filtered
    assert "/d/LICENSE" not in filtered


def test_nudge_fires_on_unverified_code_edit():
    """Nudge fires when code was written but not verified."""
    messages = [
        {"role": "user", "content": "write a python file"},
        _assistant_with_write("/a/test.py"),
        _tool_result("1"),
        {"role": "assistant", "content": "Done!"},
    ]
    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is not None
    assert "verification" in nudge.lower() or "verify" in nudge.lower()


def test_nudge_suppressed_after_verification():
    """No nudge when execute_code was called after write_file."""
    messages = [
        {"role": "user", "content": "write a file and test it"},
        _assistant_with_write("/a/test.py"),
        _tool_result("1"),
        _assistant_with_exec(),
        _tool_result("2"),
        {"role": "assistant", "content": "Done!"},
    ]
    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is None


def test_nudge_suppressed_for_docs_only():
    """No nudge when only documentation files were edited."""
    messages = [
        {"role": "user", "content": "update the README"},
        _assistant_with_write("/a/README.md"),
        _tool_result("1"),
        {"role": "assistant", "content": "Done!"},
    ]
    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is None


def test_nudge_suppressed_at_max_attempts():
    """No nudge when attempts >= max_attempts."""
    messages = [
        _assistant_with_write("/a/test.py"),
        _tool_result("1"),
        {"role": "assistant", "content": "Done!"},
    ]
    nudge = build_verify_on_stop_nudge(messages, attempts=2, max_attempts=2)
    assert nudge is None


def test_nudge_contains_changed_paths():
    """The nudge includes the changed file paths."""
    messages = [
        _assistant_with_write("/a/test.py"),
        _tool_result("1"),
        {"role": "assistant", "content": "Done!"},
    ]
    nudge = build_verify_on_stop_nudge(messages)
    assert nudge is not None
    assert "/a/test.py" in nudge
