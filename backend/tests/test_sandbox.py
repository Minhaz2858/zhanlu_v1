"""Tests for the sandbox execution system.

Covers:
- Resource limits configuration
- Runtime parsing
- Sandbox runner (Docker + fallback modes)
- sandbox_code tool handler
- Runtime frontmatter lookup from DB rows
- SkillMetadata runtime field
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Resource Limits Tests
# ---------------------------------------------------------------------------

class TestResourceLimits:
    """Test resource_limits.py configuration."""

    def test_default_limits(self):
        from app.services.sandbox.resource_limits import (
            get_resource_limits,
            SandboxResourceLimits,
        )
        limits = get_resource_limits(None)
        assert isinstance(limits, SandboxResourceLimits)
        assert limits.memory == "256m"
        assert limits.cpus == "1"
        assert limits.timeout == 120

    def test_python_limits(self):
        from app.services.sandbox.resource_limits import get_resource_limits
        limits = get_resource_limits("python")
        assert limits.memory == "256m"

    def test_python_2g_limits(self):
        from app.services.sandbox.resource_limits import get_resource_limits
        limits = get_resource_limits("python-2g")
        assert limits.memory == "2g"
        assert limits.tmpfs_size == "1g"

    def test_python_3_12_falls_to_python(self):
        from app.services.sandbox.resource_limits import get_resource_limits
        limits = get_resource_limits("python-3.12")
        assert limits.memory == "256m"

    def test_node_limits(self):
        from app.services.sandbox.resource_limits import get_resource_limits
        limits = get_resource_limits("node")
        assert limits.memory == "512m"

    def test_bash_limits(self):
        from app.services.sandbox.resource_limits import get_resource_limits
        limits = get_resource_limits("bash")
        assert limits.memory == "128m"
        assert limits.timeout == 60

    def test_unknown_runtime(self):
        from app.services.sandbox.resource_limits import get_resource_limits
        limits = get_resource_limits("unknown-runtime")
        assert limits.memory == "256m"  # fallback default

    def test_parse_runtime_info_python_3_12_2g(self):
        from app.services.sandbox.resource_limits import parse_runtime_info
        info = parse_runtime_info("python-3.12-2g")
        assert info["engine"] == "python"
        assert info["version"] == "3.12"
        assert info["memory"] == "2g"

    def test_parse_runtime_info_node_1g(self):
        from app.services.sandbox.resource_limits import parse_runtime_info
        info = parse_runtime_info("node-1g")
        assert info["engine"] == "node"

    def test_parse_runtime_info_none(self):
        from app.services.sandbox.resource_limits import parse_runtime_info
        info = parse_runtime_info(None)
        assert info["engine"] == "python"
        assert info["memory"] == "256m"

    def test_get_runtime_image(self):
        from app.services.sandbox.resource_limits import get_runtime_image
        assert "python" in get_runtime_image("python").lower()
        assert "python" in get_runtime_image(None).lower()


# ---------------------------------------------------------------------------
# Sandbox Runner Tests
# ---------------------------------------------------------------------------

class TestSandboxRunner:
    """Test runner.py execution modes."""

    @patch("app.services.sandbox.runner.is_docker_available", return_value=False)
    async def test_fallback_python_execution(self, mock_docker_avail):
        """When Docker is unavailable, fallback to subprocess."""
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="print('hello sandbox')",
            runtime="python",
            timeout=10,
        )
        assert result["execution_mode"] == "fallback"
        assert result["runtime"] == "python"

    @patch("app.services.sandbox.runner.is_docker_available", return_value=False)
    async def test_fallback_code_output(self, mock_docker_avail):
        """Fallback mode captures stdout correctly."""
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="print('answer: 42')",
            runtime="python",
            timeout=10,
        )
        assert result["exit_code"] == 0
        assert "answer: 42" in result["stdout"]

    @patch("app.services.sandbox.runner.is_docker_available", return_value=False)
    async def test_fallback_runtime_error(self, mock_docker_avail):
        """Fallback mode captures runtime errors."""
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="raise ValueError('test error')",
            runtime="python",
            timeout=10,
        )
        assert result["exit_code"] != 0
        assert "ValueError" in result["stderr"]

    @patch("app.services.sandbox.runner.is_docker_available", return_value=False)
    async def test_fallback_timeout(self, mock_docker_avail):
        """Fallback mode handles timeout correctly."""
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="import time; time.sleep(30)",
            runtime="python",
            timeout=1,
        )
        assert not result["success"]

    @patch("app.services.sandbox.runner.is_docker_available", return_value=True)
    @patch("app.services.sandbox.runner.run_sandbox_container")
    async def test_docker_mode_success(self, mock_run, mock_docker):
        """Docker mode returns successful result."""
        mock_run.return_value = {
            "exit_code": 0,
            "stdout": "hello from docker",
            "stderr": "",
            "duration_ms": 120,
        }
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="print('hello from docker')",
            runtime="python",
            timeout=10,
        )
        assert result["execution_mode"] == "docker"
        assert result["success"] is True
        assert "hello from docker" in result["stdout"]

    @patch("app.services.sandbox.runner.is_docker_available", return_value=True)
    @patch("app.services.sandbox.runner.run_sandbox_container")
    async def test_docker_mode_failure(self, mock_run, mock_docker):
        """Docker mode returns failure when container errors."""
        mock_run.return_value = {
            "exit_code": 1,
            "stdout": "",
            "stderr": "NameError: name 'x' is not defined",
            "duration_ms": 80,
        }
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="print(x)",
            runtime="python",
            timeout=10,
        )
        assert result["success"] is False
        assert "NameError" in result["stderr"]

    @patch("app.services.sandbox.runner.is_docker_available", return_value=True)
    @patch("app.services.sandbox.runner.run_sandbox_container")
    async def test_docker_with_runtime_override(self, mock_run, mock_docker):
        """Docker mode uses correct image for non-default runtime."""
        mock_run.return_value = {
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "duration_ms": 50,
        }
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="console.log('hi')",
            runtime="node",
            timeout=5,
        )
        assert result["execution_mode"] == "docker"

    @patch("app.services.sandbox.runner.is_docker_available", return_value=False)
    async def test_fallback_import_restriction(self, mock_docker):
        """Fallback mode blocks unsafe imports."""
        from app.services.sandbox.runner import execute_in_sandbox
        result = await execute_in_sandbox(
            code="import os; print(os.getcwd())",
            runtime="python",
            timeout=5,
        )
        assert result["exit_code"] != 0


# ---------------------------------------------------------------------------
# sandbox_code Tool Handler Tests
# ---------------------------------------------------------------------------

class TestSandboxCodeHandler:
    """Test the sandbox_code tool handler."""

    async def test_missing_code(self):
        """Returns error when code is empty."""
        from app.services.tool_handlers.sandbox_code import _sandbox_code
        result = await _sandbox_code({"code": ""}, db=None)
        assert result["success"] is False
        assert "required" in result["error"]

    @patch("app.services.sandbox.runner.execute_in_sandbox")
    async def test_successful_execution(self, mock_exec):
        """Successful execution returns stdout."""
        mock_exec.return_value = {
            "success": True,
            "stdout": "42\n",
            "stderr": "",
            "exit_code": 0,
            "execution_mode": "fallback",
            "runtime": "python",
        }
        from app.services.tool_handlers.sandbox_code import _sandbox_code
        result = await _sandbox_code({"code": "print(42)"}, db=None)
        assert result["success"] is True
        assert "42" in result["stdout"]

    @patch("app.services.sandbox.runner.execute_in_sandbox")
    async def test_with_runtime_override(self, mock_exec):
        """Runtime override is passed through."""
        mock_exec.return_value = {
            "success": True, "stdout": "ok", "stderr": "",
            "exit_code": 0, "execution_mode": "docker", "runtime": "node",
        }
        from app.services.tool_handlers.sandbox_code import _sandbox_code
        result = await _sandbox_code(
            {"code": "console.log('hi')", "runtime": "node"},
            db=None,
        )
        assert result["runtime"] == "node"

    @patch("app.services.sandbox.runner.execute_in_sandbox")
    async def test_execution_error(self, mock_exec):
        """Execution error returns failed result."""
        mock_exec.return_value = {
            "success": False,
            "stdout": "",
            "stderr": "SyntaxError: invalid syntax",
            "exit_code": 1,
            "execution_mode": "fallback",
            "runtime": "python",
        }
        from app.services.tool_handlers.sandbox_code import _sandbox_code
        result = await _sandbox_code({"code": "bad syntax!!!"}, db=None)
        assert result["success"] is False
        assert "SyntaxError" in result["stderr"]

    def test_registry_entry(self):
        """sandbox_code is registered in ToolRegistry."""
        from app.services.tool_registry import registry
        assert "sandbox_code" in registry.list_available()


# ---------------------------------------------------------------------------
# Runtime Frontmatter Lookup Tests
# ---------------------------------------------------------------------------

class TestRuntimeFrontmatter:
    """Test runtime frontmatter parsing and DB lookup."""

    def test_skill_metadata_runtime_field(self):
        """SkillMetadata includes runtime from frontmatter."""
        from app.services.skills_loader import SkillMetadata
        skill = SkillMetadata(
            name="test-skill",
            description="A test",
            file_path="/test/SKILL.md",
            runtime="python-2g",
        )
        assert skill.runtime == "python-2g"
        assert skill.to_dict()["runtime"] == "python-2g"

    def test_parse_skill_file_with_runtime(self, tmp_path):
        """parse_skill_file extracts runtime from frontmatter."""
        import tempfile

        skill_md_content = """---
