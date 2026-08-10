"""General-purpose Redis-based task queue.

Extends the existing sandbox Redis queue pattern to support arbitrary
task types: long-running agent work, document generation, batch analysis.

Redis key structure:
- ``task:queue:{task_type}`` — RPUSH (enqueue) / LPOP (dequeue)
- ``task:active:{task_id}`` — task metadata (status, created_at, payload)
- ``task:dlq:{task_type}`` — Dead Letter Queue (tasks that failed N times)

Degrades to in-process memory queue when Redis is unavailable.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

QUEUE_KEY_PREFIX = "task:queue:"
TASK_META_PREFIX = "task:active:"
DLQ_KEY_PREFIX = "task:dlq:"

# In-memory fallback (per-process, no cross-worker sharing)
_memory_queues: dict[str, list[dict]] = {}
_memory_dlq: dict[str, list[dict]] = {}
_task_meta: dict[str, dict] = {}


@dataclass
class TaskInfo:
    task_id: str
    task_type: str
    status: str = "queued"   # queued | running | completed | failed
    created_at: float = 0.0
    payload: dict = field(default_factory=dict)
    retry_count: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status,
            "created_at": self.created_at,
            "payload": self.payload,
            "retry_count": self.retry_count,
        }


def _get_redis_client():
    from app.database import get_redis
    return get_redis()


def enqueue(task_type: str, payload: dict) -> Optional[str]:
    """Enqueue a task. Returns task_id or None on failure."""
    task_id = uuid.uuid4().hex[:16]
    task_info = TaskInfo(
        task_id=task_id,
        task_type=task_type,
        created_at=time.time(),
        payload=payload,
    )

    r = _get_redis_client()
    if r is not None:
        try:
            r.setex(
                f"{TASK_META_PREFIX}{task_id}",
                86400,  # 24h TTL
                json.dumps(task_info.to_dict()),
            )
            r.rpush(f"{QUEUE_KEY_PREFIX}{task_type}", task_id)
            logger.debug("Enqueued task %s (type=%s) on Redis", task_id, task_type)
            return task_id
        except Exception as e:
            logger.warning("Redis enqueue failed — falling back to memory: %s", e)

    # Memory fallback
    if task_type not in _memory_queues:
        _memory_queues[task_type] = []
    _memory_queues[task_type].append({"task_id": task_id, "payload": payload})
    _task_meta[task_id] = task_info.to_dict()
    logger.debug("Enqueued task %s (type=%s) in memory", task_id, task_type)
    return task_id


def dequeue(task_type: str, timeout: float = 5.0) -> Optional[TaskInfo]:
    """Dequeue a task. Returns TaskInfo or None if queue is empty.

    Blocks up to ``timeout`` seconds when using Redis (BLPOP).
    Non-blocking for memory fallback.
    """
    r = _get_redis_client()
    if r is not None:
        try:
            result = r.blpop(f"{QUEUE_KEY_PREFIX}{task_type}", timeout=int(timeout))
            if result is None:
                return None
            _, task_id = result
            if isinstance(task_id, bytes):
                task_id = task_id.decode()
            meta_raw = r.get(f"{TASK_META_PREFIX}{task_id}")
            if meta_raw:
                meta = json.loads(meta_raw) if isinstance(meta_raw, bytes) else json.loads(str(meta_raw))
                meta["status"] = "running"
                r.setex(f"{TASK_META_PREFIX}{task_id}", 86400, json.dumps(meta))
                return TaskInfo(**meta)
        except Exception as e:
            logger.warning("Redis dequeue failed — falling back to memory: %s", e)

    # Memory fallback
    queue = _memory_queues.get(task_type, [])
    if not queue:
        return None
    entry = queue.pop(0)
    meta = _task_meta.get(entry["task_id"], {})
    meta["status"] = "running"
    _task_meta[entry["task_id"]] = meta
    return TaskInfo(task_id=entry["task_id"], task_type=task_type, status="running", payload=entry["payload"])


def mark_complete(task_id: str) -> bool:
    """Mark a task as completed and remove from active set."""
    r = _get_redis_client()
    if r is not None:
        try:
            r.delete(f"{TASK_META_PREFIX}{task_id}")
            return True
        except Exception:
            pass
    _task_meta.pop(task_id, None)
    return True


def mark_failed(task_id: str, error: str = "", max_retries: int = 3) -> bool:
    """Mark a task as failed. If retries remain, re-enqueue. If max retries exceeded, move to DLQ."""
    r = _get_redis_client()
    if r is not None:
        try:
            meta_raw = r.get(f"{TASK_META_PREFIX}{task_id}")
            if meta_raw:
                meta = json.loads(meta_raw) if isinstance(meta_raw, bytes) else meta_raw
                meta["retry_count"] = meta.get("retry_count", 0) + 1
                meta["last_error"] = error
                if meta["retry_count"] < max_retries:
                    meta["status"] = "queued"
                    r.setex(f"{TASK_META_PREFIX}{task_id}", 86400, json.dumps(meta))
                    r.rpush(f"{QUEUE_KEY_PREFIX}{meta['task_type']}", task_id)
                else:
                    r.delete(f"{TASK_META_PREFIX}{task_id}")
                    r.rpush(f"{DLQ_KEY_PREFIX}{meta['task_type']}", json.dumps(meta))
                    logger.warning("Task %s exhausted retries (%d) — moved to DLQ", task_id, max_retries)
            return True
        except Exception:
            pass

    meta = _task_meta.get(task_id)
    if meta:
        meta["retry_count"] = meta.get("retry_count", 0) + 1
        if meta["retry_count"] < max_retries and meta.get("task_type"):
            enqueue(meta["task_type"], meta.get("payload", {}))
        else:
            _task_meta.pop(task_id, None)
            if meta.get("task_type") not in _memory_dlq:
                _memory_dlq[meta["task_type"]] = []
            _memory_dlq[meta["task_type"]].append(meta)
    return True


def queue_length(task_type: str) -> int:
    """Return the current queue length for a task type."""
    r = _get_redis_client()
    if r is not None:
        try:
            return r.llen(f"{QUEUE_KEY_PREFIX}{task_type}") or 0
        except Exception:
            pass
    return len(_memory_queues.get(task_type, []))


def queue_status() -> dict:
    """Return a summary of all queues for monitoring."""
    r = _get_redis_client()
    result = {}
    if r is not None:
        try:
            # Scan for queue keys
            keys = []
            cursor = 0
            while True:
                cursor, batch = r.scan(cursor, match=f"{QUEUE_KEY_PREFIX}*", count=100)
                keys.extend(batch)
                if cursor == 0:
                    break
            for k in keys:
                k_str = k.decode() if isinstance(k, bytes) else k
                task_type = k_str[len(QUEUE_KEY_PREFIX):]
                result[task_type] = r.llen(k) or 0
        except Exception:
            pass
    for k, v in _memory_queues.items():
        result[k] = result.get(k, 0) + len(v)
    return result


__all__ = [
    "TaskInfo",
    "enqueue",
    "dequeue",
    "mark_complete",
    "mark_failed",
    "queue_length",
    "queue_status",
]
