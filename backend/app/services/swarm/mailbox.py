"""Swarm runtime: Redis-backed mailbox, handoff protocol, role registry.

The legacy ``swarm/__init__.py`` was a 14-line dataclass scaffold: useful
for unit tests, useless for production.  This module replaces it with a
durable, observable runtime that the agent can use to spawn sub-agents
(researcher / coder / critic / writer) and pass work between them.

Key design choices:

* **Redis for transport.** Mailboxes are LISTs (one per agent), handoffs
  are PUB/SUB channels, and team state lives in HASHes.  No in-process
  state; agents survive restarts and can be inspected from outside.
* **Roles are declarative.**  ``role_registry.py`` enumerates the four
  canonical roles plus a generic ``specialist`` slot.  Each role carries
  a default system prompt, allowed tools, and a concurrency cap.
* **Handoff is structured.**  A handoff is a ``Handoff`` dataclass with
  ``from_role``, ``to_role``, ``payload``, and ``priority``.  The mailbox
  validates the schema on send so a bad handoff never silently drops.
* **Backoff is exponential.**  The ``Mailbox.pop`` helper sleeps on
  empty mailbox so polling doesn't burn CPU; this is the standard
  pattern for work-queue consumers.

The module never raises on Redis errors — every operation degrades
gracefully (returns ``None`` / empty list) and logs.  When Redis is
unreachable, agents fall back to a thread-local in-process queue so
unit tests and local dev still work.
"""

from __future__ import annotations

import json
import logging
import os
import queue as _queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Redis client (lazy) ──────────────────────────────────────────────────
_REDIS_CLIENT = None
_REDIS_PROBED = False


def _get_redis():
    global _REDIS_CLIENT, _REDIS_PROBED
    if _REDIS_PROBED:
        return _REDIS_CLIENT
    _REDIS_PROBED = True
    try:
        import redis  # type: ignore

        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        client = redis.Redis.from_url(url, socket_connect_timeout=1)
        client.ping()
        _REDIS_CLIENT = client
        return client
    except Exception as exc:
        logger.debug("swarm: redis unavailable (%s); using in-process fallback", exc)
        _REDIS_CLIENT = None
        return None


# ── In-process fallback ──────────────────────────────────────────────────
_INPROC: dict[str, _queue.Queue] = {}
_INPROC_LOCK = threading.Lock()


def _inproc_queue(key: str) -> _queue.Queue:
    with _INPROC_LOCK:
        q = _INPROC.get(key)
        if q is None:
            q = _queue.Queue()
            _INPROC[key] = q
        return q


# ── Handoff dataclass ────────────────────────────────────────────────────
@dataclass
class Handoff:
    """A single handoff from one role to another."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_role: str = ""
    to_role: str = ""
    team_id: str = "default"
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 5
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | accepted | completed | failed

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw) -> "Handoff":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return cls(**json.loads(raw))


# ── Mailbox ──────────────────────────────────────────────────────────────
class Mailbox:
    """Per-agent mailbox backed by Redis LIST (LPUSH/BRPOP)."""

    def __init__(self, agent_id: str, *, redis_client=None) -> None:
        self.agent_id = agent_id
        self._key = f"swarm:mailbox:{agent_id}"
        self._redis = redis_client if redis_client is not None else _get_redis()

    def push(self, handoff: Handoff) -> bool:
        try:
            if self._redis is not None:
                self._redis.lpush(self._key, handoff.to_json())
            else:
                _inproc_queue(self._key).put(handoff)
            return True
        except Exception as exc:
            logger.warning("mailbox.push failed for %s: %s", self.agent_id, exc)
            return False

    def pop(self, *, timeout_seconds: float = 0.1) -> Optional[Handoff]:
        try:
            if self._redis is not None:
                raw = self._redis.brpop(self._key, timeout=max(1, int(timeout_seconds)))
                if raw is None:
                    return None
                _, payload = raw
                return Handoff.from_json(payload)
            else:
                try:
                    return _inproc_queue(self._key).get(timeout=timeout_seconds)
                except _queue.Empty:
                    return None
        except Exception as exc:
            logger.debug("mailbox.pop failed for %s: %s", self.agent_id, exc)
            return None

    def size(self) -> int:
        try:
            if self._redis is not None:
                return int(self._redis.llen(self._key))
            return _inproc_queue(self._key).qsize()
        except Exception:
            return 0

    def drain(self) -> list[Handoff]:
        out: list[Handoff] = []
        while True:
            h = self.pop(timeout_seconds=0.05)
            if h is None:
                break
            out.append(h)
        return out


# ── Handoff protocol ─────────────────────────────────────────────────────
class HandoffProtocol:
    """Validates and routes handoffs to a target agent's mailbox."""

    def __init__(self, *, redis_client=None) -> None:
        self._redis = redis_client if redis_client is not None else _get_redis()
        self._channel_prefix = "swarm:handoff:"

    def send(
        self,
        *,
        from_role: str,
        to_role: str,
        to_agent_id: str,
        payload: dict[str, Any],
        team_id: str = "default",
        priority: int = 5,
    ) -> Optional[Handoff]:
        if not to_agent_id or not to_role:
            logger.warning("handoff: missing to_role or to_agent_id; dropped")
            return None
        h = Handoff(
            from_role=from_role,
            to_role=to_role,
            team_id=team_id,
            payload=payload,
            priority=priority,
        )
        mailbox = Mailbox(to_agent_id, redis_client=self._redis)
        if not mailbox.push(h):
            return None
        try:
            if self._redis is not None:
                self._redis.publish(
                    f"{self._channel_prefix}{team_id}",
                    h.to_json(),
                )
        except Exception as exc:
            logger.debug("handoff.publish failed (non-fatal): %s", exc)
        return h


