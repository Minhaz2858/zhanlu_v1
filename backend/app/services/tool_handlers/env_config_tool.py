"""update_env_config and docker_compose_restart — closes the agent's
credential-collection loop.

When a tool returns a ``missing_config`` response, the agent is supposed
to ask the user for the values, then call ``update_env_config`` to write
them to ``/root/zhanlu/.env`` and ``docker_compose_restart`` to apply the
new env. Without these, the agent can detect missing config but cannot
complete the recovery on its own.

Both are admin-gated. The admin gate is the existing
``app.services.permissions.check_permission`` flow; for a stricter gate,
the handlers also check the user_id against a configured allowlist
(env var ``ZHANLU_ADMIN_USER_IDS``, comma-separated).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_handlers._missing_config import missing_config_response
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Admin gating
# ---------------------------------------------------------------------------

def _is_admin(user_id: Optional[str]) -> bool:
    """Return True if the user_id is in the admin allowlist.

    The allowlist is read from ``ZHANLU_ADMIN_USER_IDS`` (comma-separated)
    on every call (no caching) so adding/removing admins takes effect
    without a restart. When unset, no user is admin — the tools return a
    structured "admin required" error and the agent must escalate to a
    human operator.
    """
    if not user_id:
        return False
    allowlist = os.environ.get("ZHANLU_ADMIN_USER_IDS", "").strip()
    if not allowlist:
        return False
    admins = {a.strip() for a in allowlist.split(",") if a.strip()}
    return user_id in admins


# Allowed env var keys: only those starting with a known prefix.
_ALLOWED_ENV_PREFIXES = (
    "OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "SEARCH_", "IMAGE_",
    "ELEVENLABS_", "MISTRAL_", "TAVILY_", "SERPER_",
    "DISCORD_", "FEISHU_", "LARK_", "MS_", "HOMEASSISTANT_",
    "MCP_", "TWITTER_", "XAI_", "OPENROUTER_",
    "YUANBAO_", "TELEGRAM_", "SLACK_",
    "ZHANLU_",  # our own flags
)


def _validate_key(key: str) -> bool:
    """Return True if the env var name is on the allowlist.

    Aimed at preventing the agent from writing arbitrary keys (e.g.
    PATH, LD_PRELOAD, HOME) that could be used to break out of the
    sandbox or change runtime behavior.
    """
    if not key or not isinstance(key, str):
        return False
    if not re.match(r"^[A-Z][A-Z0-9_]{1,63}$", key):
        return False
    return key.startswith(_ALLOWED_ENV_PREFIXES)


# ---------------------------------------------------------------------------
# update_env_config
# ---------------------------------------------------------------------------

ENV_FILE_CANDIDATES = [
    Path("/root/zhanlu/.env"),
    Path(settings.upload_path.parent / ".env"),
    Path.cwd() / ".env",
]


def _find_env_file() -> Path:
    """Locate the .env file. Falls back to the first writable candidate."""
    for candidate in ENV_FILE_CANDIDATES:
        if candidate.exists() and candidate.is_file():
            return candidate
    # None of the candidates exist — create the first one.
    target = ENV_FILE_CANDIDATES[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Comments and blank lines skipped."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        out[key] = val
    return out


def _serialize_env_file(values: dict[str, str]) -> str:
    """Serialize a dict back to .env format. Preserves no ordering guarantees."""
    lines = []
    for key, val in values.items():
        # Quote values that contain spaces, equals signs, or newlines
        if any(c in val for c in (" ", "=", "\n", "#")):
            val = f'"{val}"'
        lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n"


async def _update_env_config(
    args: dict,
    db: Session,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Write a key=value pair to the .env file.

    Args (LLM-facing):
        key: env var name (uppercase, must match allowlist prefixes)
        value: the new value (string)
        action: "set" (default), "delete", or "read"
    """
    if not _is_admin(user_id):
        return {
            "success": False,
            "error": (
                "update_env_config requires admin privileges. Set "
                "ZHANLU_ADMIN_USER_IDS to a comma-separated list of user IDs "
                "(see /root/zhanlu/backend/app/services/auth_service.py for "
                "the current user_id format) and restart the backend."
            ),
            "tool_name": "update_env_config",
            "user_action_required": (
                "A human operator must add this env var to "
                "/root/zhanlu/.env and run `docker compose restart backend`."
            ),
        }

    action = (args.get("action") or "set").lower()
    key = (args.get("key") or "").strip()
    value = args.get("value", "")

    if action == "read":
        env_path = _find_env_file()
        values = _parse_env_file(env_path)
        return {
            "success": True,
            "env_file": str(env_path),
            "values": values,
        }

    if not _validate_key(key):
        return {
            "success": False,
            "error": (
                f"Refusing to write env var {key!r}: must be uppercase "
                f"and start with one of: {', '.join(_ALLOWED_ENV_PREFIXES)}"
            ),
        }

    env_path = _find_env_file()
    values = _parse_env_file(env_path)

    if action == "delete":
        values.pop(key, None)
    elif action == "set":
        if value is None or value == "":
            return {"success": False, "error": "value is required for action='set'"}
        values[key] = str(value)
    else:
        return {
            "success": False,
            "error": f"Unknown action {action!r}. Use 'set', 'delete', or 'read'.",
        }

    try:
        env_path.write_text(_serialize_env_file(values), encoding="utf-8")
    except Exception as exc:
        return {"success": False, "error": f"Failed to write .env: {exc}"}

    return {
        "success": True,
        "action": action,
        "key": key,
        "value": values.get(key),
        "env_file": str(env_path),
        "message": (
            f"Env var {key} {'updated' if action == 'set' else 'deleted'}. "
            f"Call docker_compose_restart to apply."
        ),
    }