name: data-analyzer
description: Analyze data with pandas
runtime: python-2g
version: "2.0"
author: test-author
---

# Data Analyzer
Use pandas to analyze the provided data.
"""
        # Write to a temp dir matching skills_loader structure
        skill_dir = tmp_path / "data-science"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(skill_md_content)

        from app.services.skills_loader import parse_skill_file
        skill = parse_skill_file(skill_file, source="test")
        assert skill is not None
        assert skill.runtime == "python-2g"

    def test_parse_skill_file_no_runtime(self, tmp_path):
        """parse_skill_file returns empty runtime when not specified."""
        skill_md_content = """---
name: simple-skill
description: Nothing fancy
---

Just do it.
"""
        skill_dir = tmp_path / "general"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(skill_md_content)

        from app.services.skills_loader import parse_skill_file
        skill = parse_skill_file(skill_file, source="test")
        assert skill is not None
        assert skill.runtime == ""

    def test_write_skill_md_includes_runtime(self, tmp_path, monkeypatch):
        """write_skill_md includes runtime in frontmatter when specified."""
        monkeypatch.setattr(
            "app.services.skill_sync.USER_SKILLS_DIR",
            tmp_path,
        )
        from app.services.skill_sync import write_skill_md

        path = write_skill_md(
            name="ml-skill",
            description="ML processing",
            body="Use pandas for ML.",
            runtime="python-2g",
        )
        content = (tmp_path / "custom" / "ml-skill" / "SKILL.md").read_text()
        assert "runtime: python-2g" in content

    def test_write_skill_md_no_runtime(self, tmp_path, monkeypatch):
        """write_skill_md omits runtime when not specified."""
        monkeypatch.setattr(
            "app.services.skill_sync.USER_SKILLS_DIR",
            tmp_path,
        )
        from app.services.skill_sync import write_skill_md

        path = write_skill_md(
            name="simple-skill",
            description="Simple",
            body="Print hello.",
        )
        content = (tmp_path / "custom" / "simple-skill" / "SKILL.md").read_text()
        assert "runtime:" not in content

    def test_lookup_skill_runtime_from_db(self):
        """_lookup_skill_runtime extracts runtime from DB row."""
        # Mock a DB row
        mock_row = MagicMock()
        mock_row.skill_md = """---
