"""Shadow divergence between a hardcoded rule and the primitive that replaces it.

Phase 2 of the Agent Harness migration. Phase 1 installed the primitives; Phase 0
instrumented the 12 rules. Neither answers the question Phase 3 depends on:

    *Does the rule ever change an outcome the model would not have reached anyway?*

This module answers it by comparing, on every live turn, **what the rule decided**
against **what the model actually did** — read from the turn's own tool-call trace.

Why the trace is the right primitive
------------------------------------
Every rule in the family asks some version of "should this turn produce X?", where
X is a deliverable, a skill, a research pass, a plan. The model's answer to the same
question is observable: did it call the tool that produces X, *before* the rule
intervened?

The trace is a prefix of the turn, and the forcing rules fire late (near the
iteration cap — Phase 0 recorded `iteration=9/10`), so when `should_force_create_file`
fires, the trace already holds every tool call the model made on its own. Comparing
at fire time is therefore exact, not approximate:

* trace has X, then the rule fires to ask for X  ->  the rule is **redundant**
* trace has no X, then the rule fires to ask for X  ->  the rule is **load-bearing**

Contract
--------
Same as ``rule_events``: **fail-open, cheap, off the DB thread.** Observation may
never raise into a turn, never block, never change a decision. Every failure is
counted in ``divergence_counters()["shadow_failures"]``.

Deliberately NOT done here: no counterfactual simulation, no replay. A trace cannot
prove the model *would* have created the deliverable had the rule stayed silent — it
can only show whether the model had already done so on its own. Labels are named
for exactly what they observe (e.g. ``suppression_unopposed`` rather than
"suppression worked") so no unproven claim enters the Phase 3 evidence.

Env
---
``HARNESS_SHADOW_ENABLED`` (default ``1``) — master switch. Off means
``record_rule_event`` writes Phase 0 rows exactly as before.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TRACE_MAX = 200

# Minimum evaluations before a rule may be called a deletion candidate. Below
# this the report says "insufficient_evidence" — Phase 3 must not delete on noise.
MIN_SAMPLE = 20

_trace: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "harness_shadow_trace", default=None
)
# Rules that fired this turn while the model had NOT yet acted — held until turn
# end, when the delivery question can actually be answered. See finish_turn().
_pending: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "harness_shadow_pending", default=None
)

_DIVERGENCE: Counter = Counter()
_SHADOW_FAILURES = 0


def enabled() -> bool:
    return os.getenv("HARNESS_SHADOW_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ── the turn's tool-call trace ───────────────────────────────────────────────
#
# Both ContextVars default to None, never to a list literal. A mutable default is
# shared by every context that has not set the var, so two turns that never called
# start_trace() would append to the SAME list and compare one user's tool calls
# against another user's rules. _calls()/_held() create the list on first use.

def _calls() -> list:
    calls = _trace.get()
    if calls is None:
        calls = []
        _trace.set(calls)
    return calls


def _held() -> list:
    held = _pending.get()
    if held is None:
        held = []
        _pending.set(held)
    return held


def start_trace() -> None:
    """Begin a fresh trace. Called once per turn, at the turn entry point."""
    if not enabled():
        return
    try:
        _trace.set([])
        _pending.set([])
    except Exception:  # pragma: no cover - ContextVar.set is effectively infallible
        pass


def clear_trace() -> None:
    """Drop the trace (turn end). Never raises."""
    try:
        _trace.set([])
        _pending.set([])
    except Exception:  # pragma: no cover
        pass


def note_tool_call(tool_name: str, arguments: Any = None) -> None:
    """Record one model-authored tool call. Never raises, never blocks.

    Called from ``execute_tool`` BEFORE any gating, so the trace records what the
    *model intended* — a call the harness later blocked is still evidence of the
    model's own judgment, which is the thing being compared.
    """
    if not enabled() or not tool_name:
        return
    try:
        calls = _calls()
        if len(calls) >= _TRACE_MAX:
            return
        action = None
        if isinstance(arguments, dict):
            raw_action = arguments.get("action") or arguments.get("type")
            if isinstance(raw_action, str):
                action = raw_action[:60]
        calls.append({
            "tool": str(tool_name)[:60],
            "action": action,
            "args": _args_digest(arguments),
        })
    except Exception:
        global _SHADOW_FAILURES
        _SHADOW_FAILURES += 1


def current_trace() -> list[dict]:
    try:
        return list(_trace.get() or [])
    except Exception:  # pragma: no cover
        return []


def _args_digest(arguments: Any, limit: int = 160) -> Optional[str]:
    """Clipped JSON of the call arguments — enough to see ``type=pptx`` etc."""
    if arguments is None:
        return None
    try:
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, default=str)
    except Exception:
        arguments = str(arguments)
    return arguments[:limit]


def trace_tools() -> set[str]:
    return {c.get("tool", "") for c in current_trace()}


def trace_has_tool(names: set[str]) -> bool:
    if not names:
        return False
    return bool(trace_tools() & names)


def trace_mentions(text: str) -> bool:
    """True when any traced call's arguments contain ``text`` (case-insensitive)."""
    needle = text.lower()
    for call in current_trace():
        if needle in str(call.get("args") or "").lower():
            return True
    return False


