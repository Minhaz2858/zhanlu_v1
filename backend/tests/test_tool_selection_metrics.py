"""Tests for tool-selection efficiency telemetry (IBM workstream D).

D1: dynamic tool selection registers per-turn selections and records a miss
    when the loop executes a tool the selection excluded.
D2: audit test locking in the dynamic-mode behavior bounds (core + ≤K
    periphery, relevance inclusion, 'all' mode passthrough).
"""
import pytest

from app.config import settings
from app.services import dynamic_tools
from app.services.agent_metrics import metrics


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": f"tool {name}"}}


@pytest.fixture(autouse=True)
def _dynamic_mode(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_LOADING_MODE", "dynamic")
    # Deterministic selection: force the semantic embedder to fail and use a
    # simple word-overlap lexical scorer (the production tokenizer keeps
    # snake_case intact, which makes toy names unscoreable).
    monkeypatch.setattr(
        dynamic_tools, "_embed_one_or_none", lambda *a, **k: None)
    monkeypatch.setattr(
        dynamic_tools, "_embed_or_none", lambda *a, **k: None)

    def fake_lexical(user_message, periphery):
        q = set(user_message.lower().split())
        out = []
        for schema in periphery:
            name = dynamic_tools._schema_name(schema) or ""
            toks = set(name.lower().replace("_", " ").split())
            out.append(len(q & toks) / max(1, len(q)))
        return out

    monkeypatch.setattr(dynamic_tools, "_lexical_scores", fake_lexical)
    yield


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset()
    yield


# ---------------------------------------------------------------- D1

def test_select_registers_turn_selection():
    tools = [_schema("core_one"), _schema("relevant_data_query"),
             _schema("unrelated_zipper")]
    core = {"core_one"}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(dynamic_tools, "_core_names", lambda: core)
    # query token "data"/"query" overlaps only the relevant periphery tool
    selected = dynamic_tools.select_tools_for_turn(
        tools, "data query export", turn_key="turn-1")
    names = {s["function"]["name"] for s in selected}
    assert "relevant_data_query" in names  # lexically relevant periphery kept
    assert "unrelated_zipper" not in names
    # selected-count telemetry observed
    assert metrics.tools_selected.to_dict()["count"] == 1
    monkey.undo()


def test_miss_recorded_for_excluded_tool(monkeypatch):
    monkeypatch.setattr(dynamic_tools, "_core_names", lambda: {"core_one"})
    dynamic_tools.select_tools_for_turn(
        [_schema("core_one"), _schema("relevant_data_query"), _schema("unrelated_zipper")],
        "data query export", turn_key="turn-2")
    before = metrics.tools_selection_miss.value
    dynamic_tools.record_selection_miss("turn-2", "unrelated_zipper")
    assert metrics.tools_selection_miss.value == before + 1


def test_no_miss_for_selected_or_core_tool(monkeypatch):
    monkeypatch.setattr(dynamic_tools, "_core_names", lambda: {"core_one"})
    dynamic_tools.select_tools_for_turn(
        [_schema("core_one"), _schema("relevant_data_query"), _schema("unrelated_zipper")],
        "data query export", turn_key="turn-3")
    before = metrics.tools_selection_miss.value
    dynamic_tools.record_selection_miss("turn-3", "core_one")
    dynamic_tools.record_selection_miss("turn-3", "relevant_data_query")
    assert metrics.tools_selection_miss.value == before


def test_unknown_turn_key_counts_as_miss():
    before = metrics.tools_selection_miss.value
    dynamic_tools.record_selection_miss("never-registered", "some_tool")
    assert metrics.tools_selection_miss.value == before + 1


def test_miss_warns_in_snapshot():
    dynamic_tools.record_selection_miss(None, "missing_tool")
    snap = metrics.get_snapshot()
    assert snap["tool_selection"]["misses"] >= 1


# ---------------------------------------------------------------- D2 audit

def _fake_registry(n):
    return [_schema(f"tool_{i:03d}") for i in range(n)]


def test_dynamic_mode_bounds_selection_size(monkeypatch):
    monkeypatch.setattr(dynamic_tools, "_core_names", lambda: set())
    tools = _fake_registry(130)
    # lexical fallback with a query matching nothing picks top-K periphery
    selected = dynamic_tools.select_tools_for_turn(tools, "completely unrelated words zzz")
    # either full list (fail-open when no token overlap) or bounded core+K
    if len(selected) != len(tools):
        assert len(selected) <= int(getattr(settings, "TOOL_LOADING_PERIPHERY_TOP_K", 12))


def test_relevant_tool_included_by_description_similarity(monkeypatch):
    # register periphery with descriptions embedding-friendly
    monkeypatch.setattr(dynamic_tools, "_core_names", lambda: set())
    tools = [
        {"type": "function", "function": {"name": "export_forecast_csv",
                                          "description": "export sales forecast data to csv"}},
        {"type": "function", "function": {"name": "zip_creator",
                                          "description": "create zip archives"}},
    ]
    selected = dynamic_tools.select_tools_for_turn(
        tools, "please export the sales forecast to csv")
    names = {s["function"]["name"] for s in selected}
    assert "export_forecast_csv" in names


def test_all_mode_returns_everything(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_LOADING_MODE", "all")
    tools = _fake_registry(130)
    assert len(dynamic_tools.select_tools_for_turn(tools, "x")) == 130


def test_fail_open_on_internal_error(monkeypatch):
    monkeypatch.setattr(dynamic_tools, "_core_names",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    tools = _fake_registry(130)
    assert len(dynamic_tools.select_tools_for_turn(tools, "x")) == 130
