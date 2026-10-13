"""Turn router tests (Option C Phase 1, 2026-10-13).

The router makes ONE LLM routing decision per turn, replacing the
keyword-governor pile. Modes: off (heuristic only) / dark (log-only
comparison) / on (confident LLM overrides).
"""
import pytest

from app.services import turn_router


def _llm_result(route, confidence):
    return {"content": f'{{"route": "{route}", "confidence": {confidence}, "reason": "test"}}'}


def _decision(route, confidence):
    """A parsed router decision (what apply_llm_route consumes)."""
    return {"route": route, "confidence": confidence, "reason": "test", "source": "llm"}


# ── mode resolution ───────────────────────────────────────────────────────


def test_router_mode_off_by_default(monkeypatch):
    monkeypatch.setattr(turn_router.settings, "V3_LLM_ROUTER_MODE", "")
    assert turn_router.router_mode() == "off"


def test_router_mode_invalid_falls_back_to_off(monkeypatch):
    monkeypatch.setattr(turn_router.settings, "V3_LLM_ROUTER_MODE", "banana")
    assert turn_router.router_mode() == "off"


# ── LLM decision parsing ──────────────────────────────────────────────────


async def test_decide_route_parses_llm_json(monkeypatch):
    async def fake_call(**kwargs):
        return _llm_result("data_pipeline", 0.9)

    monkeypatch.setattr(turn_router, "_fake_marker", None, raising=False)
    import app.services.llm_service as ls

    monkeypatch.setattr(ls, "call_llm", fake_call)
    d = await turn_router.decide_turn_route("top 10 products by revenue", has_data_context=True)
    assert d["route"] == "data_pipeline"
    assert d["confidence"] == 0.9
    assert d["source"] == "llm"


async def test_decide_route_none_on_garbage(monkeypatch):
    async def fake_call(**kwargs):
        return {"content": "I think the answer is 42"}

    import app.services.llm_service as ls

    monkeypatch.setattr(ls, "call_llm", fake_call)
    assert await turn_router.decide_turn_route("hello", has_data_context=False) is None


async def test_decide_route_none_on_invalid_route(monkeypatch):
    async def fake_call(**kwargs):
        return _llm_result("maybe", 0.9)

    import app.services.llm_service as ls

    monkeypatch.setattr(ls, "call_llm", fake_call)
    assert await turn_router.decide_turn_route("x", has_data_context=False) is None


async def test_decide_route_fails_open_on_exception(monkeypatch):
    async def fake_call(**kwargs):
        raise RuntimeError("provider down")

    import app.services.llm_service as ls

    monkeypatch.setattr(ls, "call_llm", fake_call)
    assert await turn_router.decide_turn_route("x", has_data_context=False) is None


# ── mode semantics ────────────────────────────────────────────────────────


def test_off_mode_never_overrides(monkeypatch):
    monkeypatch.setattr(turn_router.settings, "V3_LLM_ROUTER_MODE", "off")
    wants, source = turn_router.apply_llm_route(
        True, _decision("direct", 0.99), heuristic_source="data-bound"
    )
    assert wants is True and source == "data-bound"


def test_dark_mode_heuristic_wins_but_disagreement_logged(monkeypatch, caplog):
    monkeypatch.setattr(turn_router.settings, "V3_LLM_ROUTER_MODE", "dark")
    with caplog.at_level("INFO", logger="app.services.turn_router"):
        wants, source = turn_router.apply_llm_route(
            True, _decision("direct", 0.95), heuristic_source="data-bound"
        )
    assert wants is True and source == "data-bound"
    assert any("DISAGREEMENT" in r.message for r in caplog.records)


def test_on_mode_confident_llm_overrides(monkeypatch):
    monkeypatch.setattr(turn_router.settings, "V3_LLM_ROUTER_MODE", "on")
    wants, source = turn_router.apply_llm_route(
        True, _decision("direct", 0.9), heuristic_source="data-bound"
    )
    assert wants is False and source == "llm-router"


def test_on_mode_low_confidence_heuristic_stands(monkeypatch):
    monkeypatch.setattr(turn_router.settings, "V3_LLM_ROUTER_MODE", "on")
    wants, source = turn_router.apply_llm_route(
        True, _decision("direct", 0.3), heuristic_source="data-bound"
    )
    assert wants is True and source == "data-bound"


def test_automation_forced_never_overridden(monkeypatch):
    monkeypatch.setattr(turn_router.settings, "V3_LLM_ROUTER_MODE", "on")
    wants, source = turn_router.apply_llm_route(
        True, _decision("direct", 0.99), forced=True
    )
    assert wants is True  # scheduled runs always plan
