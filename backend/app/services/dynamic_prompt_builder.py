"""Dynamic prompt builder -- unified system prompt assembly.

Replaces the ad-hoc ``system_prompt += ...`` injections in the turn loop
with a single, ordered, configurable builder. Assembles the system prompt
from:

1. Base prompt (from ``get_system_prompt()``)
2. Memory snapshot (frozen at conversation start)
3. Todo list snapshot
4. Coding context (project facts: test commands, languages)
5. Learning graph (cross-session technique tracking)
6. Conversation context (follow-up awareness)

Each section is best-effort: a failure in any injection is logged and
skipped, never crashing the turn.

Inspired by Hermes' prompt builder patterns.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def build_system_prompt(
    *,
    base_prompt: str,
    db: Session,
    agent_app_id: str,
    conversation_id: str,
    user_id: str | None = None,
    agent_app: Any = None,
    workspace_path: str | None = None,
    # Project scope (2026-08-05). Forwarded to ``load_memory_snapshot``
    # so the agent's notes (target='memory') are recalled only from
    # the active project. Without this, a note taken in one project
    # leaked into every other project's system prompt (the user saw
    # the same "Q2 2026 sales report" greeting in every conv).
    project_id: str | None = None,
    inject_memory: bool = True,
    inject_todos: bool = True,
    inject_coding_context: bool = True,
    inject_learning_graph: bool = True,
    inject_recipes: bool = True,
    inject_user_profile: bool = True,
) -> str:
    """Build the complete system prompt from all sources.

    Args:
        base_prompt: The base system prompt from ``get_system_prompt()``.
        db: Database session for memory/todo lookups.
        agent_app_id: Agent app ID for memory/learning scoping.
        conversation_id: Conversation ID for todo lookups.
        user_id: Optional user ID for memory scoping.
        agent_app: Optional AgentApp object for workspace detection.
        workspace_path: Optional workspace path for coding context.
        project_id: Optional project scope for the memory snapshot.
        inject_*: Toggle each injection on/off.

    Returns:
        The complete system prompt string.
    """
    prompt = base_prompt

    # 1. Memory snapshot
    if inject_memory:
        prompt = _inject_memory(prompt, db, agent_app_id, user_id, project_id=project_id)

    # 2. Todo list
    if inject_todos:
        prompt = _inject_todos(prompt, db, conversation_id)

    # 3. Coding context (project facts)
    if inject_coding_context:
        prompt = _inject_coding_context(prompt, agent_app, workspace_path)

    # 4. Learning graph
    if inject_learning_graph:
        prompt = _inject_learning_graph(prompt, agent_app_id)

    # 5. Proven answer recipes (experience layer Layer 1) — flag-gated
    if inject_recipes:
        prompt = _inject_recipes(prompt, agent_app_id)

    # 6. User profile (experience layer Layer 3) — flag-gated
    if inject_user_profile:
        prompt = _inject_user_profile(prompt, agent_app_id, user_id)

    return prompt


def _inject_memory(
    prompt: str,
    db: Session,
    agent_app_id: str,
    user_id: str | None,
    project_id: str | None = None,
) -> str:
    """Inject frozen memory snapshot into the prompt.

    ``project_id`` is forwarded to ``load_memory_snapshot`` so the
    agent's notes (target='memory') are recalled only from the
    active project. User profile (target='user') is always
    cross-project — it's about WHO the user is.
    """
    try:
        from app.services.tool_handlers.memory_tool import load_memory_snapshot
        snapshot = load_memory_snapshot(db, agent_app_id, user_id, project_id=project_id)
        if snapshot["memory"]:
            prompt += f"\n\n{snapshot['memory']}"
        if snapshot["user"]:
            prompt += f"\n\n{snapshot['user']}"
    except Exception as e:
        logger.debug("Memory injection failed (non-fatal): %s", e)
    return prompt


def _inject_todos(prompt: str, db: Session, conversation_id: str) -> str:
    """Inject active todo list into the prompt."""
    try:
        from app.services.tool_handlers.todo_tool import load_todo_snapshot
        snapshot = load_todo_snapshot(db, conversation_id)
        if snapshot:
            prompt += f"\n\n{snapshot}"
    except Exception as e:
        logger.debug("Todo injection failed (non-fatal): %s", e)
    return prompt


def _inject_coding_context(
    prompt: str,
    agent_app: Any,
    workspace_path: str | None,
) -> str:
    """Inject detected project facts into the prompt."""
    try:
        from app.services.coding_context import detect_project_facts
        workspace = workspace_path or getattr(agent_app, "workspace_dir", None) or os.getcwd()
        facts = detect_project_facts(workspace)
        if facts.languages or facts.test_commands:
            lines = ["[Project Context]"]
            if facts.languages:
                lines.append(f"Languages: {', '.join(facts.languages)}")
            if facts.test_commands:
                lines.append(f"Test commands: {', '.join(facts.test_commands)}")
            if facts.build_commands:
                lines.append(f"Build commands: {', '.join(facts.build_commands)}")
            if facts.lint_commands:
                lines.append(f"Lint commands: {', '.join(facts.lint_commands)}")
            if facts.framework:
                lines.append(f"Framework: {facts.framework}")
            prompt += "\n\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("Coding context injection failed (non-fatal): %s", e)
    return prompt


def _inject_learning_graph(prompt: str, agent_app_id: str) -> str:
    """Inject cross-session learning graph into the prompt."""
    try:
        from app.services.learning_graph import get_learning_prompt
        learning_text = get_learning_prompt(agent_app_id)
        if learning_text:
            prompt += f"\n\n{learning_text}"
    except Exception as e:
        logger.debug("Learning graph injection failed (non-fatal): %s", e)
    return prompt


def _inject_recipes(prompt: str, agent_app_id: str) -> str:
    """Inject proven answer recipes (Layer 1) into the prompt.

    Flag-gated by ECISCO_BI_RECIPE_LEARNING_ENABLED — off means no recipes
    are recorded, so nothing to inject anyway; the guard also protects
    non-eisco deployments where the store is empty.
    """
    try:
        from app.config import settings
        if not settings.ECISCO_BI_RECIPE_LEARNING_ENABLED:
            return prompt
        from app.services.learning_graph import get_recipe_prompt
        recipe_text = get_recipe_prompt(agent_app_id)
        if recipe_text:
            prompt += f"\n\n{recipe_text}"
    except Exception as e:
        logger.debug("Recipe injection failed (non-fatal): %s", e)
    return prompt


def _inject_user_profile(prompt: str, agent_app_id: str, user_id: str | None) -> str:
    """Inject the user's profile (Layer 3) into the prompt.

    Flag-gated by ECISCO_BI_USER_PROFILE_ENABLED. Only injected when a
    user_id is known (per-user memory).
    """
    try:
        if not user_id:
            return prompt
        from app.config import settings
        if not settings.ECISCO_BI_USER_PROFILE_ENABLED:
            return prompt
        from app.services.user_profile import get_profile_prompt
        profile_text = get_profile_prompt(agent_app_id, user_id)
        if profile_text:
            prompt += f"\n\n{profile_text}"
    except Exception as e:
        logger.debug("User profile injection failed (non-fatal): %s", e)
    return prompt


__all__ = ["build_system_prompt"]