# ── rule -> primitive mapping ────────────────────────────────────────────────
# Tool names below are the real registry names (verified against
# app/services/tool_handlers/), not guesses.

_ARTIFACT_TOOLS = {"create_artifact", "edit_artifact"}
_DASHBOARD_TOOLS = {
    "create_fullstack_dashboard",
    "create_dashboard",
    "update_fullstack_dashboard",
    "update_dashboard",
}
_SKILL_TOOLS = {
    "skills", "run_sandbox_skill", "load_skill_body", "list_default_skills",
    "skills_hub", "skill_manager", "Skill", "skill",
}
_RESEARCH_TOOLS = {
    "web_search", "web_extract", "x_search", "agent_browser", "browser_navigate",
}
_TODO_TOOLS = {"todo", "update_todo"}


@dataclass(frozen=True)
class Primitive:
    """What the model's own behaviour looks like for the question a rule asks."""

    kind: str  # force | classifier | suppress | route | plan
    tools: frozenset[str] = field(default_factory=frozenset)
    # For targets that are a *property* of the call rather than the tool name
    # (a pptx artifact vs any artifact), a substring that must appear in args.
    args_must_contain: Optional[str] = None


SPECS: dict[str, Primitive] = {
    # Deliverable forcing — the family that produced the live bug.
    "file_turn_guard.should_force_create_file": Primitive("force", frozenset(_ARTIFACT_TOOLS)),
    "file_turn_guard.is_file_deliverable_request": Primitive("classifier", frozenset(_ARTIFACT_TOOLS)),
    "dashboard_turn_guard.should_force_create_dashboard": Primitive("force", frozenset(_DASHBOARD_TOOLS)),
    "dashboard_turn_guard.is_live_dashboard_request": Primitive("classifier", frozenset(_DASHBOARD_TOOLS)),
    "pptx_turn_guard.should_force_create_pptx": Primitive("force", frozenset(_ARTIFACT_TOOLS), "pptx"),
    "pptx_turn_guard.is_pptx_request": Primitive("classifier", frozenset(_ARTIFACT_TOOLS), "pptx"),
    "goal_contract.is_report_request": Primitive("force", frozenset(_ARTIFACT_TOOLS)),
    "turn_action.is_online_research_request": Primitive("force", frozenset(_RESEARCH_TOOLS)),
    # Suppression — the rule's question is inverted (it says "do NOT create").
    "intent_router.is_file_read_request": Primitive(
        "suppress", frozenset(_ARTIFACT_TOOLS | _DASHBOARD_TOOLS)
    ),
    # Skill routing.
    "intent_router.detect_file_intent": Primitive("route", frozenset(_SKILL_TOOLS | _ARTIFACT_TOOLS)),
    "intent_router.detect_deck_edit_intent": Primitive("route", frozenset(_ARTIFACT_TOOLS)),
    "default_skills.detect_soft_intent": Primitive("route", frozenset(_SKILL_TOOLS)),
    # turn_planner's server-authored plan vs the model planning for itself.
    "turn_planner.build_turn_plan": Primitive("plan", frozenset(_TODO_TOOLS)),
}


def spec_for(rule_name: str) -> Optional[Primitive]:
    return SPECS.get(rule_name)


def unmapped_rules() -> set[str]:
    """Observed rule names with no primitive mapping — a wiring gap, not a metric.

    Returns the difference between what ``rule_events`` has actually recorded and
    what this module knows how to compare. A newly instrumented rule shows up here
    until its primitive is declared, so the gap is visible instead of silent.
    """
    try:
        from app.services.harness.rule_events import counters
        seen = {k for k in counters() if not k.endswith("::fired")}
    except Exception:  # pragma: no cover - defensive
        return set()
    ignored = {"dropped", "buffered", "record_failures", "flush_failures"}
    return {name for name in seen if name not in ignored and name not in SPECS}


# ── the comparison ───────────────────────────────────────────────────────────

