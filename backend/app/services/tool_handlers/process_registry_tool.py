"""process_registry tool — list/kill background processes started by terminal_tool.

Tracks processes spawned by the agent's terminal so they can be polled,
retrieved, or killed mid-stream. The state lives in-memory; on backend
restart the registry is empty.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_handlers.ansi_strip import strip_ansi
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 200_000      # 200KB rolling output buffer
FINISHED_TTL_SECONDS = 1800     # 30 minutes
MAX_PROCESSES = 64


@dataclass
class ProcessSession:
    id: str
    command: str
    task_id: Optional[str]
    started_at: float
    proc: Optional[subprocess.Popen]
    output: List[str] = field(default_factory=list)
    output_chars: int = 0
    status: str = "running"     # running | done | killed | error
    exit_code: Optional[int] = None
    error: Optional[str] = None

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        # Strip ANSI before storing (so it doesn't pollute later model context)
        chunk = strip_ansi(chunk)
        self.output.append(chunk)
        self.output_chars += len(chunk)
        # Trim oldest output if we exceed the buffer
        while self.output_chars > MAX_OUTPUT_CHARS and len(self.output) > 1:
            removed = self.output.pop(0)
            self.output_chars -= len(removed)

    def tail(self, last_n_lines: int = 200) -> str:
        if not self.output:
            return ""
        return "".join(self.output[-max(1, last_n_lines):])


class _ProcessRegistry:
    """In-memory registry for background processes started by terminal_tool."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: Dict[str, ProcessSession] = {}

    def spawn(self, command: str, task_id: Optional[str] = None) -> ProcessSession:
        """Spawn a background process. Returns the session."""
        sid = uuid.uuid4().hex[:12]
        with self._lock:
            self._prune_finished_locked()
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            sess = ProcessSession(
                id=sid,
                command=command,
                task_id=task_id,
                started_at=time.time(),
                proc=proc,
            )
            self._sessions[sid] = sess
        # Start a background thread that reads stdout
        threading.Thread(
            target=self._reader_thread,
            args=(sess,),
            daemon=True,
        ).start()
        return sess

    def _reader_thread(self, sess: ProcessSession) -> None:
        try:
            if sess.proc is None or sess.proc.stdout is None:
                return
            for line in sess.proc.stdout:
                sess.append(line)
            sess.proc.wait()
            sess.exit_code = sess.proc.returncode
            sess.status = "done" if sess.exit_code == 0 else "error"
        except Exception as exc:
            sess.error = str(exc)
            sess.status = "error"
        finally:
            with self._lock:
                self._prune_finished_locked()

    def _prune_finished_locked(self) -> None:
        if len(self._sessions) <= MAX_PROCESSES:
            return
        # Drop the oldest finished sessions
        finished = sorted(
            (s for s in self._sessions.values() if s.status != "running"),
            key=lambda s: s.started_at,
        )
        for s in finished:
            if len(self._sessions) <= MAX_PROCESSES:
                break
            del self._sessions[s.id]

    def list(self) -> List[dict]:
        with self._lock:
            return [
                {
                    "id": s.id,
                    "command": s.command[:200],
                    "task_id": s.task_id,
                    "status": s.status,
                    "exit_code": s.exit_code,
                    "started_at": s.started_at,
                    "age_seconds": int(time.time() - s.started_at),
                    "output_chars": s.output_chars,
                }
                for s in self._sessions.values()
            ]

    def get(self, session_id: str) -> Optional[ProcessSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def tail(self, session_id: str, last_n_lines: int = 200) -> Optional[str]:
        sess = self.get(session_id)
        if sess is None:
            return None
        return sess.tail(last_n_lines)

    def kill(self, session_id: str) -> bool:
        sess = self.get(session_id)
        if sess is None or sess.proc is None:
            return False
        try:
            sess.proc.terminate()
            try:
                sess.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sess.proc.kill()
            sess.status = "killed"
            return True
        except Exception as exc:
            sess.error = str(exc)
            sess.status = "error"
            return False


process_registry = _ProcessRegistry()


async def _process_registry_list(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    return {"success": True, "processes": process_registry.list()}


async def _process_registry_tail(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    sid = (args.get("session_id") or "").strip()
    if not sid:
        return {"success": False, "error": "session_id is required"}
    last_n = int(args.get("last_n_lines", 200))
    out = process_registry.tail(sid, last_n)
    if out is None:
        return {"success": False, "error": f"Unknown session_id: {sid}"}
    return {"success": True, "session_id": sid, "output_tail": out}


async def _process_registry_kill(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    sid = (args.get("session_id") or "").strip()
    if not sid:
        return {"success": False, "error": "session_id is required"}
    ok = process_registry.kill(sid)
    if not ok:
        return {"success": False, "error": f"Could not kill session {sid} (not running or not found)"}
    return {"success": True, "session_id": sid, "message": "Process terminated"}


PROCESS_REGISTRY_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "process_registry_list",
        "description": (
            "List background processes spawned by the agent (via "
            "terminal_run with background=true). Returns id, command, "
            "status, exit_code, age, output size for each."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

PROCESS_REGISTRY_TAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "process_registry_tail",
        "description": "Get the tail of a background process's output.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The process session id."},
                "last_n_lines": {"type": "integer", "description": "How many trailing lines to return.", "default": 200},
            },
            "required": ["session_id"],
        },
    },
}

PROCESS_REGISTRY_KILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "process_registry_kill",
        "description": "Terminate a running background process.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The process session id."},
            },
            "required": ["session_id"],
        },
    },
}

registry.register(
    name="process_registry_list",
    schema=PROCESS_REGISTRY_LIST_SCHEMA,
    handler=_process_registry_list,
    category="terminal",
    toolset="terminal",
    description="List background processes spawned by the agent.",
    emoji="📜",
)

registry.register(
    name="process_registry_tail",
    schema=PROCESS_REGISTRY_TAIL_SCHEMA,
    handler=_process_registry_tail,
    category="terminal",
    toolset="terminal",
    description="Get the tail of a background process's output.",
    emoji="📃",
    max_result_size_chars=50_000,
)

registry.register(
    name="process_registry_kill",
    schema=PROCESS_REGISTRY_KILL_SCHEMA,
    handler=_process_registry_kill,
    category="terminal",
    toolset="terminal",
    description="Terminate a running background process.",
    emoji="🛑",
)
