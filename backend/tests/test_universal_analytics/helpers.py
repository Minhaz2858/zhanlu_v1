"""
Shared test helpers for universal_analytics E2E tests.
Provides make_ctx() and call_handler() utilities used across all E2E test files.
"""
import asyncio
from unittest.mock import MagicMock


def make_ctx(bound_kb_ids=None):
    """Create a minimal agent context dict for testing universal analytics handlers."""
    return {
        "bound_kb_ids": bound_kb_ids or [],
        "org_id": "default-org",
        "user_id": "test-user",
        "agent_name": "test_agent",
    }


def call_handler(handler, args, db, context=None):
    """Call an async universal_analytics handler synchronously for testing.
    
    Handlers have signature: handler(args: dict, db: Session, context: dict) -> dict
    """
    ctx = context or make_ctx()
    return asyncio.run(handler(args, db, ctx))