def model_acted(spec: Primitive) -> bool:
    """Did the model itself produce the thing this rule exists to ask for?

    When the primitive is a *property* of a call rather than a tool name (a pptx
    artifact, not any artifact), the property must also appear in the traced
    arguments. Absent it we report False — i.e. we fall back to "rule load-bearing",
    which blocks deletion. Under-detecting redundancy costs us a rule we keep; over-
    detecting it costs us a rule we should not have deleted.
    """
    if not trace_has_tool(set(spec.tools)):
        return False
    if spec.args_must_contain:
        return trace_mentions(spec.args_must_contain)
    return True


def classify(spec: Primitive, fired: bool, did: bool) -> str:
    """Name the observed relationship. Labels state evidence, not conclusions."""
    if spec.kind == "force":
        if fired and did:
            return "rule_redundant"      # model had already produced it
        if fired and not did:
            return "rule_load_bearing"   # nothing in the trace until the rule acted
        if not fired and did:
            return "rule_missed"         # rule said no, model did it anyway
        return "both_agree_idle"
    if spec.kind == "classifier":
        if fired and did:
            return "classification_corroborated"
        if not fired and did:
            return "classification_missed"
        if fired and not did:
            return "classification_unconfirmed"
        return "classification_idle"
    if spec.kind == "suppress":
        if fired and did:
            return "suppression_overridden"   # rule said don't create; it was created
        if fired and not did:
            return "suppression_unopposed"    # nothing created — cause NOT observable
        if not fired and did:
            return "creation_unconstrained"
        return "both_agree_idle"
    if spec.kind == "route":
        if fired and did:
            return "rule_redundant"
        if fired and not did:
            return "rule_load_bearing"
        if not fired and did:
            return "model_self_routed"        # the target state, with no rule
        return "both_agree_idle"
    if spec.kind == "plan":
        if fired and did:
            return "server_plan_model_todo"   # both planned; server plan redundant
        if fired and not did:
            return "server_plan_only"
        if not fired and did:
            return "model_plan_only"
        return "neither_planned"
    return "unclassified"


# Labels that mean "the rule and the model disagreed on the same question".
_DIVERGENT_LABELS = {
    "rule_redundant",              # rule asserted a need the model had already met
    "rule_missed",                 # rule denied a need the model acted on
    "classification_missed",
    "suppression_overridden",
    "server_plan_model_todo",
}

# Labels that mean the rule alone supplied the outcome -> NOT deletable.
_LOAD_BEARING_LABELS = {
    "rule_load_bearing",
    "server_plan_only",
}


def shadow_view(rule_name: str, fired: bool) -> Optional[dict]:
    """The comparison record merged into a Phase 0 row's ``detail``.

    Returns ``None`` when the rule has no declared primitive.
    """
    spec = spec_for(rule_name)
    if spec is None:
        return None
    did = model_acted(spec)
    label = classify(spec, fired, did)
    return {
        # Phase 2 contract: one flag to filter divergence rows by.
        "diverged": label in _DIVERGENT_LABELS,
        "primitive": {
            "kind": spec.kind,
            "model_acted": did,
            "args_matched": (
                trace_mentions(spec.args_must_contain) if spec.args_must_contain else None
            ),
            "trace_len": len(current_trace()),
            "trace_tools": sorted(trace_tools())[:12],
        },
        "agreement": label,
        "load_bearing": label in _LOAD_BEARING_LABELS,
        "shadow": True,
    }


def record_shadow(rule_name: str, fired: bool) -> Optional[dict]:
    """Compute + count the divergence for one rule evaluation. Never raises."""
    if not enabled():
        return None
    try:
        view = shadow_view(rule_name, fired)
        if view is None:
            return None
        global _SHADOW_FAILURES
        _DIVERGENCE[rule_name] += 1
        _DIVERGENCE[f"{rule_name}::{view['agreement']}"] += 1
        if view["load_bearing"]:
            _DIVERGENCE[f"{rule_name}::load_bearing"] += 1
            # Hold it: at fire time we can only say the model had not acted YET.
            # Whether the rule achieved anything is decided at turn end.
            try:
                _held().append(rule_name)
            except Exception:  # pragma: no cover
                pass
        if view["diverged"]:
            _DIVERGENCE[f"{rule_name}::diverged"] += 1
        return view
    except Exception:
        _SHADOW_FAILURES += 1
        return None


