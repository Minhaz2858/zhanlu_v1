"""Tests for the profile_data database tool handler.

Covers the JSON function schema shape, kb_id rejection behavior, and
registration presence — no live DB or LLM needed.
"""

import pytest


def test_profile_data_schema_validates():
    from app.services.tool_handlers import db_tools

    schema = db_tools.PROFILE_DATA_SCHEMA
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "profile_data"

    params = fn["parameters"]
    assert params["type"] == "object"
    assert "data_source_id" in params["required"]

    props = params["properties"]
    assert props["data_source_id"]["type"] == "string"
    assert props["table"]["type"] == "string"
    assert props["columns"]["type"] == "array"
    assert props["columns"]["items"]["type"] == "string"
    assert props["max_tables"]["type"] == "integer"
    assert props["max_tables"]["default"] == 8


@pytest.mark.asyncio
async def test_profile_data_rejects_unknown_kb():
    """An unbound / unknown data source id must be rejected up front.

    With `bound_kb_ids` scoping in context, `_require_kb_id` rejects the
    unknown id before any DB work happens (no live DB required).
    """
    from app.services.tool_handlers import db_tools

    res = await db_tools._profile_data(
        {"data_source_id": "missing-kb", "table": "orders", "columns": ["amount"]},
        db=None,
        user_id="u1",
        context={"bound_kb_ids": ["other-kb"]},
    )
    assert res["success"] is False
    assert "data source" in res["error"].lower()


@pytest.mark.asyncio
async def test_profile_data_requires_data_source_id():
    from app.services.tool_handlers import db_tools

    res = await db_tools._profile_data({}, db=None, user_id="u1", context={})
    assert res["success"] is False
    assert "data_source_id" in res["error"]


def test_profile_data_registered():
    """profile_data must be present in the tool registry after import."""
    from app.services.tool_handlers import db_tools
    from app.services.tool_registry import registry

    entry = registry.get_entry("profile_data")
    assert entry is not None
    assert registry.get_handler("profile_data") is db_tools._profile_data
    assert entry.category == "database"