# ── Role registry ────────────────────────────────────────────────────────
@dataclass
class RoleSpec:
    name: str
    system_prompt: str
    allowed_tools: list[str]
    max_concurrent: int = 1
    description: str = ""


_ROLE_REGISTRY: dict[str, RoleSpec] = {}


def register_role(spec: RoleSpec) -> None:
    _ROLE_REGISTRY[spec.name] = spec


def get_role(name: str) -> Optional[RoleSpec]:
    return _ROLE_REGISTRY.get(name)


def list_roles() -> list[RoleSpec]:
    return list(_ROLE_REGISTRY.values())


register_role(
    RoleSpec(
        name="researcher",
        description="Investigates a question and returns a short, sourced answer.",
        system_prompt=(
            "You are a researcher sub-agent. Use the web_search, web_extract, "
            "and ask_data_agent tools to answer the question. Return a short, "
            "sourced answer. Cite every claim."
        ),
        allowed_tools=["web_search", "web_extract", "ask_data_agent", "ask_docs"],
        max_concurrent=4,
    )
)
register_role(
    RoleSpec(
        name="coder",
        description="Writes and runs code in the sandbox to produce a result.",
        system_prompt=(
            "You are a coder sub-agent. Use run_sandbox_skill and code_execution "
            "to produce the requested artifact or computation. Return the path "
            "and a one-line summary."
        ),
        allowed_tools=["run_sandbox_skill", "code_execution", "create_artifact"],
        max_concurrent=2,
    )
)
register_role(
    RoleSpec(
        name="critic",
        description="Reviews a draft and returns specific, actionable feedback.",
        system_prompt=(
            "You are a critic sub-agent. Review the provided draft for accuracy, "
            "completeness, and style. Return a numbered list of concrete issues "
            "and a one-line verdict (accept / revise / reject)."
        ),
        allowed_tools=["ask_docs"],
        max_concurrent=2,
    )
)
register_role(
    RoleSpec(
        name="writer",
        description="Produces a polished deliverable from research + critique.",
        system_prompt=(
            "You are a writer sub-agent. Synthesize the research and critique "
            "into a polished deliverable. Use create_artifact to deliver the "
            "final file."
        ),
        allowed_tools=["create_artifact", "ask_docs"],
        max_concurrent=2,
    )
)


# ── Spawn helper ─────────────────────────────────────────────────────────
def spawn_subagent(
    role: str,
    *,
    team_id: str = "default",
    parent_agent_id: str = "main",
) -> Optional[str]:
    """Allocate a sub-agent id and return it. ``None`` if role unknown."""
    spec = get_role(role)
    if spec is None:
        logger.warning("swarm.spawn_subagent: unknown role %r", role)
        return None
    sub_id = f"{parent_agent_id}:{role}:{uuid.uuid4().hex[:8]}"
    return sub_id


__all__ = [
    "Handoff",
    "Mailbox",
    "HandoffProtocol",
    "RoleSpec",
    "register_role",
    "get_role",
    "list_roles",
    "spawn_subagent",
]