def finish_turn() -> dict:
    """Resolve this turn's unresolved fires against the FINAL trace.

    This is what makes the evidence mean something for the three force guards
    (file / dashboard / pptx). Each of them already returns False when the artifact
    exists — ``if file_artifact_created(...): return False`` — so by construction
    ``rule_redundant`` can never fire for them: a fire proves only that no
    deliverable existed *at that moment*.

    Resolving at turn end splits that ambiguity into the two facts Phase 3 needs:

      * ``rule_preceded_delivery`` — after the rule fired, the deliverable appeared
        in the same turn. Consistent with the rule working; causation is NOT
        observable and is not claimed.
      * ``rule_fired_no_delivery`` — the rule fired and the turn still produced
        nothing. The rule did not achieve its stated aim, which is evidence
        against it — and specifically evidence that deleting it loses nothing,
        because it lost nothing while it was there.

    Never raises.
    """
    if not enabled():
        return {}
    try:
        pending = list(_pending.get() or [])
        _pending.set([])
    except Exception:  # pragma: no cover
        return {}
    out: dict[str, list[str]] = {"rule_preceded_delivery": [], "rule_fired_no_delivery": []}
    for rule_name in pending:
        spec = spec_for(rule_name)
        if spec is None:
            continue
        try:
            label = (
                "rule_preceded_delivery" if model_acted(spec) else "rule_fired_no_delivery"
            )
            _DIVERGENCE[f"{rule_name}::{label}"] += 1
            out[label].append(rule_name)
        except Exception:
            global _SHADOW_FAILURES
            _SHADOW_FAILURES += 1
    return out


# ── reading the evidence ─────────────────────────────────────────────────────

def divergence_counters() -> dict:
    return {"counts": dict(_DIVERGENCE), "shadow_failures": _SHADOW_FAILURES}


def reset() -> None:
    global _SHADOW_FAILURES
    _DIVERGENCE.clear()
    _SHADOW_FAILURES = 0
    clear_trace()


def divergence_report() -> dict:
    """Per-rule deletion verdict — the Phase 3 shortlist.

    ``verdict`` is one of:
      * ``keep_but_ineffective`` — the rule fired with no model action and the turn
        STILL produced nothing (``rule_fired_no_delivery``, never
        ``rule_preceded_delivery``). It is not deletable on this evidence alone —
        it did not mis-decide — but it is not achieving its stated aim either.
        This is the Phase 4 case: the fix is a bounded evaluator loop, not silence.
      * ``keep_load_bearing``   — fired where the model had not already done it and
        the outcome followed. Do NOT delete.
      * ``delete_candidate``    — enough samples, never load-bearing. (Reachable for
        the text classifiers; NOT reachable for the three force guards, which
        already return False when the artifact exists — see ``finish_turn``.)
      * ``insufficient_evidence`` — fewer than ``MIN_SAMPLE`` evaluations.
    """
    counts = _DIVERGENCE
    rules: dict[str, dict] = {}
    for key, value in counts.items():
        if "::" in key:
            rule, label = key.split("::", 1)
            rules.setdefault(rule, {"evaluated": 0, "labels": {}})
            rules[rule]["labels"][label] = value
        else:
            rules.setdefault(key, {"evaluated": 0, "labels": {}})
            rules[key]["evaluated"] = value
    for name, stats in rules.items():
        labels = stats.get("labels", {})
        evaluated = stats.get("evaluated") or sum(labels.values()) or 0
        load_bearing = labels.get("load_bearing", 0)
        diverged = labels.get("diverged", 0)
        no_delivery = labels.get("rule_fired_no_delivery", 0)
        preceded = labels.get("rule_preceded_delivery", 0)
        stats["evaluated"] = evaluated
        stats["load_bearing"] = load_bearing
        stats["diverged"] = diverged
        stats["preceded_delivery"] = preceded
        stats["fired_no_delivery"] = no_delivery
        stats["divergence_rate"] = round(diverged / evaluated, 4) if evaluated else 0.0
        if load_bearing and no_delivery and not preceded:
            stats["verdict"] = "keep_but_ineffective"
        elif load_bearing:
            stats["verdict"] = "keep_load_bearing"
        elif evaluated >= MIN_SAMPLE:
            stats["verdict"] = "delete_candidate"
        else:
            stats["verdict"] = "insufficient_evidence"
    return {
        "rules": rules,
        "min_sample": MIN_SAMPLE,
        "unmapped": sorted(unmapped_rules()),
        "telemetry": {"shadow_failures": _SHADOW_FAILURES},
    }


__all__ = [
    "MIN_SAMPLE",
    "SPECS",
    "Primitive",
    "classify",
    "clear_trace",
    "current_trace",
    "divergence_counters",
    "divergence_report",
    "enabled",
    "finish_turn",
    "model_acted",
    "note_tool_call",
    "record_shadow",
    "reset",
    "shadow_view",
    "spec_for",
    "start_trace",
    "trace_has_tool",
    "trace_mentions",
    "trace_tools",
    "unmapped_rules",
]
