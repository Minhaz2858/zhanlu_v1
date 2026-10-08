"""Shared fixtures for the nl2sql test suite.

The backend `.env` (bind-mounted into the test container) may enable the OCI
agentic-planning flags for the SERVING stack. Unit tests here pin legacy
pipeline behaviour, so the env-dependent stages default OFF for every test in
this directory; tests that exercise a stage explicitly enable it via their own
monkeypatch fixture (which runs after this autouse fixture).
"""

from __future__ import annotations

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _deterministic_pipeline_flags(monkeypatch):
    """Env-independent pipeline: no planner, no clarify gate, no promotion,
    no chart hints — unless a test explicitly opts in."""
    monkeypatch.setattr(settings, "NL2SQL_PLANNER_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_CHART_SUGGESTION_ENABLED", False)
    yield
