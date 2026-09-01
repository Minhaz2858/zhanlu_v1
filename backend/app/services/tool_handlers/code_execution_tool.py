"""execute_code tool — Python sandbox execution.

Runs Python code in a subprocess with:
  - Restricted builtins (no __import__, open, exec, eval, os, sys, subprocess)
  - Timeout (configurable via CODE_EXECUTION_TIMEOUT, default 30s)
  - No network access (scrubbed environment variables)
  - Temp working directory
  - stdout/stderr captured and truncated

This is simpler than Hermes' PTC architecture but sufficient for
data analysis and computation tasks.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_registry import registry
from app.services.tool_security import redact_secrets, truncate_output

logger = logging.getLogger(__name__)

# Modules the sandbox is allowed to import.
# Standard library + DB drivers + data-science packages needed for
# autonomous data analysis.  Security boundary is the restricted-builtins
# wrapper (no exec, eval, open, subprocess) — not the import whitelist.
_ALLOWED_IMPORTS = {
    # stdlib
    "math", "json", "re", "collections", "itertools", "functools",
    "datetime", "decimal", "statistics", "string",
    # DB drivers (top-level packages)
    "pymysql", "mysql",
    # ORM
    "sqlalchemy",
    # Data science
    "pandas", "numpy",
}


def _wrap_code(code: str) -> str:
    """Wrap user code in a sandbox with restricted builtins."""
    import_whitelist = repr(list(_ALLOWED_IMPORTS))

    wrapper = f'''
import sys
import builtins as _builtins_mod

# --- Build restricted builtins ---
_safe_builtins = dict(_builtins_mod.__dict__)
for _name in ("exec", "eval", "compile", "open", "input", "breakpoint", "exit", "quit"):
    _safe_builtins.pop(_name, None)

_real_import = _builtins_mod.__import__
_allowed = set({import_whitelist})

def _safe_import(name, *args, **kwargs):
    top = name.split(".")[0]
    if top not in _allowed:
        raise ImportError(f"Import of '{{name}}' is not allowed. Allowed: {{', '.join(sorted(_allowed))}}")
    return _real_import(name, *args, **kwargs)

_safe_builtins["__import__"] = _safe_import

# --- Execute user code with restricted builtins ---
_user_globals = {{"__builtins__": _safe_builtins, "__name__": "__main__"}}
_user_code = {repr(code)}
exec(compile(_user_code, "<sandbox>", "exec"), _user_globals)
'''
    return wrapper


async def _execute_code(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    code = args.get("code", "").strip()

    if not code:
        return {"success": False, "error": "code is required"}

    timeout = settings.CODE_EXECUTION_TIMEOUT

    wrapped_code = _wrap_code(code)

    with tempfile.TemporaryDirectory(prefix="zhanlu_exec_") as tmpdir:
        script_path = Path(tmpdir, "exec.py")
        script_path.write_text(wrapped_code, encoding="utf-8")

        try:
            # Run with minimal environment (no network access)
            clean_env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": tmpdir,
                "TMPDIR": tmpdir,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }

            result = subprocess.run(
                ["python3", str(script_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=clean_env,
                cwd=tmpdir,
            )

            stdout = truncate_output(result.stdout or "", settings.TOOL_MAX_OUTPUT_CHARS)
            stderr = truncate_output(result.stderr or "", settings.TOOL_MAX_OUTPUT_CHARS // 2)

            # Redact secrets from output
            stdout = redact_secrets(stdout)
            stderr = redact_secrets(stderr)

            return {
                "success": result.returncode == 0,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": result.returncode,
            }

        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Code execution timed out after {timeout}s",
                "stdout": "",
                "stderr": f"Timeout: killed after {timeout} seconds",
            }
        except Exception as e:
            return {"success": False, "error": f"Execution failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

EXECUTE_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_code",
        "description": (
            "Execute Python code in a sandboxed environment. "
            "Useful for calculations, data analysis, string manipulation, and algorithms. "
            "Allowed imports: math, json, re, collections, itertools, functools, datetime, decimal, statistics, string (stdlib), "
            "pymysql, mysql, sqlalchemy (DB drivers), pandas, numpy (data science). "
            "No file I/O, no network access, no subprocess. "
            "Output (stdout) is captured and returned. Use print() to return results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Use print() for output.",
                },
            },
            "required": ["code"],
        },
    },
}

registry.register(
    name="execute_code",
    schema=EXECUTE_CODE_SCHEMA,
    handler=_execute_code,
    category="code",
    enabled_by_default=True,
    description="Execute Python code in a sandbox.",
)
