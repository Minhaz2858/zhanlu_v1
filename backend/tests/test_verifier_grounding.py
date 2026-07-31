"""Tests for verify_grounding in synexia.verifier (P2 Task 4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.services.synexia.verifier import verify_grounding


@dataclass
class _FakeObservation:
    id: str = "obs-1"
    tool_name: Optional[str] = None
    success: bool = True
    result_data: Optional[dict] = None
    artifact_ids: list = field(default_factory=list)


@dataclass
class _FakeExecution:
    id: str = "exec-1"
    observations: list = field(default_factory=list)
    assistant_content: str = ""


# --- source_citation --------------------------------------------------------


def test_source_citation_passes_when_assistant_cites_a_tool_name():
    obs = _FakeObservation(tool_name="ask_data_agent", result_data={"rows": [1, 2, 3]})
    exec_ = _FakeExecution(
        observations=[obs],
        assistant_content="Based on the data from ask_data_agent, the answer is 3.",
    )
    profile = {"grounding_checks": ["source_citation"]}
    out = verify_grounding(exec_, profile)
    assert len(out) == 1
    assert out[0]["check"] == "source_citation"
    assert out[0]["ok"] is True


def test_source_citation_fails_when_assistant_makes_unsupported_claim():
    obs1 = _FakeObservation(tool_name="ask_data_agent", result_data={"rows": [10]})
    obs2 = _FakeObservation(tool_name="web_search", result_data={"title": "X"})
    exec_ = _FakeExecution(
        observations=[obs1, obs2],
        assistant_content="There are exactly 9999 entries and the answer is 42.",
    )
    profile = {"grounding_checks": ["source_citation"]}
    out = verify_grounding(exec_, profile)
    assert out[0]["check"] == "source_citation"
    assert out[0]["ok"] is False


def test_source_citation_passes_when_no_observations():
    exec_ = _FakeExecution(observations=[], assistant_content="Some claim.")
    profile = {"grounding_checks": ["source_citation"]}
    out = verify_grounding(exec_, profile)
    assert out[0]["ok"] is True


def test_source_citation_passes_when_observation_has_no_tool_name():
    obs = _FakeObservation(tool_name=None, result_data={"x": 1})
    exec_ = _FakeExecution(observations=[obs], assistant_content="Answer: 1")
    out = verify_grounding(exec_, {"grounding_checks": ["source_citation"]})
    assert out[0]["ok"] is True


# --- hallucination_check ----------------------------------------------------


def test_hallucination_check_passes_when_artifact_refs_resolve():
    obs = _FakeObservation(
        tool_name="create_artifact",
        result_data={"artifact_id": "art-1"},
        artifact_ids=["art-1"],
    )
    exec_ = _FakeExecution(
        observations=[obs],
        assistant_content="I produced the report at art-1 as requested.",
    )
    profile = {"grounding_checks": ["hallucination_check"]}
    out = verify_grounding(exec_, profile)
    assert out[0]["check"] == "hallucination_check"
    assert out[0]["ok"] is True


def test_hallucination_check_fails_when_artifact_id_not_in_observations():
    obs = _FakeObservation(
        tool_name="create_artifact",
        result_data={"artifact_id": "art-1"},
        artifact_ids=["art-1"],
    )
    exec_ = _FakeExecution(
        observations=[obs],
        assistant_content="I produced art-1 and art-2.",
    )
    out = verify_grounding(exec_, {"grounding_checks": ["hallucination_check"]})
    assert out[0]["ok"] is False


def test_hallucination_check_passes_when_no_artifact_refs_in_content():
    exec_ = _FakeExecution(
        observations=[],
        assistant_content="Here is the answer in plain text.",
    )
    out = verify_grounding(exec_, {"grounding_checks": ["hallucination_check"]})
    assert out[0]["ok"] is True


# --- multi-check + unknown -------------------------------------------------


def test_multiple_checks_in_profile_yields_one_entry_per_check():
    profile = {"grounding_checks": ["source_citation", "hallucination_check"]}
    exec_ = _FakeExecution(observations=[], assistant_content="")
    out = verify_grounding(exec_, profile)
    assert len(out) == 2
    names = [c["check"] for c in out]
    assert names == ["source_citation", "hallucination_check"]


def test_unknown_check_names_are_skipped_silently(caplog):
    profile = {"grounding_checks": ["source_citation", "future_check_xyz"]}
    exec_ = _FakeExecution(observations=[], assistant_content="")
    out = verify_grounding(exec_, profile)
    assert len(out) == 1
    assert out[0]["check"] == "source_citation"


def test_empty_profile_returns_empty_list():
    exec_ = _FakeExecution(observations=[], assistant_content="")
    out = verify_grounding(exec_, {})
    assert out == []


def test_no_grounding_checks_in_profile_returns_empty_list():
    exec_ = _FakeExecution(observations=[], assistant_content="")
    out = verify_grounding(exec_, {"grounding_checks": []})
    assert out == []


def test_none_profile_returns_empty_list():
    exec_ = _FakeExecution(observations=[], assistant_content="")
    out = verify_grounding(exec_, None)
    assert out == []


# --- shape contract --------------------------------------------------------


def test_returned_check_dicts_have_required_keys():
    out = verify_grounding(
        _FakeExecution(observations=[], assistant_content=""),
        {"grounding_checks": ["source_citation", "hallucination_check"]},
    )
    for c in out:
        assert set(c.keys()) >= {"check", "ok", "detail"}
        assert isinstance(c["ok"], bool)
        assert isinstance(c["detail"], str)
