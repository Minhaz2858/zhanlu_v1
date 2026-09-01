"""Regression test for I5: entity router handlers are sync (not async def)."""
import os, re

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
from fastapi import APIRouter

import app.models  # noqa: F401
from app.models.agent_app import AgentApp


def test_no_async_def_in_entities_routers():
    """I5: Zero 'async def' handlers in entities.py after fix."""
    with open("app/routers/entities.py") as f:
        src = f.read()
    async_defs = re.findall(r'^\s+async def ', src, re.MULTILINE)
    assert len(async_defs) == 0, \
        f"I5: Found {len(async_defs)} 'async def' — all should be 'def'"


def test_register_entity_router_works_with_sync_handlers():
    """I5: register_entity_router still works after converting to sync handlers."""
    from app.routers.entities import register_entity_router

    router = APIRouter()
    try:
        register_entity_router(router, AgentApp)
    except Exception as e:
        pytest.fail(f"register_entity_router raised {e}")
