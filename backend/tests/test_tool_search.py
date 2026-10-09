"""P0-2 semantic tool-selection tests."""
import pytest

from app.config import settings
from app.services.tool_search import (
    ALWAYS_KEEP,
    SEARCH_TOOLS_SCHEMA,
    handle_search_tools,
    rank_tools,
    select_relevant_tools,
)


def _schema(name: str, desc: str, params: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {p: {"type": "string"} for p in (params or [])},
            },
        },
    }


def _catalog(n: int = 50) -> list[dict]:
    out = []
    for i in range(n):
        out.append(_schema(f"tool_{i}", f"does thing {i}"))
    out.append(_schema("web_search", "search the web for real-time info"))
    out.append(_schema("database_query", "run SQL against the warehouse"))
    return out


def test_disabled_flag_returns_full_list(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SEARCH_ENABLED", False)
    catalog = _catalog()
    assert select_relevant_tools("query about databases", catalog) is catalog


def test_small_catalog_untouched(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_SEARCH_TOP_K", 32)
    small = _catalog(5)
    assert select_relevant_tools("big catalog here", small) is small


def test_large_catalog_filtered_with_essentials_and_meta(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_SEARCH_TOP_K", 5)
    catalog = _catalog(50)
    out = select_relevant_tools(
        "I need to run SQL against the database warehouse", catalog)
    names = [s["function"]["name"] for s in out]
    assert len(out) < len(catalog)
    # essentials survive regardless of rank
    assert "web_search" in names
    # the discovery escape hatch is appended
    assert "search_tools" in names
    # lexical fallback should rank the SQL tool highly without embeddings
    assert "database_query" in names


def test_essential_tools_never_dropped(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_SEARCH_ENABLED", True)
    monkeypatch.setattr(settings, "TOOL_SEARCH_TOP_K", 2)
    catalog = _catalog(40)  # irrelevant filler
    out = select_relevant_tools("totally unrelated message", catalog)
    names = [s["function"]["name"] for s in out]
    for essential in ALWAYS_KEEP:
        if essential == "search_tools":
            continue
        if any(s["function"]["name"] == essential for s in catalog):
            assert essential in names


def test_rank_tools_lexical_fallback_no_key(monkeypatch):
    # no embedding backend in tests → lexical path
    schemas = [
        _schema("a", "generate images from text"),
        _schema("b", "send email notifications"),
    ]
    scores = rank_tools("generate an image of a cat", schemas)
    assert scores[0] > scores[1]


def test_search_tools_handler_returns_matches():
    # registers real catalog tools? registry has the full app catalog in
    # test env (tool_handlers imported); just check shape
    out = handle_search_tools({"query": "search the web", "limit": 3})
    assert out["success"] is True
    assert isinstance(out["matches"], list)
    assert len(out["matches"]) <= 3
    for m in out["matches"]:
        assert "name" in m and "description" in m


def test_search_tools_handler_requires_query():
    out = handle_search_tools({})
    assert out["success"] is False


def test_meta_schema_shape():
    fn = SEARCH_TOOLS_SCHEMA["function"]
    assert fn["name"] == "search_tools"
    assert "query" in fn["parameters"]["required"]