UPDATE_ENV_CONFIG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_env_config",
        "description": (
            "Write, delete, or read a single key in /root/zhanlu/.env. "
            "Use this when a tool returns a missing_config response and the "
            "user has provided the value. After writing, call "
            "docker_compose_restart to apply. ADMIN-GATED: requires "
            "ZHANLU_ADMIN_USER_IDS to include your user_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "delete", "read"],
                    "description": "'set' (default) writes the key, 'delete' removes it, 'read' returns current values.",
                },
                "key": {
                    "type": "string",
                    "description": "Env var name (e.g. 'ELEVENLABS_API_KEY'). Must start with one of: OPENAI_, ANTHROPIC_, DEEPSEEK_, SEARCH_, IMAGE_, ELEVENLABS_, MISTRAL_, TAVILY_, SERPER_, DISCORD_, FEISHU_, LARK_, MS_, HOMEASSISTANT_, MCP_, TWITTER_, XAI_, OPENROUTER_, YUANBAO_, TELEGRAM_, SLACK_, ZHANLU_.",
                },
                "value": {
                    "type": "string",
                    "description": "The new value. Required for action='set'.",
                },
            },
            "required": ["action", "key"],
        },
    },
}


# ---------------------------------------------------------------------------
# docker_compose_restart
# ---------------------------------------------------------------------------

async def _docker_compose_restart(
    args: dict,
    db: Session,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Restart one or more docker compose services.

    Args (LLM-facing):
        service: "backend" (default), "frontend", "postgres", or "all"
        wait_seconds: how long to wait for the service to come back (default 10)
    """
    if not _is_admin(user_id):
        return {
            "success": False,
            "error": (
                "docker_compose_restart requires admin privileges. A human "
                "operator must run `docker compose restart backend` (or the "
                "relevant service) from /root/zhanlu/."
            ),
            "tool_name": "docker_compose_restart",
            "user_action_required": (
                "Run `docker compose restart backend` from /root/zhanlu/."
            ),
        }

    service = (args.get("service") or "backend").lower()
    wait_seconds = int(args.get("wait_seconds", 10))
    if wait_seconds < 0 or wait_seconds > 120:
        wait_seconds = 10

    # Locate compose file
    compose_candidates = [
        Path("/root/zhanlu/docker-compose.yml"),
        Path("/root/zhanlu/docker-compose.yaml"),
        Path.cwd() / "docker-compose.yml",
    ]
    compose_path = next((p for p in compose_candidates if p.exists()), None)
    if compose_path is None:
        return {
            "success": False,
            "error": "docker-compose.yml not found in expected locations",
        }

    cmd = ["docker", "compose", "-f", str(compose_path), "restart"]
    if service != "all":
        cmd.append(service)

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(compose_path.parent),
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"`{' '.join(cmd)}` timed out after 60s",
        }
    except FileNotFoundError:
        return {
            "success": False,
            "error": "`docker` CLI not found on PATH inside the backend container",
        }

    if proc.returncode != 0:
        return {
            "success": False,
            "error": f"docker compose restart failed: {proc.stderr.strip()}",
            "stdout": proc.stdout,
        }

    # Brief sleep so the new container starts up before subsequent calls
    import asyncio
    await asyncio.sleep(min(wait_seconds, 5))

    return {
        "success": True,
        "service": service,
        "message": f"Restarted {service} via docker compose. New env vars should be active.",
        "stdout": proc.stdout,
    }


DOCKER_COMPOSE_RESTART_SCHEMA = {
    "type": "function",
    "function": {
        "name": "docker_compose_restart",
        "description": (
            "Restart one or more docker compose services. Use after "
            "update_env_config writes new values to /root/zhanlu/.env so the "
            "new env takes effect. ADMIN-GATED: requires ZHANLU_ADMIN_USER_IDS "
            "to include your user_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["backend", "frontend", "postgres", "all"],
                    "description": "Which service to restart. Default: 'backend'.",
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "Seconds to wait after restart so the new container is up (default 10, max 120).",
                },
            },
            "required": ["service"],
        },
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="update_env_config",
    schema=UPDATE_ENV_CONFIG_SCHEMA,
    handler=_update_env_config,
    category="admin",
    toolset="admin",
    enabled_by_default=True,
    description="Write/delete/read a key in /root/zhanlu/.env (admin-gated).",
    emoji="🔑",
    max_result_size_chars=8_000,
)

registry.register(
    name="docker_compose_restart",
    schema=DOCKER_COMPOSE_RESTART_SCHEMA,
    handler=_docker_compose_restart,
    category="admin",
    toolset="admin",
    enabled_by_default=True,
    description="Restart a docker compose service (admin-gated).",
    emoji="🔄",
    max_result_size_chars=8_000,
)
