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
from datetime import datetime, timezone
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
                learning.timestamp = datetime.now(timezone.utc).isoformat()
                return

        # New learning
        self.learnings.append(Learning(
            technique=technique,
            outcome=outcome,
            context=context,
            confidence=confidence_boost,
            timestamp=datetime.now(timezone.utc).isoformat(),
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


# --------------------------------------------------------------------------- #
# Recipe store (Layer 1 — experience layer)
#
# A Recipe records the ordered tool sequence that produced a good answer for a
# question intent class (e.g. "price_report" -> ask_pricing -> ask_report).
# Recipes are recorded at TURN END (not per tool call) and injected into the
# system prompt as a playbook so future turns follow proven recipes instead of
# exploring. Stored in the same JSON-file-per-agent pattern as the learning
# graph, in a separate ".recipes.json" file so the two stores never clobber
# each other.
# --------------------------------------------------------------------------- #

# Maximum recipes kept per agent (pruned by confidence, oldest first)
_MAX_RECIPES = 30

# Minimum confidence for a recipe to appear in the injected playbook
_MIN_RECIPE_PROMPT_CONFIDENCE = 0.6


@dataclass
class Recipe:
    """A proven tool sequence for answering a question intent class."""
    intent_class: str            # "price_report" | "market_analysis" | ...
    tool_sequence: list[str]     # ordered tools that produced a good answer
    success_count: int = 0
    failure_count: int = 0
    confidence: float = 0.0      # 0-1, Laplace-smoothed success ratio
    avg_iterations: float = 0.0  # average LLM/tool rounds for this recipe
    last_used: str = ""          # ISO datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_class": self.intent_class,
            "tool_sequence": self.tool_sequence,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "confidence": self.confidence,
            "avg_iterations": self.avg_iterations,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Recipe":
        return cls(
            intent_class=str(data.get("intent_class", "")),
            tool_sequence=list(data.get("tool_sequence", []) or []),
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            confidence=float(data.get("confidence", 0.0)),
            avg_iterations=float(data.get("avg_iterations", 0.0)),
            last_used=str(data.get("last_used", "")),
        )


def _get_recipe_storage_path(agent_app_id: str, storage_dir: str | None = None) -> Path:
    """Get the file path for an agent's recipe store."""
    base = Path(storage_dir or _DEFAULT_STORAGE_DIR)
    base.mkdir(parents=True, exist_ok=True)
    safe_id = agent_app_id.replace("/", "_").replace("\\", "_")
    return base / f"{safe_id}.recipes.json"


def load_recipes(
    agent_app_id: str,
    storage_dir: str | None = None,
) -> list[Recipe]:
    """Load an agent's recipes from disk. Returns empty list if not found."""
    path = _get_recipe_storage_path(agent_app_id, storage_dir)
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [
            Recipe.from_dict(item)
            for item in data.get("recipes", [])
            if isinstance(item, dict)
        ]
    except Exception as e:
        logger.warning("Failed to load recipes for %s: %s", agent_app_id, e)
        return []


def _save_recipes(
    agent_app_id: str,
    recipes: list[Recipe],
    storage_dir: str | None = None,
) -> None:
    """Save an agent's recipes to disk (fire-and-forget, best-effort)."""
    path = _get_recipe_storage_path(agent_app_id, storage_dir)
    try:
        path.write_text(
            json.dumps(
                {"agent_app_id": agent_app_id, "recipes": [r.to_dict() for r in recipes]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save recipes for %s: %s", agent_app_id, e)


def _recipe_confidence(success_count: int, failure_count: int) -> float:
    """Laplace-smoothed success ratio in [0, 1]."""
    return (success_count + 1.0) / (success_count + failure_count + 2.0)


def record_recipe(
    agent_app_id: str,
    intent_class: str,
    tool_sequence: list[str] | tuple[str, ...],
    success: bool,
    iterations: int = 0,
    storage_dir: str | None = None,
) -> None:
    """Record (or reinforce) a recipe at turn end. Fire-and-forget.

    Matching is by (intent_class, tool_sequence): the same sequence for the
    same intent reinforces confidence; a different sequence for the same
    intent becomes a distinct recipe (the best one wins in the prompt).
    """
    try:
        recipes = load_recipes(agent_app_id, storage_dir)
        seq = list(tool_sequence)

        now = datetime.now(timezone.utc).isoformat()
        match = next(
            (r for r in recipes if r.intent_class == intent_class and r.tool_sequence == seq),
            None,
        )

        if match is None:
            match = Recipe(
                intent_class=intent_class,
                tool_sequence=seq,
                success_count=1 if success else 0,
                failure_count=0 if success else 1,
                last_used=now,
            )
            recipes.append(match)
        else:
            if success:
                match.success_count += 1
            else:
                match.failure_count += 1
            match.last_used = now

        total = match.success_count + match.failure_count
        match.confidence = _recipe_confidence(match.success_count, match.failure_count)
        if iterations > 0:
            match.avg_iterations = (
                (match.avg_iterations * (total - 1) + iterations) / total
                if total > 1
                else float(iterations)
            )

        # Prune: keep the most-confident, most-recent recipes.
        if len(recipes) > _MAX_RECIPES:
            recipes.sort(
                key=lambda r: (-r.confidence, r.last_used or ""),
            )
            recipes = recipes[:_MAX_RECIPES]

        _save_recipes(agent_app_id, recipes, storage_dir)
    except Exception as e:
        logger.debug("Failed to record recipe: %s", e)


def adjust_recipe_feedback(
    agent_app_id: str,
    intent_class: str,
    rating: int,
    storage_dir: str | None = None,
) -> None:
    """Reinforce (thumbs up) or penalize (thumbs down) the best recipe for
    an intent class. Fire-and-forget.

    Adjusts the most-confident recipe matching ``intent_class``; when no
    recipe exists for the intent there is nothing to adjust and the call
    is a no-op.
    """
    try:
        recipes = load_recipes(agent_app_id, storage_dir)
        candidates = [r for r in recipes if r.intent_class == intent_class]
        if not candidates:
            return
        match = max(candidates, key=lambda r: (r.confidence, r.success_count))
        if rating > 0:
            match.success_count += 1
        elif rating < 0:
            match.failure_count += 1
        else:
            return
        match.last_used = datetime.now(timezone.utc).isoformat()
        match.confidence = _recipe_confidence(match.success_count, match.failure_count)
        _save_recipes(agent_app_id, recipes, storage_dir)
    except Exception as e:
        logger.debug("Failed to adjust recipe feedback: %s", e)


def get_recipe_prompt(
    agent_app_id: str,
    storage_dir: str | None = None,
) -> str:
    """Build the playbook text for system prompt injection.

    Only recipes with confidence >= _MIN_RECIPE_PROMPT_CONFIDENCE are shown,
    so a recipe needs at least ~2 successes (or 2-0) before it is trusted.
    """
    recipes = load_recipes(agent_app_id, storage_dir)
    good = [
        r for r in recipes
        if r.confidence >= _MIN_RECIPE_PROMPT_CONFIDENCE and r.tool_sequence
    ]
    if not good:
        return ""

    # Best recipe per intent class only (avoid contradictory playbooks).
    best_by_intent: dict[str, Recipe] = {}
    for r in good:
        cur = best_by_intent.get(r.intent_class)
        if cur is None or r.confidence > cur.confidence:
            best_by_intent[r.intent_class] = r

    lines = ["[Proven answer recipes by question type]"]
    for intent in sorted(best_by_intent):
        r = best_by_intent[intent]
        lines.append(f'Question type "{intent}":')
        lines.append("  Tool sequence that works: " + " -> ".join(r.tool_sequence))
        lines.append(
            f"  (confidence {r.confidence:.2f}, {r.success_count} success"
            f"/{r.failure_count} fail, avg {r.avg_iterations:.1f} iterations)"
        )
    return "\n".join(lines)


__all__ = [
    "Learning",
    "LearningGraph",
    "load_learning_graph",
    "save_learning_graph",
    "record_learning",
    "get_learning_prompt",
    "Recipe",
    "load_recipes",
    "record_recipe",
    "get_recipe_prompt",
]
