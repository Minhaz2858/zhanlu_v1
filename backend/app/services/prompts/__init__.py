"""Prompt infrastructure package (P2-7): versioned block registry.

The shared system-prompt blocks themselves still live in
``app/services/agent_prompts.py``; this package is the central seam for
versioning, token accounting, and the A/B harness.
"""
from app.services.prompts.registry import (
    BLOCK_REGISTRY,
    BlockSpec,
    get_block,
    iter_blocks,
    registry_snapshot,
)

__all__ = [
    "BLOCK_REGISTRY",
    "BlockSpec",
    "get_block",
    "iter_blocks",
    "registry_snapshot",
]
