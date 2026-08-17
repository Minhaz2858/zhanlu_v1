"""Integration test: project LLM binding propagates to the Data Agent.

Verifies the full chain end-to-end:

    resolve_effective_llm (project -> kimi)
        -> context["endpoint"]
        -> delegation_tools._ask_data_agent
        -> _call_llm_with_retry
        -> _call_llm
        -> call_llm_with_reliability
        -> httpx.AsyncClient.post

When a project is bound to a non-default LLM (kimi/moonshot), the Data
Agent's LLM POST must land at kimi's base_url — NOT the global DeepSeek
URL. This is the fix that lets a project keep working when the global
DeepSeek key is out of balance but the project binding has credits.
"""
import asyncio
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import TimestampedBase
from app.models.llm_model import LlmModel
from app.models.project import Project
from app.services.llm_router import resolve_effective_llm

KIMI_BASE_URL = "https://api.moonshot.cn/v1"
KIMI_API_KEY = "sk-kimi-test"
KIMI_MODEL_ID = "kimi-k2.6"


@pytest.fixture(scope="function")
def db():
    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _make_llm_model(db, **kwargs):
    m = LlmModel(
        id=str(uuid.uuid4()),
        name=kwargs.get("name", "Kimi"),
        model_id=kwargs.get("model_id", KIMI_MODEL_ID),
        provider="moonshot",
        base_url=kwargs.get("base_url", KIMI_BASE_URL),
        api_key=kwargs.get("api_key", KIMI_API_KEY),
        is_default=kwargs.get("is_default", False),
        is_private=kwargs.get("is_private", False),
        enabled=kwargs.get("enabled", True),
        org_id=kwargs.get("org_id", "default-org"),
        app_id=kwargs.get("app_id", "default-app"),
    )
    db.add(m)
    db.flush()
    return m


def _make_project(db, llm_model_id):
    p = Project(
        id=str(uuid.uuid4()),
        name="Ecisco BI",
        llm_model_id=llm_model_id,
        resource_type="personal",
        org_id="default-org",
        app_id="default-app",
    )
    db.add(p)
    db.flush()
    return p


def _resolve_project_endpoint(db, project_id):
    """Resolve the effective LLM for a bound project (Rule 1: project wins)."""
    with patch("app.services.llm_router.settings") as mock_settings:
        mock_settings.HIERARCHICAL_LLM_ENABLED = True
        return resolve_effective_llm(
            db,
            project_id=project_id,
            agent_name="general_assistant",
            user_model=None,
            user_is_admin=True,
            org_id="default-org",
            app_id="default-app",
        )


class TestProjectBindingReachesDataAgent:
    def test_project_binding_resolves_kimi_endpoint(self, db):
        kimi = _make_llm_model(db)
        proj = _make_project(db, kimi.id)

        result = _resolve_project_endpoint(db, proj.id)

        assert result.endpoint is not None
        assert result.endpoint.model_id == KIMI_MODEL_ID
        assert result.endpoint.base_url == KIMI_BASE_URL
        assert result.endpoint.api_key == KIMI_API_KEY

    def test_data_agent_posts_to_project_bound_endpoint(self, db):
        """The Data Agent's LLM POST must land at kimi, not global DeepSeek."""
        from app.services.tool_handlers import delegation_tools

        kimi = _make_llm_model(db)
        proj = _make_project(db, kimi.id)
        endpoint = _resolve_project_endpoint(db, proj.id).endpoint

        captured = {}

        async def fake_post(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers") or {}
            captured["payload"] = kwargs.get("json") or {}
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "choices": [
                    {"message": {"content": "Top 3 materials.", "tool_calls": []}}
                ]
            }
            return resp

        with patch("httpx.AsyncClient.post", new=fake_post):
            result = asyncio.run(
                delegation_tools._ask_data_agent(
                    {"question": "top materials by volume"},
                    db,
                    "user-1",
                    context={"endpoint": endpoint, "bound_kb_ids": ["kb-1"]},
                )
            )

        assert captured["url"] == KIMI_BASE_URL + "/chat/completions"
        assert captured["headers"]["Authorization"] == f"Bearer {KIMI_API_KEY}"
        assert captured["payload"]["model"] == KIMI_MODEL_ID
        assert result["success"] is True
        assert result["answer"] == "Top 3 materials."

    def test_data_agent_falls_back_to_global_when_no_endpoint(self, db):
        """Without a project binding, the endpoint kwarg is None (global fallback)."""
        from app.services.tool_handlers import delegation_tools

        captured = {}

        async def fake_retry(messages, tools, endpoint=None):
            captured["endpoint"] = endpoint
            return {"content": "done", "tool_calls": [], "reasoning": ""}

        with patch.object(delegation_tools, "_call_llm_with_retry", new=fake_retry):
            asyncio.run(
                delegation_tools._ask_data_agent(
                    {"question": "top materials"},
                    db,
                    "user-1",
                    context={"bound_kb_ids": ["kb-1"]},
                )
            )

        assert captured["endpoint"] is None
