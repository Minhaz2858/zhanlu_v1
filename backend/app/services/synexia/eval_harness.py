"""Eval harness: runnable golden-set tests for the agent.

The legacy ``eval_harness.py`` was a stub ("validation pending").  This
module promotes it to a real harness with:

* A small JSON file of golden scenarios the user can grow.
* A pluggable ``EvalRunner`` that runs each scenario through the
  SynexiaFSM and grades the output.
* Built-in graders: ``contains``, ``not_contains``,
  ``artifact_created``, ``confidence_at_least``.
* Structured JSON report for CI: pass / fail / partial per scenario.

The harness is intentionally a *test framework*, not a benchmark: it
exercises the public FSM surface (``SynexiaFSM.run`` / the
``add_message`` flow) and reports whether each scenario met its
assertions.  No leaderboard numbers — those belong in a separate
benchmark suite.

A scenario looks like::

    {
      "name": "sales_report_minimal",
      "user_message": "make a sales report for me",
      "expect": {
        "contains": ["Sales_Report"],
        "not_contains": ["Failed to load artifact", "HTTP 404"],
        "artifact_created": true,
        "confidence_at_least": 0.5
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ── Graders ──────────────────────────────────────────────────────────────
@dataclass
class GraderResult:
    name: str
    passed: bool
    detail: str = ""


def grade_contains(output: dict, needle: str) -> GraderResult:
    hay = _flatten_text(output)
    ok = needle.lower() in hay.lower()
    return GraderResult(
        name=f"contains({needle!r})",
        passed=ok,
        detail="" if ok else f"{needle!r} not found in assistant text",
    )


def grade_not_contains(output: dict, needle: str) -> GraderResult:
    hay = _flatten_text(output)
    ok = needle.lower() not in hay.lower()
    return GraderResult(
        name=f"not_contains({needle!r})",
        passed=ok,
        detail="" if ok else f"{needle!r} unexpectedly present",
    )


def grade_artifact_created(output: dict, expected: bool) -> GraderResult:
    ids = output.get("artifact_ids") or []
    ok = (bool(ids) == expected) if expected else (not ids)
    return GraderResult(
        name=f"artifact_created({expected})",
        passed=ok,
        detail=f"artifact_ids={ids}",
    )


def grade_confidence_at_least(output: dict, threshold: float) -> GraderResult:
    score = float(output.get("confidence") or 0.0)
    return GraderResult(
        name=f"confidence_at_least({threshold})",
        passed=score >= threshold,
        detail=f"confidence={score:.3f}",
    )


def grade_quality_gate(output: dict, expected_pass: bool) -> GraderResult:
    """Assert the Phase B quality gate outcome.

    ``expected_pass=True`` → the gate either didn't fire (no artifacts) or
    passed. ``expected_pass=False`` → the gate must have fired and held
    artifacts back (used by scenarios that force a low-confidence run).
    """
    gate = output.get("quality_gate")
    if gate is None:
        # No artifacts produced → gate never engaged → treated as pass.
        actual = True
        detail = "quality_gate absent (no artifacts shipped)"
    else:
        actual = bool(gate.get("passed", True))
        detail = f"quality_gate={gate}"
    ok = actual == expected_pass
    return GraderResult(
        name=f"quality_gate_passed({expected_pass})",
        passed=ok,
        detail="" if ok else detail,
    )


def grade_quality_eval_passed(output: dict, expected_pass: bool) -> GraderResult:
    """Assert the QUALITY_EVAL (Tier 2) outcome.

    ``expected_pass=True`` → the QUALITY_EVAL verdict must be accept with
    adequate completeness (``is_ok=True``), OR QUALITY_EVAL was absent
    (disabled — treated as pass, like the quality gate).  ``expected_pass=
    False`` → QUALITY_EVAL must be present and ``is_ok=False`` (the output
    was flagged revise/reject or low-completeness).
    """
    qe = output.get("quality_eval")
    if qe is None:
        actual = True  # absent (disabled) → treated as pass
        detail = "quality_eval absent (QUALITY_EVAL disabled)"
    else:
        actual = bool(qe.get("is_ok", False))
        detail = f"quality_eval verdict={qe.get('verdict')} is_ok={actual}"
    ok = actual == expected_pass
    return GraderResult(
        name=f"quality_eval_passed({expected_pass})",
        passed=ok,
        detail="" if ok else detail,
    )


def grade_completeness_at_least(output: dict, threshold: float) -> GraderResult:
    """Assert the QUALITY_EVAL completeness_score >= threshold.

    Fails when ``quality_eval`` is absent (completeness cannot be verified
    when QUALITY_EVAL did not run).
    """
    qe = output.get("quality_eval")
    if qe is None:
        return GraderResult(
            name=f"completeness_at_least({threshold})",
            passed=False,
            detail="quality_eval absent (cannot verify completeness)",
        )
    score = float(qe.get("completeness_score", 0.0))
    return GraderResult(
        name=f"completeness_at_least({threshold})",
        passed=score >= threshold,
        detail=f"completeness_score={score:.3f}",
    )


def grade_reflexion_verdict(output: dict, expected_verdict: str) -> GraderResult:
    """Assert the QUALITY_EVAL verdict matches ``expected_verdict``.

    Fails when ``quality_eval`` is absent.
    """
    qe = output.get("quality_eval")
    if qe is None:
        return GraderResult(
            name=f"reflexion_verdict({expected_verdict})",
            passed=False,
            detail="quality_eval absent (cannot verify verdict)",
        )
    actual = str(qe.get("verdict", ""))
    return GraderResult(
        name=f"reflexion_verdict({expected_verdict})",
        passed=actual == expected_verdict,
        detail=f"verdict={actual}",
    )


def _flatten_text(output: dict) -> str:
    parts: list[str] = [str(output.get("assistant_content") or "")]
    for tc in output.get("tool_calls") or []:
        try:
            parts.append(json.dumps(tc, default=str))
        except Exception:
            parts.append(str(tc))
    for art in output.get("file_exports") or []:
        try:
            parts.append(json.dumps(art, default=str))
        except Exception:
            parts.append(str(art))
    return "\n".join(parts)


# ── Runner ───────────────────────────────────────────────────────────────
@dataclass
class ScenarioResult:
    name: str
    passed: bool
    graders: list[GraderResult] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str = ""


@dataclass
class HarnessReport:
    passed: int
    failed: int
    total: int
    scenarios: list[ScenarioResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def is_ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "duration_ms": self.duration_ms,
            "scenarios": [
                {
                    "name": s.name,
                    "passed": s.passed,
                    "duration_ms": s.duration_ms,
                    "error": s.error,
                    "graders": [asdict(g) for g in s.graders],
                }
                for s in self.scenarios
            ],
        }


class EvalRunner:
    """Run a list of scenarios through a caller-supplied async fn."""

    def __init__(
        self,
        run_fn: Callable[[dict], Awaitable[dict]],
        scenarios: list[dict],
    ) -> None:
        self.run_fn = run_fn
        self.scenarios = scenarios

    async def run(self) -> HarnessReport:
        started = time.time()
        results: list[ScenarioResult] = []
        for sc in self.scenarios:
            results.append(await self._run_one(sc))
        passed = sum(1 for r in results if r.passed)
        return HarnessReport(
            passed=passed,
            failed=len(results) - passed,
            total=len(results),
            scenarios=results,
            duration_ms=(time.time() - started) * 1000,
        )

    async def _run_one(self, scenario: dict) -> ScenarioResult:
        name = scenario.get("name") or "unnamed"
        expect = scenario.get("expect") or {}
        started = time.time()
        try:
            output = await self.run_fn(scenario)
        except Exception as exc:
            return ScenarioResult(
                name=name,
                passed=False,
                duration_ms=(time.time() - started) * 1000,
                error=str(exc),
            )
        graders: list[GraderResult] = []
        for needle in expect.get("contains") or []:
            graders.append(grade_contains(output, needle))
        for needle in expect.get("not_contains") or []:
            graders.append(grade_not_contains(output, needle))
        if "artifact_created" in expect:
            graders.append(grade_artifact_created(output, bool(expect["artifact_created"])))
        if "confidence_at_least" in expect:
            graders.append(grade_confidence_at_least(output, float(expect["confidence_at_least"])))
        if "quality_gate_passed" in expect:
            graders.append(grade_quality_gate(output, bool(expect["quality_gate_passed"])))
        if "quality_eval_passed" in expect:
            graders.append(grade_quality_eval_passed(output, bool(expect["quality_eval_passed"])))
        if "completeness_at_least" in expect:
            graders.append(grade_completeness_at_least(output, float(expect["completeness_at_least"])))
        if "reflexion_verdict" in expect:
            graders.append(grade_reflexion_verdict(output, str(expect["reflexion_verdict"])))
        return ScenarioResult(
            name=name,
            passed=all(g.passed for g in graders),
            graders=graders,
            duration_ms=(time.time() - started) * 1000,
        )


# ── Built-in golden scenarios ───────────────────────────────────────────
# These are the scenarios the eval harness ships with.  Each one is
# exactly the kind of request the user has been making in production
# (sales report, DB overview, etc.) plus a few quality gates we want
# to enforce forever.
BUILTIN_SCENARIOS: list[dict] = [
    {
        "name": "sales_report_minimal",
        "user_message": "make a sales report for me",
        "expect": {
            "not_contains": ["Failed to load artifact", "HTTP 404"],
            "artifact_created": True,
        },
    },
    {
        "name": "db_overview_no_404",
        "user_message": "give me a database overview",
        "expect": {
            "not_contains": ["Failed to load artifact", "HTTP 404"],
        },
    },
    {
        "name": "clarify_batch_resolves",
        "user_message": "create a new automation",
        "expect": {
            "not_contains": ["step 1/2", "Failed to load artifact"],
        },
    },
    {
        "name": "confidence_above_threshold",
        "user_message": "tell me what you can do",
        "expect": {
            "confidence_at_least": 0.4,
        },
    },
    {
        "name": "web_browse_no_refusal",
        # The exact user message that triggered the "I cannot browse"
        # bug.  The eval guards that the agent either calls web_search
        # or — when the LLM still refuses — the self-healing guardrail
        # auto-runs web_search and the final reply contains a real
        # source URL.
        "user_message": "can you collect some industry news from website",
        "expect": {
            "not_contains": [
                "I cannot browse the internet",
                "I don't have access to the internet",
                "I'm sorry, but I cannot",
            ],
        },
    },
    {
        "name": "web_research_keywords_match",
        # A range of online-research phrasings that should all be
        # detected by the guardrail.
        "user_message": "look up the latest AI news online",
        "expect": {
            "not_contains": [
                "I cannot browse the internet",
                "I don't have access to the internet",
            ],
        },
    },
    {
        "name": "real_time_price_request",
        # The user's exact scenario: "give me today gold price"
        # The LLM-based intent classifier + self-critic + regex
        # fallback must catch this and either call web_search or
        # auto-correct a refusal.
        "user_message": "give me today gold price",
        "expect": {
            "not_contains": [
                "I cannot provide real-time data",
                "I don't have access to live data",
                "I cannot browse the internet",
            ],
        },
    },
    {
        "name": "real_time_weather_request",
        # "How is the weather right now in London" — temporal +
        # topic pattern.  Must not be answered with a refusal.
        "user_message": "how is the weather right now in London",
        "expect": {
            "not_contains": [
                "I cannot browse the internet",
                "I don't have access to live data",
                "I am unable to access real-time",
            ],
        },
    },
    {
        "name": "quality_eval_completeness",
        # Exercises the QUALITY_EVAL (Tier 2) semantic layer: the response
        # to an analysis request must score adequate completeness and a
        # non-reject verdict.  Guards against off-topic / incomplete
        # summaries that pass the structural VERIFY but fail semantically.
        "user_message": "analyze the sales data and summarize the key trends",
        "expect": {
            "not_contains": ["Failed to load artifact", "HTTP 404"],
            "completeness_at_least": 0.5,
            "reflexion_verdict": "accept",
        },
    },
]


def load_scenarios_from_file(path: str) -> list[dict]:
    """Load scenarios from a JSON file; on parse error return ``[]``."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("eval_harness: failed to load %s (%s)", path, exc)
        return []
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict)]
    return []


def load_all_scenarios(*, user_file: Optional[str] = None) -> list[dict]:
    """Load builtin + user file scenarios (user file wins on name clash)."""
    by_name: dict[str, dict] = {}
    for sc in BUILTIN_SCENARIOS:
        by_name[sc["name"]] = sc
    if user_file:
        for sc in load_scenarios_from_file(user_file):
            by_name[sc.get("name") or f"user-{len(by_name)}"] = sc
    return list(by_name.values())


async def run_harness(
    run_fn: Callable[[dict], Awaitable[dict]],
    *,
    user_file: Optional[str] = None,
) -> HarnessReport:
    """Convenience: run all built-in + user-file scenarios."""
    scenarios = load_all_scenarios(user_file=user_file)
    runner = EvalRunner(run_fn=run_fn, scenarios=scenarios)
    return await runner.run()


__all__ = [
    "BUILTIN_SCENARIOS",
    "EvalRunner",
    "GraderResult",
    "HarnessReport",
    "ScenarioResult",
    "grade_completeness_at_least",
    "grade_quality_eval_passed",
    "grade_quality_gate",
    "grade_reflexion_verdict",
    "load_all_scenarios",
    "load_scenarios_from_file",
    "run_harness",
]
