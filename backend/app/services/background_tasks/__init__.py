"""Background task management system — adapted from OpenHarness.

Provides task_create/get/list/stop/output/update operations.
Tasks run as asyncio background tasks with status polling.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class TaskType(str, Enum):
    SHELL = "shell"
    AGENT = "agent"
    PYTHON = "python"


@dataclass
class BackgroundTask:
    """A background task with lifecycle management."""
    id: str
    name: str
    type: str  # TaskType value
    status: str = TaskStatus.PENDING.value
    output: str = ""
    error: str = ""
    created_at: str = ""
    finished_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    _async_task: asyncio.Task | None = None  # internal: the asyncio task handle

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "output": self.output[-4000:] if len(self.output) > 4000 else self.output,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "metadata": self.metadata,
        }


class BackgroundTaskManager:
    """Manages background tasks with asyncio."""

    def __init__(self):
        self._tasks: dict[str, BackgroundTask] = {}

    def create(
        self,
        name: str,
        type: str = TaskType.SHELL.value,
        command: str | None = None,
        agent_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BackgroundTask:
        """Create a new background task and start it."""
        task_id = str(uuid.uuid4())
        task = BackgroundTask(
            id=task_id,
            name=name,
            type=type,
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )
        self._tasks[task_id] = task

        # Start the task
        if type == TaskType.SHELL.value and command:
            task._async_task = asyncio.create_task(self._run_shell_task(task, command))
        elif type == TaskType.PYTHON.value and command:
            task._async_task = asyncio.create_task(self._run_python_task(task, command))
        elif type == TaskType.AGENT.value:
            task._async_task = asyncio.create_task(self._run_agent_task(task, agent_name, command or ""))
        else:
            task.status = TaskStatus.FAILED.value
            task.error = f"Unknown task type or missing command: {type}"

        return task

    async def _run_shell_task(self, task: BackgroundTask, command: str) -> None:
        """Run a shell command as a background task."""
        task.status = TaskStatus.RUNNING.value
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            task.output = stdout.decode("utf-8", errors="replace")
            if stderr:
                task.output += "\n" + stderr.decode("utf-8", errors="replace")
            task.status = TaskStatus.COMPLETED.value if proc.returncode == 0 else TaskStatus.FAILED.value
            if proc.returncode != 0:
                task.error = f"Exit code: {proc.returncode}"
        except asyncio.CancelledError:
            task.status = TaskStatus.STOPPED.value
        except Exception as e:
            task.status = TaskStatus.FAILED.value
            task.error = str(e)
        finally:
            task.finished_at = datetime.now(timezone.utc).isoformat()

    async def _run_python_task(self, task: BackgroundTask, code: str) -> None:
        """Run Python code as a background task."""
        task.status = TaskStatus.RUNNING.value
        try:
            import io
            import contextlib
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                exec(code, {"__name__": "__background_task__"})
            task.output = stdout_buf.getvalue()
            if stderr_buf.getvalue():
                task.output += "\n" + stderr_buf.getvalue()
            task.status = TaskStatus.COMPLETED.value
        except asyncio.CancelledError:
            task.status = TaskStatus.STOPPED.value
        except Exception as e:
            task.status = TaskStatus.FAILED.value
            task.error = str(e)
        finally:
            task.finished_at = datetime.now(timezone.utc).isoformat()

    async def _run_agent_task(self, task: BackgroundTask, agent_name: str | None, prompt: str) -> None:
        """Run an agent conversation as a background task."""
        task.status = TaskStatus.RUNNING.value
        try:
            from app.services.llm_service import call_llm
            result = await call_llm(prompt=prompt, temperature=0.7)
            task.output = result.get("response", "")
            task.metadata["usage"] = result.get("usage", {})
            task.status = TaskStatus.COMPLETED.value
        except asyncio.CancelledError:
            task.status = TaskStatus.STOPPED.value
        except Exception as e:
            task.status = TaskStatus.FAILED.value
            task.error = str(e)
        finally:
            task.finished_at = datetime.now(timezone.utc).isoformat()

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self, status: str | None = None) -> list[BackgroundTask]:
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def stop(self, task_id: str) -> bool:
        """Stop a running task."""
        task = self._tasks.get(task_id)
        if not task or task.status not in (TaskStatus.RUNNING.value, TaskStatus.PENDING.value):
            return False
        if task._async_task:
            task._async_task.cancel()
        task.status = TaskStatus.STOPPED.value
        task.finished_at = datetime.now(timezone.utc).isoformat()
        return True

    def update_output(self, task_id: str, output: str) -> bool:
        """Append to a task's output."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.output += output
        return True

    def delete(self, task_id: str) -> bool:
        """Delete a task from the registry."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status in (TaskStatus.RUNNING.value, TaskStatus.PENDING.value):
            self.stop(task_id)
        del self._tasks[task_id]
        return True


# Singleton
_manager: BackgroundTaskManager | None = None


def get_task_manager() -> BackgroundTaskManager:
    global _manager
    if _manager is None:
        _manager = BackgroundTaskManager()
    return _manager


__all__ = [
    "TaskStatus", "TaskType", "BackgroundTask", "BackgroundTaskManager",
    "get_task_manager",
]
