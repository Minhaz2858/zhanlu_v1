"""sandbox_code tool — execute arbitrary code in a Docker sandbox.

Replaces the existing ``execute_code`` subprocess-based handler with a
Docker-isolated container that provides true resource enforcement via
cgroups. Falls back to the subprocess executor when Docker is unavailable.

The tool reads the optional ``runtime`` key from the frontmatter of any
skill that triggers this execution, allowing per-skill CPU/memory/timeout
overrides.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _sandbox_code(
    args: dict,
    db: Session,
    user_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Execute code in an isolated sandbox.

    Accepts the same ``code`` parameter as ``execute_code``, plus an
    optional ``runtime`` override (e.g. "python", "python-2g", "node").
    """
    code = (args.get("code") or "").strip()
    if not code:
        return {"success": False, "error": "code is required"}

    runtime = (args.get("runtime") or "").strip() or None

    # If context provides a skill_name, look up its runtime from DB frontmatter
    if not runtime and context:
        skill_name = context.get("skill_name") or context.get("tool_name")
        if skill_name and db:
            runtime = _lookup_skill_runtime(db, skill_name)

    from app.services.sandbox.runner import execute_in_sandbox

    timeout = args.get("timeout")
    if timeout and isinstance(timeout, (int, float)):
        timeout = int(timeout)

    env_vars: dict | None = None
    if args.get("env"):
        env_vars = args["env"] if isinstance(args["env"], dict) else None

    result = await execute_in_sandbox(
        code=code,
        runtime=runtime,
        timeout=timeout,
        env_vars=env_vars,
    )
    return result


def _lookup_skill_runtime(db: Session, skill_name: str) -> str | None:
    """Look up the ``runtime`` frontmatter field for a skill from the DB.

    Checks the ``tools`` table for a row where ``name`` matches and
    ``skill_md`` has a ``runtime:`` entry in its YAML frontmatter.
    """
    try:
        from app.models.tool import Tool
        import yaml

        row = (
            db.query(Tool.skill_md)
            .filter(
                Tool.name == skill_name,
                Tool.skill_md.isnot(None),
                Tool.skill_md != "",
                Tool.is_deleted == False,
            )
            .first()
        )
        if not row or not row.skill_md:
            return None

        skill_content = row.skill_md
        if not skill_content.startswith("---"):
            return None
        parts = skill_content.split("---", 2)
        if len(parts) < 3:
            return None
        fm = yaml.safe_load(parts[1].strip())
        if isinstance(fm, dict):
            runtime = fm.get("runtime")
            if runtime:
                return str(runtime).strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

SANDBOX_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "sandbox_code",
        "description": (
            "Execute code in a fully isolated Docker sandbox with cgroup-backed "
            "resource limits. Preferred over execute_code for untrusted or "
            "long-running code. "
            "Supports multiple runtimes: python (default), node, bash. "
            "Optional 'runtime' parameter overrides the container image and "
            "resource limits (e.g. 'python-2g' for 2GB RAM). "
            "Returns stdout, stderr, exit_code, and any output files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Source code to execute. Use print() for output.",
                },
                "runtime": {
                    "type": "string",
                    "description": (
                        "Runtime to use. One of: python, python-1g, python-2g, "
                        "node, node-1g, bash. Default: python."
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Override the default timeout in seconds.",
                },
                "env": {
                    "type": "object",
                    "description": "Extra environment variables to pass into the sandbox.",
                },
            },
            "required": ["code"],
        },
    },
}

registry.register(
    name="sandbox_code",
    schema=SANDBOX_CODE_SCHEMA,
    handler=_sandbox_code,
    category="code",
    enabled_by_default=True,
    description="Execute code in a fully isolated Docker sandbox with resource limits.",
)
