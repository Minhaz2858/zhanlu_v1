"""Tests for coding context -- project fact detection."""
import json
import os
import sys
import tempfile
from pathlib import Path

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.coding_context import (
    ProjectFacts,
    detect_project_facts,
    get_test_command_for_files,
)


def test_detect_python_project():
    """Detects Python project with pyproject.toml."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "pyproject.toml").write_text("[tool.pytest]\n")
        facts = detect_project_facts(tmpdir)
        assert "python" in facts.languages
        assert "pytest" in facts.test_commands
        assert "ruff check ." in facts.lint_commands


def test_detect_rust_project():
    """Detects Rust project with Cargo.toml."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "Cargo.toml").write_text("[package]\nname = \"test\"\n")
        facts = detect_project_facts(tmpdir)
        assert "rust" in facts.languages
        assert "cargo test" in facts.test_commands
        assert "cargo build" in facts.build_commands
        assert "cargo clippy" in facts.lint_commands


def test_detect_go_project():
    """Detects Go project with go.mod."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "go.mod").write_text("module test\n")
        facts = detect_project_facts(tmpdir)
        assert "go" in facts.languages
        assert "go test ./..." in facts.test_commands


def test_detect_npm_project():
    """Detects npm project with package.json scripts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = {
            "scripts": {
                "test": "jest",
                "build": "webpack",
                "lint": "eslint .",
            },
            "devDependencies": {"typescript": "^5.0", "jest": "^29.0"},
        }
        Path(tmpdir, "package.json").write_text(json.dumps(pkg))
        facts = detect_project_facts(tmpdir)
        assert "typescript" in facts.languages
        assert "npm test" in facts.test_commands
        assert "npm run build" in facts.build_commands
        assert "npm run lint" in facts.lint_commands
        assert facts.package_manager == "npm"


def test_detect_makefile_project():
    """Detects Makefile-based project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "Makefile").write_text("test:\n\tpytest\n")
        facts = detect_project_facts(tmpdir)
        assert "make" in facts.languages
        assert "make test" in facts.test_commands


def test_detect_empty_directory():
    """Empty directory returns empty facts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        facts = detect_project_facts(tmpdir)
        assert facts.languages == []
        assert facts.test_commands == []


def test_detect_nonexistent_directory():
    """Nonexistent directory returns empty facts."""
    facts = detect_project_facts("/nonexistent/path/12345")
    assert facts.languages == []
    assert facts.test_commands == []


def test_detect_multi_language_project():
    """Detects multiple languages in a polyglot project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "pyproject.toml").write_text("[tool.pytest]\n")
        Path(tmpdir, "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        facts = detect_project_facts(tmpdir)
        assert "python" in facts.languages
        assert "javascript" in facts.languages or "typescript" in facts.languages
        assert "pytest" in facts.test_commands
        assert "npm test" in facts.test_commands


def test_get_test_command_python():
    """Returns pytest for Python files."""
    facts = ProjectFacts(languages=["python"], test_commands=["pytest"])
    cmd = get_test_command_for_files(["/src/test_app.py", "/src/main.py"], facts)
    assert cmd == "pytest"


def test_get_test_command_javascript():
    """Returns npm test for JS files."""
    facts = ProjectFacts(languages=["typescript"], test_commands=["npm test", "npx jest"])
    cmd = get_test_command_for_files(["/src/app.tsx", "/src/test.ts"], facts)
    assert "npm" in cmd or "jest" in cmd


def test_get_test_command_rust():
    """Returns cargo test for Rust files."""
    facts = ProjectFacts(languages=["rust"], test_commands=["cargo test"])
    cmd = get_test_command_for_files(["/src/lib.rs"], facts)
    assert cmd == "cargo test"


def test_get_test_command_no_commands():
    """Returns None when no test commands available."""
    facts = ProjectFacts(languages=["python"])
    cmd = get_test_command_for_files(["/src/test.py"], facts)
    assert cmd is None


def test_get_test_command_fallback():
    """Falls back to primary test command for unknown file types."""
    facts = ProjectFacts(languages=["make"], test_commands=["make test"])
    cmd = get_test_command_for_files(["/src/unknown.xyz"], facts)
    assert cmd == "make test"


def test_primary_test_command():
    """primary_test_command returns the first test command."""
    facts = ProjectFacts(test_commands=["pytest", "npm test"])
    assert facts.primary_test_command == "pytest"


def test_primary_test_command_empty():
    """primary_test_command returns None when no commands."""
    facts = ProjectFacts()
    assert facts.primary_test_command is None