name: heavy-skill
runtime: python-2g
---

Heavy processing.
"""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_row

        from app.services.tool_handlers.sandbox_code import _lookup_skill_runtime
        runtime = _lookup_skill_runtime(mock_db, "heavy-skill")
        assert runtime == "python-2g"

    def test_lookup_skill_runtime_not_found(self):
        """_lookup_skill_runtime returns None when skill has no runtime."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        from app.services.tool_handlers.sandbox_code import _lookup_skill_runtime
        runtime = _lookup_skill_runtime(mock_db, "nonexistent")
        assert runtime is None


# ---------------------------------------------------------------------------
# execute_code compatibility test
# ---------------------------------------------------------------------------

class TestExecuteCodeStillWorks:
    """Verify the original execute_code tool still works after sandbox additions."""

    async def test_execute_code_simple(self):
        """Original execute_code runs simple Python."""
        from app.services.tool_handlers.code_execution_tool import _execute_code
        result = await _execute_code({"code": "print('hello')"}, db=None, user_id=None)
        assert result["success"] is True
        assert "hello" in result["stdout"]

    async def test_execute_code_math(self):
        """Original execute_code handles math operations."""
        from app.services.tool_handlers.code_execution_tool import _execute_code
        result = await _execute_code(
            {"code": "result = 2 + 2\nprint(result)"},
            db=None, user_id=None,
        )
        assert result["success"] is True
        assert "4" in result["stdout"]

    async def test_execute_code_error(self):
        """Original execute_code captures execution errors."""
        from app.services.tool_handlers.code_execution_tool import _execute_code
        result = await _execute_code(
            {"code": "1/0"},
            db=None, user_id=None,
        )
        assert result["success"] is False
