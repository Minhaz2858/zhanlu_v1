"""Tests for the profile_data database tool handler.

Covers the JSON function schema shape, kb_id rejection behavior, registration
presence, access-policy filtering (KB / table / column level), list_tables
failure handling, max_tables clamping, column extraction, and per-table error
shapes — all monkeypatch-based, no live DB or LLM needed.
"""

import pytest
from unittest.mock import patch


def _mk_policy(blocked_kbs=None, denied=None, allowed=None, columns=None):
    """Build a ResolvedPolicy with the requested restrictions (lower-cased)."""
    from app.services import access_policy_service

    return access_policy_service.ResolvedPolicy(
        blocked_kb_ids=set(blocked_kbs or []),
        denied_tables={(k, t): "deny" for (k, t) in (denied or [])},
        allowlisted_kbs={k: set(v) for k, v in (allowed or {}).items()},
        column_allowlists={(k, t): cols for (k, t), cols in (columns or {}).items()},
        has_policies=True,
    )


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


# ── policy: KB fully denied ─────────────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "app.services.tool_handlers.db_tools._resolve_user_policy",
    return_value=_mk_policy(blocked_kbs=["kb1"]),
)
async def test_profile_data_kb_fully_denied(mock_policy):
    """A KB the user cannot see at all must return the restricted error."""
    from app.services.tool_handlers import db_tools

    res = await db_tools._profile_data(
        {"kb_id": "kb1", "table": "orders"}, db=None, user_id="u1", context={}
    )
    assert res["success"] is False
    assert "restricted" in res["error"].lower()
    mock_policy.assert_called_once()


# ── policy: table-level filtering ───────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "app.services.tool_handlers.db_tools._resolve_user_policy",
    return_value=_mk_policy(denied=[("kb1", "secrets")]),
)
async def test_profile_data_blocked_table_rejected(mock_policy):
    """A user-requested table that is denied must be rejected before profiling."""
    from app.services.tool_handlers import db_tools

    res = await db_tools._profile_data(
        {"kb_id": "kb1", "table": "Secrets"}, db=None, user_id="u1", context={}
    )
    assert res["success"] is False
    assert "table is restricted" in res["error"].lower()


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    return_value={"table": "Orders", "row_count": 5, "status": "ok", "columns": []},
)
@patch(
    "app.services.tool_handlers.db_tools._resolve_user_policy",
    return_value=_mk_policy(denied=[("kb1", "secrets")]),
)
async def test_profile_data_allowed_table_profiled(mock_policy, mock_profile):
    """An allowed table proceeds to profiling with the requested columns."""
    from app.services.tool_handlers import db_tools

    res = await db_tools._profile_data(
        {"kb_id": "kb1", "table": "Orders", "columns": ["amount"]},
        db=object(),
        user_id="u1",
        context={},
    )
    assert res["success"] is True
    assert len(res["tables"]) == 1
    mock_profile.assert_called_once()
    args, _ = mock_profile.call_args
    assert args[1] == "kb1"
    assert args[2] == "Orders"
    assert args[3] == ["amount"]


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    side_effect=lambda db, kb, t, cols: {
        "table": t, "row_count": 1, "status": "ok", "columns": [],
    },
)
@patch("app.services.tool_handlers.db_tools.SchemaService")
@patch(
    "app.services.tool_handlers.db_tools._resolve_user_policy",
    return_value=_mk_policy(denied=[("kb1", "secrets")]),
)
async def test_profile_data_listing_filters_blocked_tables(
    mock_policy, mock_svc, mock_profile
):
    """Tables returned by list_tables are filtered against the policy."""
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.return_value = {"tables": ["Orders", "Secrets"]}
    res = await db_tools._profile_data(
        {"kb_id": "kb1"}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is True
    assert [t["table"] for t in res["tables"]] == ["Orders"]


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    return_value={"table": "Orders", "row_count": 1, "status": "ok", "columns": []},
)
@patch(
    "app.services.tool_handlers.db_tools._resolve_user_policy",
    return_value=_mk_policy(allowed={"kb1": {"orders"}}),
)
async def test_profile_data_whitelist_mode_blocks_other_tables(mock_policy, mock_profile):
    """Whitelist-mode KBs (KB deny + table allows) restrict profiling too."""
    from app.services.tool_handlers import db_tools

    res = await db_tools._profile_data(
        {"kb_id": "kb1", "table": "Customers"}, db=None, user_id="u1", context={}
    )
    assert res["success"] is False
    assert "table is restricted" in res["error"].lower()


# ── policy: column-level filtering ──────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    return_value={"table": "Orders", "row_count": 0, "status": "ok", "columns": []},
)
@patch(
    "app.services.tool_handlers.db_tools._resolve_user_policy",
    return_value=_mk_policy(columns={("kb1", "orders"): ["id"]}),
)
async def test_profile_data_filters_columns_by_allowlist(mock_policy, mock_profile):
    """Requested columns outside the column allowlist are dropped."""
    from app.services.tool_handlers import db_tools

    res = await db_tools._profile_data(
        {"kb_id": "kb1", "table": "Orders", "columns": ["id", "amount"]},
        db=object(),
        user_id="u1",
        context={},
    )
    assert res["success"] is True
    args, _ = mock_profile.call_args
    assert args[3] == ["id"]


