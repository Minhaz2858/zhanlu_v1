"""Memory scoring: blend cosine similarity with recency and importance.

The legacy ``memory_advanced`` module scores memories by keyword overlap.
This module layers three signals:

* ``cosine``  — semantic similarity (0..1) from a vector store
* ``recency`` — decay over time (1.0 = just now, decays to 0)
* ``importance`` — a stored weight (0..1) supplied when the memory was
  written (e.g. explicit "remember this" / a tool call the user marked
  as critical)

The blended score is::

    final = α * cosine + β * recency + γ * importance

Defaults: α=0.6, β=0.25, γ=0.15.  Callers may override per-call.

This is intentionally a pure module: no I/O, no DB, just math + dataclass
plumbing.  The ``search_memories`` wrapper in ``memory_advanced``
combines this scorer with a vector store and the legacy keyword fallback.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Iterable, Optional

# Default blend weights.  α dominates because cosine is the strongest
# signal in practice; recency keeps "stale but on-topic" memories from
# drowning out fresh ones; importance is a tie-breaker.
DEFAULT_ALPHA = 0.6
DEFAULT_BETA = 0.25
DEFAULT_GAMMA = 0.15

# Recency half-life in seconds (7 days).  After one half-life the
# recency component drops to 0.5; after two, 0.25.  This is gentle
# enough that a 30-day-old "remember this is the project name" memory
# still has weight, while a year-old casual remark does not.
RECENCY_HALF_LIFE_SECONDS = 7 * 24 * 3600.0


@dataclass
class MemoryHit:
    """A single scored memory candidate."""

    id: str
    text: str
    cosine: float
    recency: float
    importance: float
    final_score: float
    metadata: dict


def _recency_score(age_seconds: float, half_life: float = RECENCY_HALF_LIFE_SECONDS) -> float:
    """Exponential decay: 1.0 → 0.5 over ``half_life`` seconds."""
    if age_seconds <= 0:
        return 1.0
    return 0.5 ** (age_seconds / half_life)


def score(
    memory: MemoryHit,
    *,
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
    gamma: float = DEFAULT_GAMMA,
) -> float:
    """Blend the three signals into a final score in [0, 1]."""
    # Clamp to keep a misbehaving caller from blowing past 1.0.
    cosine = max(0.0, min(1.0, memory.cosine))
    recency = max(0.0, min(1.0, memory.recency))
    importance = max(0.0, min(1.0, memory.importance))
    s = alpha * cosine + beta * recency + gamma * importance
    # Normalize so the maximum possible score is 1.0 (all weights sum to 1).
    return max(0.0, min(1.0, s))


def rank(
    candidates: Iterable[MemoryHit],
    *,
    alpha: float = DEFAULT_ALPHA,
    beta: float = DEFAULT_BETA,
    gamma: float = DEFAULT_GAMMA,
) -> list[MemoryHit]:
    """Score each candidate, return sorted descending by final score."""
    out: list[MemoryHit] = []
    now = time.time()
    for cand in candidates:
        # If recency is 0 and we have a timestamp, compute it.
        if cand.recency == 0 and cand.metadata.get("created_at"):
            try:
                age = max(0.0, now - float(cand.metadata["created_at"]))
                cand.recency = _recency_score(age)
            except Exception:
                cand.recency = 0.5  # be generous when we can't tell
        elif cand.recency == 0:
            cand.recency = 0.5
        cand.final_score = score(cand, alpha=alpha, beta=beta, gamma=gamma)
        out.append(cand)
    out.sort(key=lambda m: m.final_score, reverse=True)
    return out


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_BETA",
    "DEFAULT_GAMMA",
    "RECENCY_HALF_LIFE_SECONDS",
    "MemoryHit",
    "rank",
    "score",
]
