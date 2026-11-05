"""Platform reliability & performance metrics (Phase 3.4).

Aggregates the mission metric table from EXISTING records only —
AgentInvocation (outcomes, latency, tokens, cost, recovery) and AgentRunStep
(tool success/failure, delegation frequency). No second telemetry system.

Honesty rules:
- Costs: only entries with a recorded ``cost_amount`` are summed. Executions
  without known model pricing are counted as ``unknown_pricing_executions``
  and NEVER estimated (mission §F).
- The provider probe reports configuration + a LIVE availability flag that is
  only set by an actual out-of-band probe call — mocked/deterministic tests
  can never claim live verification. Secrets never appear in the payload.

Live evaluation runs remain BLOCKED until a funded provider is available;
this module exists so the measurements are ready the moment it is.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))


def platform_reliability_report(db: Session, *, org_id: str, days: int = 30) -> dict:
    """The metric table: task completion rate, tool success rate, recovery
    frequency, delegation frequency, latency, token usage, execution cost,
    recovery duplication rate."""
    from app.models.agent_invocation import AgentInvocation
    from app.models.agent_run_step import AgentRunStep

    invs = (
        db.query(AgentInvocation)
        .filter(
            AgentInvocation.org_id == org_id,
            AgentInvocation.created_date >= _since(days),
        )
        .all()
    )
    steps = (
        db.query(AgentRunStep)
        .filter(
            AgentRunStep.org_id == org_id,
            AgentRunStep.created_date >= _since(days),
            AgentRunStep.step_type == "tool_call",
        )
        .all()
    )

    n = len(invs) or 1
    terminal = [i for i in invs if i.status in ("completed", "failed", "timeout", "cancelled")]
    successes = sum(
        1 for i in terminal
        if i.status == "completed" and (i.outcome or "success") == "success"
    )
    tool_ok = sum(1 for s in steps if s.status == "completed")
    tool_total = sum(
        1 for s in steps if s.status in ("completed", "failed", "timeout", "cancelled")
    )
    recoveries = sum(
        1 for i in invs
        if isinstance(i.output_json, dict)
        and isinstance(i.output_json.get("recovery"), dict)
    )
    durations = [int(i.duration_ms) for i in invs if i.duration_ms is not None]
    prompt = completion = 0
    known_cost = 0.0
    unknown_pricing = 0
    for i in invs:
        tu = i.token_usage if isinstance(i.token_usage, dict) else {}
        prompt += int(tu.get("prompt_tokens") or 0)
        completion += int(tu.get("completion_tokens") or 0)
        if i.cost_amount is not None:
            known_cost += float(i.cost_amount)
        else:
            unknown_pricing += 1

    # Recovery duplication: recovered invocations whose terminal outcome is
    # still failure — the recovery ran but did not produce a success.
    failed_recoveries = sum(
        1 for i in invs
        if isinstance(i.output_json, dict)
        and isinstance(i.output_json.get("recovery"), dict)
        and i.status in ("failed", "timeout")
    )
    dup_rate = (failed_recoveries / recoveries) if recoveries else 0.0

    delegation_markers = ("delegate_task", "_delegate_batch", "ask_data_agent")
    delegations = sum(
        1 for s in steps if (s.tool_name or "") in delegation_markers
    )

    return {
        "window_days": days,
        "task_completion_rate": round(successes / max(1, len(terminal)), 4),
        "task_completion_count": [successes, max(0, len(terminal) - successes)],
        "tool_success_rate": round(tool_ok / tool_total, 4) if tool_total else None,
        "recovery_success_rate": (
            round((recoveries - failed_recoveries) / recoveries, 4) if recoveries else None
        ),
        "delegation_success_rate": None,  # needs outcome-tagged delegation runs (see TODO below)
        "delegation_frequency": delegations,
        "agent_selection_accuracy": None,  # live-eval metric (reviewer-judged), not derivable here
        "evidence_accuracy": None,  # live-eval metric
        "citation_validity": None,  # live-eval metric
        "partial_result_accuracy": None,  # live-eval metric
        "recovery_frequency": recoveries,
        "recovery_duplication_rate": round(dup_rate, 4),
        "avg_latency_ms": round(sum(durations) / len(durations)) if durations else None,
        "token_usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "execution_cost": {
            "known_total": round(known_cost, 6),
            "unknown_pricing_executions": unknown_pricing,
            "estimated": False,  # we NEVER estimate — unknown stays unknown
        },
    }


def provider_status(*, probe: bool = False, timeout_s: float = 8.0,
                    sidecar_path=None) -> dict:
    """Provider configuration/availability state. NEVER includes secrets.

    ``probe=True`` performs ONE minimal live call (out-of-band use only —
    not part of the deterministic suite). Without a probe, ``available`` is
    None (unknown) — never claimed from mocks. Failed probes persist the
    error CATEGORY via the provider-diagnostics sidecar (``sidecar_path``
    override for tests).
    """
    from app.config import settings

    configured = bool(getattr(settings, "OPENAI_API_KEY", ""))
    status = {
        "configured": configured,
        "available": None,  # unknown until a real probe succeeds
        "probed": False,
    }
    if not probe or not configured:
        return status
    try:
        # ONE minimal live call (1 token) — out-of-band use only.
        from app.services.llm_service import llm_headers, llm_url

        resp = httpx.post(
            llm_url(),
            headers=llm_headers(),
            json={
                "model": getattr(settings, "LLM_MODEL", "") or "gpt-4o-mini",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            timeout=timeout_s,
        )
        if resp.status_code == 200:
            status["available"] = True
        else:
            status["available"] = False
            body = resp.text[:200]
            status["unavailable_reason"] = (
                f"HTTP {resp.status_code}: {body}".replace("sk-", "sk-***")
            )
    except Exception as exc:  # noqa: BLE001 — the failure reason IS the signal
        status["available"] = False
        reason = str(exc)[:200]
        # Quota/429 signals recorded verbatim (minus any secret-looking parts)
        status["unavailable_reason"] = reason.replace("sk-", "sk-***")
    if status["available"] is False:
        # Phase 3.5 P5: persist the error CATEGORY (never the message/keys)
        # for the offline provider diagnostics command.
        try:
            from app.services.provider_diagnostics import record_probe_error

            _reason = status.get("unavailable_reason", "")
            if "401" in _reason or "invalid_api_key" in _reason:
                _cat = "invalid_credentials"
            elif "429" in _reason or "quota" in _reason.lower():
                _cat = "quota_exhausted"
            elif "timeout" in _reason.lower():
                _cat = "timeout"
            else:
                _cat = "unknown"
            record_probe_error(_cat, sidecar_path=sidecar_path)
        except Exception:  # noqa: BLE001
            pass
    status["probed"] = True
    return status
