"""In-process event bus for live workflow run streaming (n8n push pattern).

run_workflow_agent() already emits events through an `on_event` callback;
this bus fans those events out to any number of SSE subscribers keyed by
run_id. Subscribers are asyncio.Queues created by the SSE endpoint; the
synchronous run thread publishes via put_nowait (safe from any thread).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

_subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)


def subscribe(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers[run_id].append(q)
    return q


def unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    subs = _subscribers.get(run_id)
    if subs:
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            _subscribers.pop(run_id, None)


def publish(run_id: str, event: dict) -> None:
    for q in list(_subscribers.get(run_id, [])):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:  # slow consumer — drop oldest, keep newest
            try:
                q.get_nowait()
                q.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


def subscriber_count(run_id: str) -> int:
    return len(_subscribers.get(run_id, []))