# ── max_tables clamp ────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    side_effect=lambda db, kb, t, cols: {
        "table": t, "row_count": 1, "status": "ok", "columns": [],
    },
)
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_max_tables_clamped(mock_svc, mock_profile):
    """max_tables above the ceiling is clamped to 8."""
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.return_value = {
        "tables": [f"t{i}" for i in range(12)]
    }
    mock_svc.return_value.describe_table.return_value = {"columns": []}
    res = await db_tools._profile_data(
        {"kb_id": "kb1", "max_tables": 99}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is True
    assert len(res["tables"]) == 8
    assert mock_profile.call_count == 8


# ── column extraction ───────────────────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    return_value={"table": "orders", "row_count": 0, "status": "ok", "columns": []},
)
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_column_extraction_and_cap(mock_svc, mock_profile):
    """Columns are extracted via name/column_name keys, missing keys dropped,
    and the list capped at 20."""
    from app.services.tool_handlers import db_tools

    cols_info = [{"name": f"c{i}"} for i in range(22)]
    cols_info += [{"column_name": "alt1"}, {"column_name": "alt2"}]
    cols_info += [{"other": "x"}, {}]  # missing keys -> filtered out
    mock_svc.return_value.list_tables.return_value = {"tables": ["orders"]}
    mock_svc.return_value.describe_table.return_value = {"columns": cols_info}

    res = await db_tools._profile_data(
        {"kb_id": "kb1"}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is True
    assert len(res["tables"]) == 1
    args, _ = mock_profile.call_args
    profiled_cols = args[3]
    assert len(profiled_cols) == 20  # 24 valid names capped at 20
    assert profiled_cols[0] == "c0"


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    return_value={"table": "orders", "row_count": 0, "status": "ok", "columns": []},
)
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_columns_string_not_char_split(mock_svc, mock_profile):
    """A JSON string in `columns` must not be char-split into single letters."""
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.return_value = {"tables": ["orders"]}
    mock_svc.return_value.describe_table.return_value = {"columns": []}

    res = await db_tools._profile_data(
        {"kb_id": "kb1", "columns": '["amount"]'}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is True
    args, _ = mock_profile.call_args
    assert args[3] == []


# ── per-table error shape ───────────────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "app.services.dashboard_profiler.profile_kb",
    side_effect=RuntimeError("boom"),
)
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_profile_kb_error_entry(mock_svc, mock_profile):
    """A profile_kb failure yields the canonical per-table error dict."""
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.return_value = {"tables": ["orders"]}
    res = await db_tools._profile_data(
        {"kb_id": "kb1", "columns": ["amount"]}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is True
    entry = res["tables"][0]
    assert set(entry) == {"table", "row_count", "status", "error_message", "columns"}
    assert entry["table"] == "orders"
    assert entry["row_count"] == 0
    assert entry["status"] == "error"
    assert "boom" in entry["error_message"]
    assert entry["columns"] == []


# ── empty listing / list_tables failures ────────────────────────────────


@pytest.mark.asyncio
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_empty_tables_list(mock_svc):
    """An empty table listing yields success with no table entries."""
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.return_value = {"tables": []}
    res = await db_tools._profile_data(
        {"kb_id": "kb1"}, db=object(), user_id="u1", context={}
    )
    assert res == {"success": True, "tables": []}


@pytest.mark.asyncio
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_list_tables_driver_unavailable(mock_svc):
    """A missing DB driver must map to error_kind=driver_missing."""
    from app.services.db.connector_factory import DriverUnavailable
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.side_effect = DriverUnavailable(
        "no driver for this database"
    )
    res = await db_tools._profile_data(
        {"kb_id": "kb1"}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is False
    assert res["error_kind"] == "driver_missing"
    assert "driver" in res["error"].lower()


@pytest.mark.asyncio
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_list_tables_value_error(mock_svc):
    """A missing KB row (ValueError) must be returned as an error dict."""
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.side_effect = ValueError("kb row missing")
    res = await db_tools._profile_data(
        {"kb_id": "kb1"}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is False
    assert "kb row missing" in res["error"]


@pytest.mark.asyncio
@patch("app.services.tool_handlers.db_tools.SchemaService")
async def test_profile_data_list_tables_unknown_error(mock_svc):
    """Any other list_tables failure is caught and returned as an error dict."""
    from app.services.tool_handlers import db_tools

    mock_svc.return_value.list_tables.side_effect = RuntimeError("db exploded")
    res = await db_tools._profile_data(
        {"kb_id": "kb1"}, db=object(), user_id="u1", context={}
    )
    assert res["success"] is False
    assert "db exploded" in res["error"]
