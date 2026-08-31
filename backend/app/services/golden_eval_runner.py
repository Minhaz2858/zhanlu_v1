"""Golden-eval regression runner (2026-08-29).

Runs the seeded golden test cases (``agent_test_cases``) as REAL agent turns
against a CANDIDATE LLM endpoint, judges each response with the CHAMPION
model (a model must never grade itself — keeps the scale constant), persists
``EvalResult`` rows under ``conversation_id="golden:<case.name>"``, and
compares the candidate's pass rate against the champion's history.

Gate semantics:
  - ``pass``     — candidate meets the absolute floor and champion parity.
  - ``warn``     — within tolerance but below champion, or minor score dip.
  - ``fail``     — below the absolute floor, or a champion-passed case
                   regressed (hard fail), or the run timed out (fail-closed).

The candidate turn runs through ``_run_sub_agent_inner`` (the same real
agentic tool-calling loop delegate_task uses) with the candidate endpoint
forced — no ``resolve_effective_llm`` involvement, so the gate tests the
model itself, not the routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

_GOLDEN_CONV_PREFIX = "golden:"


def _parse_case_payload(case) -> dict:
    """Normalise a case's input/expected-output JSON (robust to str|dict)."""
    def _loads(v):
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    return {
        "input": _loads(getattr(case, "input_json", None)),
        "expected": _loads(getattr(case, "expected_output_json", None)),
    }


def _judge_llm_call(endpoint):
    """Build the sync ``llm_call`` adapter for quality_eval on an endpoint."""
    from app.services.llm_service import chat_completion_json_sync

    def _call(*, prompt, messages, temperature):
        return chat_completion_json_sync(
            prompt=prompt, temperature=temperature, endpoint=endpoint,
        )

    return _call


def _resolve_judge_endpoint(db: Session, champion_label: str | None):
    """Resolve the CHAMPION endpoint used to grade the candidate.

    A model must never grade itself — the judge is always the current
    champion. Resolution: the enabled LlmModel row whose ``model_id`` matches
    the champion label; falls back to the legacy env provider when the
    champion is ``settings.LLM_MODEL`` (e.g. deepseek-chat); otherwise None
    (judge falls back to the heuristic verdict).
    """
    if not champion_label:
        return None
    try:
        from app.models.llm_model import LlmModel
        from app.services.crypto_utils import decrypt_value
        from app.services.llm_router import LLMEndpoint

        row = (
            db.query(LlmModel)
            .filter(
                LlmModel.model_id == champion_label,
                LlmModel.enabled == True,  # noqa: E712
                LlmModel.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if row is not None:
            return LLMEndpoint(
                base_url=row.base_url,
                api_key=decrypt_value(row.api_key) if row.api_key else "",
                model_id=row.model_id,
                is_private=bool(row.is_private),
                bypass_hallucination_guardrail=bool(row.bypass_hallucination_guardrail),
                provider=row.provider or "",
                context_window=row.context_window,
                max_output_tokens=row.max_output_tokens,
                supports_structured_tool_calls=bool(row.supports_structured_tool_calls),
            )
    except Exception as exc:  # noqa: BLE001 — fall through to legacy/env
        logger.warning("golden_eval: champion row resolution failed (non-fatal): %s", exc)

    if champion_label == getattr(settings, "LLM_MODEL", None):
        base_url = getattr(settings, "OPENAI_BASE_URL", None) or getattr(settings, "LLM_BASE_URL", None)
        api_key = getattr(settings, "OPENAI_API_KEY", None) or getattr(settings, "LLM_API_KEY", None)
        if base_url and api_key:
            try:
                from app.services.llm_router import LLMEndpoint

                return LLMEndpoint(base_url=base_url, api_key=api_key, model_id=champion_label)
            except Exception:  # noqa: BLE001
                return None
    return None


def _artifact_text_evidence(response: str) -> bool:
    """Weak text heuristic for artifact-ish assertions (v1 — tool-level
    evidence is not surfaced by the sub-agent loop, so these assertions are
    advisory only and never hard-fail a case)."""
    if not response:
        return False
    low = response.lower()
    return any(k in low for k in ("created", "generated", "saved", "downloaded", "ready", "built"))


def _evaluate_assertions(case, result: dict, judge) -> dict:
    """Evaluate the case's declared assertions against the run.

    v1 scope: run-level + judge-level signals only. Tool-level assertions
    (schema_inspected, real_sql, artifact_created, dashboard_quality_not_c,
    filters_declared, sections_gt_1) cannot be observed from the sub-agent
    loop's final text, so they are reported as ``advisory`` — they never fail
    a case in v1, but are surfaced so a future loop-level tracer can upgrade
    them.
    """
    response = result.get("response") or ""
    outcome: dict[str, str] = {}
    for assertion in (case.assertions or []) if hasattr(case, "assertions") else []:
        if assertion == "no_failed_required_tools":
            outcome[assertion] = "pass" if result.get("success") else "fail"
        elif assertion == "all_parts_answered":
            outcome[assertion] = "pass" if judge.verdict == "accept" else "fail"
        elif assertion == "grounding_must_pass":
            outcome[assertion] = "pass" if judge.verdict == "accept" else "fail"
        else:
            # Tool-level / artifact-level assertions — advisory in v1.
            outcome[assertion] = "pass" if _artifact_text_evidence(response) else "advisory"
    return outcome


async def _run_one_case(db, case, agent_app_id: str, user_id: str, endpoint, judge_endpoint) -> dict:
    """Run one golden case as a real agent turn against ``endpoint``.

    ``endpoint`` is the CANDIDATE (the model under test); ``judge_endpoint``
    is the CHAMPION model used to grade the response (None → heuristic).
    """
    from app.services.tool_handlers.delegate_tool import _run_sub_agent_inner

    payload = _parse_case_payload(case)
    user_message = payload["input"].get("user_message") or case.name
    expected = payload["expected"] or {}
    expected_accuracy = float(expected.get("expected_accuracy", 0.6))

    started = datetime.now(timezone.utc)
    try:
        result = await asyncio.wait_for(
            _run_sub_agent_inner(
                task=user_message,
                agent_name=getattr(case, "agent_name", None) or "general_assistant",
                db=db,
                user_id=user_id,
                max_iterations=int(getattr(settings, "GOLDEN_EVAL_MAX_ITERATIONS", 6)),
                endpoint=endpoint,
            ),
            timeout=float(getattr(settings, "EVAL_GATE_CASE_TIMEOUT_S", 60)),
        )
        timed_out = False
    except asyncio.TimeoutError:
        result = {"success": False, "error": "golden case timed out (fail-closed)", "task": user_message}
        timed_out = True
    except Exception as exc:  # noqa: BLE001 — fail-closed on any runner error
        result = {"success": False, "error": f"golden case runner error: {exc}", "task": user_message}
        timed_out = True

    assistant_text = result.get("response") or result.get("error") or ""
    duration_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))

    # Judge with the CHAMPION model (never the candidate).
    from app.services.synexia.quality_eval import evaluate_quality

    judge = evaluate_quality(
        user_message=user_message,
        assistant_text=assistant_text,
        task_spec={
            "acceptance_criteria": [getattr(case, "expected_behavior", None) or case.description or ""],
        },
        llm_call=_judge_llm_call(judge_endpoint) if judge_endpoint is not None else None,
    )

    assertion_results = _evaluate_assertions(case, result, judge)
    hard_fail = any(v == "fail" for v in assertion_results.values())

    completeness = judge.completeness_score if judge.verdict == "accept" else min(judge.completeness_score, 0.5)
    case_pass = (
        result.get("success") is True
        and judge.verdict == "accept"
        and completeness >= expected_accuracy
        and not hard_fail
    )

    return {
        "case_name": case.name,
        "test_type": getattr(case, "test_type", "unit") or "unit",
        "success": bool(result.get("success")),
        "timed_out": timed_out,
        "error": result.get("error"),
        "assistant_excerpt": assistant_text[:400],
        "judge_verdict": judge.verdict,
        "completeness": round(completeness, 3),
        "confidence": round(judge.confidence, 3) if judge.confidence is not None else None,
        "expected_accuracy": expected_accuracy,
        "assertions": assertion_results,
        "pass": case_pass,
        "duration_ms": duration_ms,
        "iterations": result.get("iterations"),
    }


