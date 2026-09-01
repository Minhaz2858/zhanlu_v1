"""Team registry + coordinator — the functional swarm layer (2026-08-29).

The swarm tools (swarm_tools.py) were written against a PRE-REFACTOR API
(``get_team_registry`` / ``get_swarm_coordinator`` with an in-memory
TeamRegistry) that no longer exists after the scaffold-to-runtime
migration — every handler crashed with ImportError the moment it ran.
This module restores that surface on top of the CURRENT runtime:

* :class:`TeamRegistry` — in-process team coordination state (teams,
  members, scratch space).  Messages are PERSISTED to the
  ``swarm_mailbox_messages`` table (:class:`SwarmMailboxMessage`) so
  inter-agent communication survives restarts.
* :class:`SwarmCoordinator` — spawns agents via :class:`SwarmRuntime`
  (real tool-calling loop, AGENT_HARNESS_ENABLED → AgentRunOrchestrator)
  wired to the app's ``call_llm`` + ``execute_tool``, then posts the
  final response to the team lead's mailbox.

Teams themselves are ephemeral (in-process singleton): they are per-turn
coordination contexts; messages are durable.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Durable mailbox (DB) ──────────────────────────────────────────────────


def _persist_message(team_id: str, sender: str, recipient: str, content: str,
                     summary: str = "", priority: int = 0) -> Optional[dict]:
    """Insert one message row; returns its dict or None on failure."""
    try:
        from app.database import SessionLocal
        from app.models.swarm_mailbox import SwarmMailboxMessage

        db = SessionLocal()
        try:
            row = SwarmMailboxMessage(
                team_id=team_id[:64], sender=sender[:64], recipient=recipient[:64],
                content=(content or "")[:4000], summary=(summary or "")[:500],
                priority=max(0, min(priority, 100)), read=False,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            d = row.to_dict()
            d["timestamp"] = d.get("created_date") or d.get("created_at")
            return d
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — never break the agent turn
        logger.warning("swarm: persist message failed: %s", exc)
        return None


def _fetch_messages(team_id: str, recipient: str, limit: int = 50) -> list[dict]:
    """Read a mailbox (optionally mark-as-read); [] on any failure."""
    try:
        from app.database import SessionLocal
        from app.models.swarm_mailbox import SwarmMailboxMessage

        db = SessionLocal()
        try:
            q = (
                db.query(SwarmMailboxMessage)
                .filter(SwarmMailboxMessage.team_id == team_id[:64])
                .order_by(SwarmMailboxMessage.created_date.asc())
            )
            if recipient:
                q = q.filter(SwarmMailboxMessage.recipient == recipient[:64])
            rows = q.limit(limit).all()
            out = []
            for row in rows:
                d = row.to_dict()
                d["timestamp"] = d.get("created_date") or d.get("created_at")
                out.append(d)
            return out
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("swarm: fetch messages failed: %s", exc)
        return []


# ── Team state (in-process) ───────────────────────────────────────────────


@dataclass
class Team:
    """A coordination group for spawned agents."""

    id: str = field(default_factory=lambda: f"team_{uuid.uuid4().hex[:10]}")
    name: str = ""
    description: str = ""
    members: dict = field(default_factory=dict)  # member_name → role
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at
        return d


class TeamRegistry:
    """In-process registry of swarm teams + shared scratch space."""

    def __init__(self) -> None:
        self._teams: dict[str, Team] = {}
        self._scratch: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def create_team(self, name: str, description: str = "") -> Team:
        with self._lock:
            team = Team(name=name or "team", description=description or "")
            team.members["main"] = "lead"
            self._teams[team.id] = team
            return team

    def get_team(self, team_id: str) -> Optional[Team]:
        return self._teams.get(team_id)

    def list_teams(self) -> list[Team]:
        return list(self._teams.values())

    def add_member(self, team_id: str, member: str, role: str = "member") -> bool:
        team = self._teams.get(team_id)
        if not team:
            return False
        team.members[member] = role
        return True

    # ── Messaging (durable via DB) ──────────────────────────────────────
    def send_message(self, team_id: str, sender: str, recipient: str, content: str,
                     summary: str = "", priority: int = 0) -> bool:
        if not self._teams.get(team_id):
            return False
        row = _persist_message(team_id, sender, recipient, content, summary, priority)
        return row is not None

    def get_messages(self, team_id: str, member_name: str, limit: int = 50) -> list[dict]:
        return _fetch_messages(team_id, member_name, limit=limit)

    # ── Scratch space (ephemeral working memory) ────────────────────────
    def set_scratch(self, team_id: str, key: str, value: str) -> bool:
        if not self._teams.get(team_id):
            return False
        with self._lock:
            self._scratch.setdefault(team_id, {})[key] = value
            return True

    def get_scratch(self, team_id: str, key: str) -> Optional[str]:
        return self._scratch.get(team_id, {}).get(key)


# ── Coordinator (spawn / orchestrate runner) ──────────────────────────────


class SwarmCoordinator:
    """Spawns swarm agents via SwarmRuntime wired to the app's LLM + tools.

    ``run_task`` matches the orchestrator's Runner signature
    ``(team_id, agent_name, task, member_name) -> SwarmAgentResult``.
    ``spawn_agent`` is the tool-facing convenience that returns the
    member name and posts the final answer to the team lead mailbox.
    """

    def __init__(self, registry: Optional[TeamRegistry] = None,
                 runtime: Any = None) -> None:
        self.registry = registry or get_team_registry()
        self.runtime = runtime

    def _get_runtime(self):
        if self.runtime is None:
            from app.services.swarm.runtime import SwarmRuntime
            self.runtime = SwarmRuntime()
        return self.runtime

    async def _llm(self, system_prompt: str, messages: list[dict]) -> dict:
        from app.services.llm_service import call_llm

        res = await call_llm(prompt=system_prompt, messages=messages, temperature=0.3)
        return {
            "response": res.get("response") or res.get("content") or "",
            "tool_calls": res.get("tool_calls") or [],
        }

    async def _dispatch(self, name: str, arguments: dict, db: Any,
                        user_id: Optional[str], context: Optional[dict]) -> dict:
        from app.services.agent_tools import execute_tool

        return await execute_tool(name, arguments, db, user_id, context)

    async def run_task(self, team_id: str, agent_name: str, task: str,
                       member_name: Optional[str] = None, db: Any = None,
                       user_id: Optional[str] = None):
        """Run one agent; returns the SwarmAgentResult (orchestrator Runner)."""
        from app.services.swarm.runtime import SwarmRuntime

        runtime = self.runtime or SwarmRuntime()
        try:
            return await runtime.run(
                agent_name=agent_name,
                task=task,
                llm_fn=self._llm,
                tool_dispatcher=self._dispatch,
                db=db,
                user_id=user_id,
                member_name=member_name,
            )
        except Exception as exc:  # noqa: BLE001 — never kill the turn
            logger.warning("swarm: run_task failed: %s", exc)
            from app.services.swarm.runtime import SwarmAgentResult

            return SwarmAgentResult(
                member_name=member_name or agent_name, agent_name=agent_name,
                task=task, success=False, error=str(exc),
            )

    async def spawn_agent(self, team_id: str, agent_name: str, task: str,
                          member_name: Optional[str] = None, db: Any = None,
                          user_id: Optional[str] = None) -> str:
        """Spawn a worker, post its final answer to the team lead mailbox."""
        result = await self.run_task(team_id, agent_name, task, member_name, db, user_id)
        mname = result.member_name or member_name or agent_name
        # Add to the team roster so the lead can see who worked.
        self.registry.add_member(team_id, mname, role=agent_name)
        if result.final_response or result.error:
            self.registry.send_message(
                team_id, sender=mname, recipient="main",
                content=result.final_response or result.error or "",
                summary=f"{agent_name} finished: {'success' if result.success else 'failed'}",
            )
        return mname


# ── Singletons ────────────────────────────────────────────────────────────

_registry_singleton: Optional[TeamRegistry] = None
_coordinator_singleton: Optional[SwarmCoordinator] = None
# RLock (not Lock): get_swarm_coordinator() calls get_team_registry() while
# holding this lock — a plain Lock deadlocks on itself (non-reentrant).
_singleton_lock = threading.RLock()


def get_team_registry() -> TeamRegistry:
    global _registry_singleton
    with _singleton_lock:
        if _registry_singleton is None:
            _registry_singleton = TeamRegistry()
        return _registry_singleton


def get_swarm_coordinator() -> SwarmCoordinator:
    global _coordinator_singleton
    with _singleton_lock:
        if _coordinator_singleton is None:
            _coordinator_singleton = SwarmCoordinator(registry=get_team_registry())
        return _coordinator_singleton


__all__ = [
    "Team",
    "TeamRegistry",
    "SwarmCoordinator",
    "get_team_registry",
    "get_swarm_coordinator",
]
