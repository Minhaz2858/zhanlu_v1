"""Learning graph -- track successful/failed techniques across sessions.

Records what worked and what didn't for each agent: which tools solved
which problems, which approaches failed, which techniques the user
preferred. These learnings are injected into the system prompt so future
turns benefit from past experience.

Storage is lightweight: a JSON file per agent (no DB table needed).
Designed to be read at conversation start and written at turn end.

Inspired by Hermes' learning graph patterns.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default storage directory for learning graphs
_DEFAULT_STORAGE_DIR = os.path.join(
    os.environ.get("ZHANLU_DATA_DIR", "/root/zhanlu"),
    "learning_graphs",
)

# Maximum learnings per agent (old ones are pruned)
_MAX_LEARNINGS = 50

# Minimum confidence to include a learning in the prompt (0-1)
_MIN_PROMPT_CONFIDENCE = 0.4


@dataclass
class Learning:
    """A single learning entry."""
    technique: str           # what was done (e.g. "used pytest with -v flag")
    outcome: str             # "success" or "failure"
    context: str             # what problem it solved/failed on
    confidence: float        # 0-1, increases with repetitions
    timestamp: str           # ISO datetime
    tool: str = ""           # which tool was involved

    def to_dict(self) -> dict[str, Any]:
        return {
            "technique": self.technique,
            "outcome": self.outcome,
            "context": self.context,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
            "tool": self.tool,
        }


@dataclass
class LearningGraph:
    """A collection of learnings for one agent."""
    agent_app_id: str
    learnings: list[Learning] = field(default_factory=list)

    def add_learning(
        self,
        technique: str,
        outcome: str,
        context: str = "",
        tool: str = "",
        confidence_boost: float = 0.3,
    ) -> None:
        """Add or reinforce a learning.

        If the same technique+outcome already exists, increment its confidence.
        Otherwise, create a new entry with initial confidence.
        """
        # Check for existing
        for learning in self.learnings:
            if learning.technique == technique and learning.outcome == outcome:
                learning.confidence = min(1.0, learning.confidence + confidence_boost)
                learning.timestamp = datetime.utcnow().isoformat()
                return

        # New learning
        self.learnings.append(Learning(
            technique=technique,
            outcome=outcome,
            context=context,
            confidence=confidence_boost,
            timestamp=datetime.utcnow().isoformat(),
            tool=tool,
        ))

        # Prune if too many
        if len(self.learnings) > _MAX_LEARNINGS:
            # Keep highest confidence, most recent
            self.learnings.sort(key=lambda l: (-l.confidence, l.timestamp), reverse=False)
            self.learnings = self.learnings[:_MAX_LEARNINGS]

    def get_successful_techniques(self, min_confidence: float = _MIN_PROMPT_CONFIDENCE) -> list[Learning]:
        """Get high-confidence successful techniques for prompt injection."""
        return [
            l for l in self.learnings
            if l.outcome == "success" and l.confidence >= min_confidence
        ]

    def get_warnings(self, min_confidence: float = _MIN_PROMPT_CONFIDENCE) -> list[Learning]:
        """Get high-confidence failure warnings for prompt injection."""
        return [
            l for l in self.learnings
            if l.outcome == "failure" and l.confidence >= min_confidence
        ]

    def to_prompt_text(self) -> str:
        """Format learnings as text for system prompt injection."""
        successes = self.get_successful_techniques()
        warnings = self.get_warnings()

        if not successes and not warnings:
            return ""

        lines = ["[Learned from past sessions]"]

        if successes:
            lines.append("Techniques that worked well:")
            for s in successes[:5]:
                lines.append(f"  - {s.technique}" + (f" (for: {s.context})" if s.context else ""))

        if warnings:
            lines.append("Approaches to avoid:")
            for w in warnings[:3]:
                lines.append(f"  - {w.technique}" + (f" (failed on: {w.context})" if w.context else ""))

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_app_id": self.agent_app_id,
            "learnings": [l.to_dict() for l in self.learnings],
        }


# -- Storage --

def _get_storage_path(agent_app_id: str, storage_dir: str | None = None) -> Path:
    """Get the file path for an agent's learning graph."""
    base = Path(storage_dir or _DEFAULT_STORAGE_DIR)
    base.mkdir(parents=True, exist_ok=True)
    safe_id = agent_app_id.replace("/", "_").replace("\\", "_")
    return base / f"{safe_id}.json"


def load_learning_graph(
    agent_app_id: str,
    storage_dir: str | None = None,
) -> LearningGraph:
    """Load a learning graph from disk. Returns empty if not found."""
    path = _get_storage_path(agent_app_id, storage_dir)
    if not path.exists():
        return LearningGraph(agent_app_id=agent_app_id)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        learnings = [
            Learning(**l) for l in data.get("learnings", [])
            if isinstance(l, dict)
        ]
        return LearningGraph(agent_app_id=agent_app_id, learnings=learnings)
    except Exception as e:
        logger.warning("Failed to load learning graph for %s: %s", agent_app_id, e)
        return LearningGraph(agent_app_id=agent_app_id)


def save_learning_graph(
    graph: LearningGraph,
    storage_dir: str | None = None,
) -> None:
    """Save a learning graph to disk."""
    path = _get_storage_path(graph.agent_app_id, storage_dir)
    try:
        path.write_text(
            json.dumps(graph.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save learning graph for %s: %s", graph.agent_app_id, e)


def record_learning(
    agent_app_id: str,
    technique: str,
    outcome: str,
    context: str = "",
    tool: str = "",
    confidence_boost: float = 0.3,
    storage_dir: str | None = None,
) -> None:
    """Record a learning and persist to disk. Fire-and-forget."""
    try:
        graph = load_learning_graph(agent_app_id, storage_dir)
        graph.add_learning(technique, outcome, context, tool, confidence_boost=confidence_boost)
        save_learning_graph(graph, storage_dir)
    except Exception as e:
        logger.debug("Failed to record learning: %s", e)


def get_learning_prompt(
    agent_app_id: str,
    storage_dir: str | None = None,
) -> str:
    """Get the learning graph text for system prompt injection."""
    graph = load_learning_graph(agent_app_id, storage_dir)
    return graph.to_prompt_text()


__all__ = [
    "Learning",
    "LearningGraph",
    "load_learning_graph",
    "save_learning_graph",
    "record_learning",
    "get_learning_prompt",
]
