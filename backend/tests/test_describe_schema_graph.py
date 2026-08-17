"""Tests for the describe_schema graph fast path (db_tools._describe_schema)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tool_handlers.db_tools import _describe_schema


def _policy(restricted=False, denied=False):
    p = MagicMock()
    p.is_kb_fully_denied.return_value = denied
    p.is_kb_restricted.return_value = restricted
    return p


def _settings(graph_enabled):
    return SimpleNamespace(
        SCHEMA_LINKING_ENABLED=True,
        SEMANTIC_CATALOG_ENABLED=True,
        SCHEMA_GRAPH_ENABLED=graph_enabled,
    )


_LINKER_RESULT = {
    "tables": [
        {"table_name": "orders", "table_meta_id": 1},
        {"table_name": "users", "table_meta_id": 2},
    ],
    "slice_text": "-- Semantic catalog\nTable: orders\nTable: users\n",
}


def test_flag_off_returns_catalog_path():
    db = MagicMock()
    with patch(
        "app.services.tool_handlers.db_tools.settings", new=_settings(False)
    ), patch(
        "app.services.tool_handlers.db_tools._require_kb_id",
        return_value=("kb1", None),
    ), patch(
        "app.services.tool_handlers.db_tools._resolve_user_policy",
        return_value=_policy(),
    ), patch(
        "app.services.knowledge_graph.schema_linker.link_schema",
        new=AsyncMock(return_value=_LINKER_RESULT),
    ) as link_schema:
        result = asyncio.run(
            _describe_schema({"kb_id": "kb1", "question": "orders per user"}, db, "u1")
        )
    assert result["source"] == "catalog"
    assert result["success"] is True
    link_schema.assert_awaited_once()


def test_flag_on_returns_schema_graph():
    db = MagicMock()
    graph = MagicMock()
    graph.build.return_value = graph  # SchemaGraph.build returns self
    graph.to_llm_context.return_value = "Table: orders\nJOIN EDGES\norders->users FK"

    with patch(
        "app.services.tool_handlers.db_tools.settings", new=_settings(True)
    ), patch(
        "app.services.tool_handlers.db_tools._require_kb_id",
        return_value=("kb1", None),
    ), patch(
        "app.services.tool_handlers.db_tools._resolve_user_policy",
        return_value=_policy(),
    ), patch(
        "app.services.knowledge_graph.schema_linker.link_schema",
        new=AsyncMock(return_value=_LINKER_RESULT),
    ), patch(
        "app.services.db.schema_graph.SchemaGraph",
        return_value=graph,
    ) as sg_cls:
        result = asyncio.run(
            _describe_schema({"kb_id": "kb1", "question": "orders per user"}, db, "u1")
        )
    assert result["source"] == "schema_graph"
    assert "JOIN EDGES" in result["schema"]
    # build() was called with the candidate table names
    built = sg_cls.return_value
    built.build.assert_called_once()
    assert set(built.build.call_args[0][0]) == {"orders", "users"}


def test_restricted_kb_skips_graph_and_linker():
    db = MagicMock()
    with patch(
        "app.services.tool_handlers.db_tools.settings", new=_settings(True)
    ), patch(
        "app.services.tool_handlers.db_tools._require_kb_id",
        return_value=("kb1", None),
    ), patch(
        "app.services.tool_handlers.db_tools._resolve_user_policy",
        return_value=_policy(restricted=True),
    ), patch(
        "app.services.knowledge_graph.schema_linker.link_schema",
        new=AsyncMock(return_value=_LINKER_RESULT),
    ) as link_schema, patch(
        "app.services.db.schema_graph.SchemaGraph",
    ) as sg_cls:
        # fall back to _do_work (describe_all). Mock SchemaService.describe_table
        # to return a trivial column list so the fallback does not blow up.
        with patch("app.services.tool_handlers.db_tools.SchemaService") as svc:
            svc.return_value.describe_table.return_value = [
                {"name": "id", "type": "int", "pk": True}
            ]
            result = asyncio.run(
                _describe_schema({"kb_id": "kb1"}, db, "u1")
            )
    # linker / graph must never run for a policy-restricted KB
    link_schema.assert_not_awaited()
    sg_cls.assert_not_called()
    assert result.get("source") != "schema_graph"