def _persist_case_result(db, case, run: dict, model_label: str) -> None:
    """Persist EvalResult + bump AgentTestCase run counters (best-effort)."""
    try:
        from app.models.eval_result import EvalResult

        db.add(EvalResult(
            conversation_id=f"{_GOLDEN_CONV_PREFIX}{run['case_name']}",
            user_message=getattr(case, "description", None) or run["case_name"],
            assistant_text=run["assistant_excerpt"],
            scores=json.dumps({"completeness": run["completeness"], "confidence": run["confidence"]}),
            verdict="accept" if run["pass"] else "fail",
            model=model_label,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("golden_eval: EvalResult persist failed (non-fatal): %s", exc)

    try:
        case.run_count = (case.run_count or 0) + 1
        if run["pass"]:
            case.pass_count = (case.pass_count or 0) + 1
        case.last_run_at = datetime.now(timezone.utc)
        case.last_result = "pass" if run["pass"] else "fail"
        case.last_output_json = {
            "judge_verdict": run["judge_verdict"],
            "completeness": run["completeness"],
            "success": run["success"],
            "error": run.get("error"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("golden_eval: case counter bump failed (non-fatal): %s", exc)


def _champion_stats(db, champion_label: str) -> dict | None:
    """Aggregate the champion model's golden-run history."""
    from app.models.eval_result import EvalResult

    rows = (
        db.query(EvalResult)
        .filter(
            EvalResult.conversation_id.like(f"{_GOLDEN_CONV_PREFIX}%"),
            EvalResult.model == champion_label,
        )
        .all()
    )
    if not rows:
        return None
    pass_count = sum(1 for r in rows if r.verdict == "accept")
    completions = []
    for r in rows:
        try:
            scores = json.loads(r.scores or "{}")
            c = float(scores.get("completeness", 0.0))
        except (json.JSONDecodeError, TypeError, ValueError):
            c = 0.0
        completions.append(c)
    return {
        "model": champion_label,
        "n": len(rows),
        "pass_rate": round(pass_count / max(len(rows), 1), 3),
        "mean_completeness": round(sum(completions) / max(len(completions), 1), 3),
    }


def _gate_verdict(candidate: dict, champion: dict | None) -> str:
    """pass | warn | fail — parity math against the champion."""
    floor = float(getattr(settings, "EVAL_GATE_FLOOR", 0.8))
    tolerance = float(getattr(settings, "EVAL_GATE_PARITY_TOLERANCE", 0.05))
    n = candidate["n"]
    if n == 0:
        return "fail"  # empty run: fail-closed (no evidence)

    cand_rate = candidate["pass_rate"]
    if cand_rate < floor:
        return "fail"

    if champion is None:
        return "pass"  # no baseline yet — absolute floor satisfied

    delta = champion["pass_rate"] - cand_rate
    comp_delta = champion["mean_completeness"] - candidate["mean_completeness"]
    if delta > tolerance or comp_delta > tolerance:
        return "warn"

    # Hard-fail: any case the champion passed that the candidate failed.
    if candidate.get("regressed_cases"):
        return "fail"
    return "pass"


async def run_golden_suite(
    db: Session,
    *,
    endpoint,
    model_label: str,
    champion_label: str | None = None,
    agent_app_id: str | None = None,
    user_id: str = "golden-runner",
    seed_if_empty: bool = True,
) -> dict:
    """Run the golden suite against a candidate endpoint. Returns a report.

    Args:
        endpoint: candidate LLMEndpoint (forced — routing not consulted).
        model_label: label stored on EvalResult rows for the candidate.
        champion_label: model id of the current champion (judge + baseline);
            when None, the suite still runs (floor-only gating).
    """
    from app.models.agent_test_case import AgentTestCase

    if seed_if_empty:
        try:
            from scripts.seed_golden_test_cases import seed

            seed(db, app_name="general_assistant")
        except Exception as exc:  # noqa: BLE001 — seeding is best-effort
            logger.warning("golden_eval: case seeding failed (non-fatal): %s", exc)

    q = db.query(AgentTestCase).filter(AgentTestCase.status != "disabled")
    if agent_app_id:
        q = q.filter(AgentTestCase.agent_app_id == agent_app_id)
    cases = q.order_by(AgentTestCase.name.asc()).all()
    if not cases:
        return {
            "status": "fail", "reason": "no_golden_cases",
            "candidate": {"model": model_label, "n": 0, "pass_rate": 0.0, "mean_completeness": 0.0},
            "champion": _champion_stats(db, champion_label) if champion_label else None,
            "cases": [], "regressed_cases": [], "error": "agent_test_cases table is empty — run scripts.seed_golden_test_cases",
        }

    results = []
    judge_endpoint = _resolve_judge_endpoint(db, champion_label)
    for case in cases:
        run = await _run_one_case(
            db, case, agent_app_id or case.agent_app_id, user_id, endpoint, judge_endpoint,
        )
        _persist_case_result(db, case, run, model_label)
        results.append(run)
    db.commit()

    n = len(results)
    passed = [r for r in results if r["pass"]]
    candidate = {
        "model": model_label,
        "n": n,
        "pass_rate": round(len(passed) / max(n, 1), 3),
        "mean_completeness": round(sum(r["completeness"] for r in results) / max(n, 1), 3),
    }

    champion = _champion_stats(db, champion_label) if champion_label else None
    regressed_cases = []
    if champion is not None:
        # Determine champion-passed case names from its history.
        from app.models.eval_result import EvalResult

        champ_rows = (
            db.query(EvalResult)
            .filter(
                EvalResult.conversation_id.like(f"{_GOLDEN_CONV_PREFIX}%"),
                EvalResult.model == champion_label,
                EvalResult.verdict == "accept",
            )
            .all()
        )
        champ_passed_names = {
            r.conversation_id[len(_GOLDEN_CONV_PREFIX):] for r in champ_rows
        }
        regressed_cases = [
            {"case_name": r["case_name"], "champion": "pass", "candidate": r["judge_verdict"]}
            for r in results
            if not r["pass"] and r["case_name"] in champ_passed_names
        ]
    candidate["regressed_cases"] = regressed_cases

    status = _gate_verdict(candidate, champion)
    return {
        "status": status,
        "candidate": candidate,
        "champion": champion,
        "cases": [
            {
                "case_name": r["case_name"],
                "test_type": r["test_type"],
                "pass": r["pass"],
                "success": r["success"],
                "timed_out": r["timed_out"],
                "judge_verdict": r["judge_verdict"],
                "completeness": r["completeness"],
                "confidence": r["confidence"],
                "expected_accuracy": r["expected_accuracy"],
                "duration_ms": r["duration_ms"],
                "iterations": r["iterations"],
                "assertions": r["assertions"],
                "error": r.get("error"),
                "assistant_excerpt": r["assistant_excerpt"],
            }
            for r in results
        ],
        "regressed_cases": regressed_cases,
    }
