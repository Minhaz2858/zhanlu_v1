"""Run a coroutine from synchronous code — including when a loop is running.

Why this exists
---------------
This codebase has sync functions that need an async LLM call, and it reached for
``asyncio.run(coro)``. That is correct in a worker thread and wrong on the event
loop thread, where it raises:

    RuntimeError: Cannot run the event loop while another loop is running

and the failure is easy to hide: the call site's ``except`` turns it into a
fallback string, so the LLM is never called and the user never learns. That is
exactly what happened to ``SynexiaFSM._generate_response`` (see fsm.py:1009) —
every *blocking* FINALIZE answered "I've processed your request." because
``SynexiaFSM.run`` is sync and the chat router calls it directly from
``async def add_message`` (agents.py:6676).

Two contexts are both real here, so callers should not have to know which one they
are in:

* **no loop running** on this thread — worker thread, CLI, script, cron. Plain
  ``asyncio.run`` is correct and cheap.
* **a loop IS running** on this thread — a sync method invoked from an ``async
  def``. Sync code cannot ``await``, and it cannot nest a loop, so the coroutine is
  driven to completion on a dedicated thread with its own loop.

Blocking caveat (read before using on a hot path)
-------------------------------------------------
The second branch blocks the calling thread, and therefore the event loop it runs
on, for the duration of the coroutine. It does not *add* blocking that was not
already there — a sync state machine called from async code was already occupying
the loop — but it does mean a slow coroutine stalls that loop. Prefer ``await``
from async callers; use this only where the sync boundary is already fixed.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def loop_is_running() -> bool:
    """True when the calling thread has a running event loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def run_coro_blocking(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion from sync code and return its result.

    Raises whatever the coroutine raises — callers decide how to report failure.
    This helper never swallows, so a genuine LLM failure stays visible instead of
    being rewritten into a success-shaped string.
    """
    if not loop_is_running():
        return asyncio.run(coro)

    # A loop is running on this thread: nesting is impossible and sync code cannot
    # await, so give the coroutine a thread whose loop is free.
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="coro-bridge"
    ) as pool:
        return pool.submit(asyncio.run, coro).result()


__all__ = ["loop_is_running", "run_coro_blocking"]
