"""In-process per-conversation steer message queue registry.

P2 mid-turn steer/interrupt feature. The new ``POST /steer`` endpoint
enqueues a user steer message here; the v3 ``add_message_stream``
SSE generator drains pending messages between tool-loop iterations
and feeds them into the next LLM call as user messages.

Why in-process + ``asyncio.Queue`` (not DB or Redis):
- v3 streaming is single-process; persistence adds latency for no gain.
- Drain happens only at existing iteration boundaries (zero added hot-path
  latency), and bounded queues prevent unbounded growth if the frontend
  spams steer while the loop is busy with a long-running tool.

Thread/process model: this module is called from the async event loop
only. The dict and queues are not protected by a lock; FastAPI runs
the chat endpoints on the same loop so there is no concurrent mutation.
If/when the deployment becomes multi-process, replace this with a
Redis-backed implementation behind the same surface.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Default maxsize per the plan. 20 steer messages per conversation is
# far above any realistic user interaction; it's a safety bound, not a
# quota.
_DEFAULT_MAXSIZE = 20


# Per-conversation queue registry. Keyed by conversation_id (str).
# Touched only from the FastAPI event loop — no lock needed.
_QUEUES: dict[str, asyncio.Queue] = {}


def get_queue(conversation_id: str, maxsize: int = _DEFAULT_MAXSIZE) -> asyncio.Queue:
    """Return the (lazily-created) queue for ``conversation_id``.

    Public for testability; production code uses ``enqueue``/``drain``/
    ``discard`` which create the queue implicitly.
    """
    q = _QUEUES.get(conversation_id)
    if q is None:
        q = asyncio.Queue(maxsize=maxsize)
        _QUEUES[conversation_id] = q
    return q


def enqueue(conversation_id: str, message: str, maxsize: int = _DEFAULT_MAXSIZE) -> bool:
    """Enqueue a steer message for ``conversation_id``.

    Returns ``True`` on success, ``False`` if the queue is full
    (caller — the HTTP endpoint — should surface a 429 in that case).
    Never raises; never blocks (put_nowait is non-blocking).
    """
    q = get_queue(conversation_id, maxsize=maxsize)
    try:
        q.put_nowait(message)
        return True
    except asyncio.QueueFull:
        logger.warning(
            "steer_bus: queue full for conversation=%s (maxsize=%d); dropping",
            conversation_id, maxsize,
        )
        return False


def drain(conversation_id: str) -> list[str]:
    """Drain all currently-pending steer messages for ``conversation_id``.

    Non-blocking (uses ``get_nowait``). Returns messages in FIFO order.
    Returns ``[]`` if there is no queue for the conversation or the
    queue is empty. The queue itself is preserved so subsequent
    enqueues continue to accumulate.
    """
    q = _QUEUES.get(conversation_id)
    if q is None:
        return []
    out: list[str] = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def discard(conversation_id: str) -> None:
    """Remove the queue for ``conversation_id`` entirely.

    Called on stream completion (done/error/finally) so queues do not
    leak across turns. No-op if the conversation has no queue.
    """
    existed = _QUEUES.pop(conversation_id, None)
    if existed is not None:
        logger.debug("steer_bus: discarded queue for conversation=%s", conversation_id)
