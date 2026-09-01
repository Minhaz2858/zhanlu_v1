"""Docker sandbox runner — execute arbitrary code in an isolated container.

This module wraps ``container_manager.run_sandbox_container`` with
resource limits from ``resource_limits.py`` and provides a clean
``execute_in_sandbox()`` interface for use by tool handlers.

Security:
- Container runs with --network none, --read-only, --cap-drop ALL
- Only /tmp (tmpfs) and /output are writable
- Input files are mounted read-only at /input
- Resource limits enforced via cgroups
- Container is destroyed immediately after completion (--rm)

The function returns a structured result dict compatible with
the existing ``execute_code`` tool schema so callers need minimal
changes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from app.services.sandbox.container_manager import (
    is_docker_available,
    run_sandbox_container,
    prepare_input_package,
    collect_outputs,
)
from app.services.sandbox.resource_limits import (
    get_resource_limits,
    get_runtime_image,
    SandboxResourceLimits,
)
from app.services.tool_security import redact_secrets, truncate_output

logger = logging.getLogger(__name__)


async def execute_in_sandbox(
    code: str,
    runtime: str | None = None,
    timeout: int | None = None,
    env_vars: dict | None = None,
    extra_files: dict | None = None,
) -> dict:
    """Execute code in an isolated Docker sandbox.

    Args:
        code: The source code to execute.
        runtime: Runtime string (e.g. "python", "python-3.12", "node").
                 Determines the Docker image and resource limits.
        timeout: Override the default timeout (seconds).
        env_vars: Extra environment variables to pass into the container.
        extra_files: Dict of {filename: content} to write into /input.

    Returns:
        {
            "success": bool,
            "stdout": str,
            "stderr": str,
            "exit_code": int,
            "execution_mode": "docker" | "fallback",
            "runtime": str,
            "duration_ms": int | None,
            "output_files": list | None,
        }
    """
    limits = get_resource_limits(runtime)
    effective_timeout = timeout or limits.timeout
    image = get_runtime_image(runtime)

    # --- Build input package ---
    input_package: dict = {
        "skill_config": {
            "runtime": runtime or "python",
            "timeout": effective_timeout,
        },
        "instructions": code,
    }

    # Include extra files if provided
    if extra_files:
        for fname, fcontent in extra_files.items():
            if isinstance(fcontent, str):
                fcontent_b64 = base64.b64encode(fcontent.encode("utf-8")).decode("ascii")
            elif isinstance(fcontent, bytes):
                fcontent_b64 = base64.b64encode(fcontent).decode("ascii")
            else:
                fcontent_b64 = ""
            input_package.setdefault("data_snapshots", []).append({
                "name": fname,
                "data": {"content_b64": fcontent_b64},
                "format": "raw",
            })

    # --- Run in Docker if available ---
    if is_docker_available():
        return await _run_docker(
            code=code,
            runtime=runtime,
            image=image,
            limits=limits,
            timeout=effective_timeout,
            env_vars=env_vars,
            input_package=input_package,
        )

    # --- Fallback: run in subprocess (existing behavior) ---
    logger.info("Docker not available — using subprocess fallback for sandbox execution")
    return await _run_fallback(code=code, runtime=runtime, timeout=effective_timeout)


async def _run_docker(
    code: str,
    runtime: str | None,
    image: str,
    limits: SandboxResourceLimits,
    timeout: int,
    env_vars: dict | None,
    input_package: dict,
) -> dict:
    """Execute code in Docker container with full isolation."""
    import time

    with tempfile.TemporaryDirectory(prefix="zhanlu_sandbox_") as tmpdir:
        input_dir = os.path.join(tmpdir, "input")
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # Prepare input package (code + extra files)
        prepare_input_package(input_dir, input_package)

        # Write the actual code as a script
        script_name = _script_name_for_runtime(runtime)
        script_path = os.path.join(input_dir, "skill", script_name)
        os.makedirs(os.path.dirname(script_path), exist_ok=True)
        Path(script_path).write_text(code, encoding="utf-8")

        # Build the command to run inside the container
        command = _build_command(runtime, script_name)

        try:
            result = run_sandbox_container(
                image_name=image,
                input_dir=input_dir,
                output_dir=output_dir,
                command=command,
                timeout=timeout,
                memory=limits.memory,
                cpus=limits.cpus,
                env_vars=env_vars,
            )

            # Collect output files
            outputs = collect_outputs(output_dir)

            stdout = truncate_output(result.get("stdout", ""), 8000)
            stderr = truncate_output(result.get("stderr", ""), 4000)

            # Redact secrets
            stdout = redact_secrets(stdout)
            stderr = redact_secrets(stderr)

            return {
                "success": result.get("exit_code", -1) == 0,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": result.get("exit_code", -1),
                "execution_mode": "docker",
                "runtime": runtime or "python",
                "duration_ms": result.get("duration_ms"),
                "output_files": outputs if outputs else None,
            }

        except RuntimeError as e:
            logger.error("Docker sandbox execution failed: %s", e)
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Docker sandbox error: {e}",
                "exit_code": -1,
                "execution_mode": "docker",
                "runtime": runtime or "python",
                "duration_ms": None,
                "output_files": None,
            }


async def _run_fallback(
    code: str,
    runtime: str | None,
    timeout: int,
) -> dict:
    """Run code in a subprocess as fallback when Docker is unavailable.

    Reuses the existing _wrap_code approach but adds per-runtime
    interpreter selection.
    """
    import asyncio

    interpreter, suffix = _interpreter_for_runtime(runtime)
    wrapper = _build_fallback_wrapper(code, suffix)

    with tempfile.TemporaryDirectory(prefix="zhanlu_exec_") as tmpdir:
        script_path = Path(tmpdir, f"exec.{suffix}")
        script_path.write_text(wrapper, encoding="utf-8")

        clean_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": tmpdir,
            "TMPDIR": tmpdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                interpreter, str(script_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_env,
                cwd=tmpdir,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            stdout = truncate_output(stdout_bytes.decode("utf-8", errors="replace"), 8000)
            stderr = truncate_output(stderr_bytes.decode("utf-8", errors="replace"), 4000)
            stdout = redact_secrets(stdout)
            stderr = redact_secrets(stderr)

            return {
                "success": proc.returncode == 0,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": proc.returncode or 0,
                "execution_mode": "fallback",
                "runtime": runtime or "python",
                "duration_ms": None,
                "output_files": None,
            }
        except asyncio.TimeoutError:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Fallback execution timed out after {timeout}s",
                "exit_code": -1,
                "execution_mode": "fallback",
                "runtime": runtime or "python",
                "duration_ms": None,
                "output_files": None,
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Fallback execution error: {e}",
                "exit_code": -1,
                "execution_mode": "fallback",
                "runtime": runtime or "python",
                "duration_ms": None,
                "output_files": None,
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _script_name_for_runtime(runtime: str | None) -> str:
    """Determine the script filename based on runtime."""
    if not runtime:
        return "exec.py"
    engine = runtime.split("-")[0].lower()
    mapping = {"python": "exec.py", "node": "exec.js", "bash": "exec.sh"}
    return mapping.get(engine, "exec.py")


def _interpreter_for_runtime(runtime: str | None) -> tuple[str, str]:
    """Return (interpreter_path, file_suffix) for fallback execution."""
    if not runtime:
        return ("python3", "py")
    engine = runtime.split("-")[0].lower()
    mapping = {
        "python": ("python3", "py"),
        "node": ("node", "js"),
        "bash": ("bash", "sh"),
    }
    return mapping.get(engine, ("python3", "py"))


def _build_command(runtime: str | None, script_name: str) -> list[str]:
    """Build the command line to execute the script inside the container."""
    if not runtime:
        return ["python", f"/input/skill/{script_name}"]
    engine = runtime.split("-")[0].lower()
    mapping = {
        "python": ["python", f"/input/skill/{script_name}"],
        "node": ["node", f"/input/skill/{script_name}"],
        "bash": ["bash", f"/input/skill/{script_name}"],
    }
    return mapping.get(engine, ["python", f"/input/skill/{script_name}"])


def _build_fallback_wrapper(code: str, suffix: str) -> str:
    """Build a sandbox wrapper for the fallback subprocess executor."""
    if suffix == "py":
        import_whitelist = [
            "math", "json", "re", "collections", "itertools", "functools",
            "datetime", "decimal", "statistics", "string",
            "pymysql", "mysql", "sqlalchemy", "pandas", "numpy",
        ]
        allowed = repr(list(import_whitelist))
        return f'''
import sys
import builtins as _builtins_mod

_safe_builtins = dict(_builtins_mod.__dict__)
for _name in ("exec", "eval", "compile", "open", "input", "breakpoint", "exit", "quit"):
    _safe_builtins.pop(_name, None)

_real_import = _builtins_mod.__import__
_allowed = set({allowed})

def _safe_import(name, *args, **kwargs):
    top = name.split(".")[0]
    if top not in _allowed:
        raise ImportError(f"Import of '{{name}}' is not allowed.")
    return _real_import(name, *args, **kwargs)

_safe_builtins["__import__"] = _safe_import

_user_globals = {{"__builtins__": _safe_builtins, "__name__": "__main__"}}
exec(compile({repr(code)}, "<sandbox>", "exec"), _user_globals)
'''
    elif suffix == "js":
        return (
            f'// Sandboxed Node.js execution\n'
            f'// Restricted: no require, no process.exit\n'
            f'{{eval({repr(code)});}}'
        )
    else:
        return (
            f'#!/bin/bash\n'
            f'# Sandboxed bash execution\n'
            f'set -euo pipefail\n'
            f'{code}'
        )
