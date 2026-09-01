"""Hook loader — clears the executor and registers builtins + DB rules.

Called at app lifespan startup (after schema creation) and re-invoked after
any ``HookRule`` mutation via the /api/hooks CRUD API so the live executor
reflects the change without a restart.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.hook_rule import HookRule
from app.services.hooks import HookConfig, get_hook_executor
from app.services.hooks.registry import BUILTIN_HOOKS

logger = logging.getLogger(__name__)


def _rule_to_config(rule: HookRule) -> HookConfig:
    """Convert a HookRule DB row into a HookConfig for the executor."""
    return HookConfig(
        id=rule.id,
        name=rule.name,
        event=rule.event,
        type=rule.type,
        command=rule.command,
        url=rule.url,
        method=rule.method or "POST",
        headers=rule.headers,
        prompt=rule.prompt,
        timeout=rule.timeout or 30,
        priority=rule.priority or 0,
        matcher=rule.matcher,
        block_on_failure=bool(rule.block_on_failure),
        enabled=bool(rule.enabled),
    )


def load_hooks(db: Session) -> int:
    """Clear the executor and re-register built-in + DB-backed hooks.

    Returns the total number of hooks registered. Safe to call repeatedly
    (idempotent reload). Must be called with a live DB session.
    """
    executor = get_hook_executor()
    executor.clear_hooks()

    count = 0
    # 1. Built-in safety hooks (always registered first).
    for cfg in BUILTIN_HOOKS:
        executor.add_hook(cfg)
        count += 1

    # 2. DB-backed org/app rules (enabled, not soft-deleted).
    try:
        rules = (
            db.query(HookRule)
            .filter(
                HookRule.is_deleted == False,  # noqa: E712
                HookRule.enabled == True,  # noqa: E712
            )
            .all()
        )
        for rule in rules:
            try:
                executor.add_hook(_rule_to_config(rule))
                count += 1
            except Exception as e:
                logger.warning("Failed to register HookRule %s: %s", rule.id, e)
    except Exception as e:
        # Table may not exist yet on first run before create_all; degrade
        # gracefully — builtins are still registered.
        logger.warning("HookRule load skipped (table unavailable?): %s", e)

    logger.info("Hooks loaded: %d (%d builtin + DB rules)", count, len(BUILTIN_HOOKS))
    return count
