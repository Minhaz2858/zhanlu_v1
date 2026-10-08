"""NL2SQL Planner — OCI agentic planning stage (P1-5) + multi-turn state (P1-6).

The reference design treats NL2SQL as a planning problem, not a translation
problem: before any retrieval or generation, resolve WHAT the user is asking —
the business entity, the metric, the time semantics and the default slice —
grounded in the semantic store's VERIFIED vocabulary (glossary terms, approved
metrics, metric definitions), not in the physical schema.

Two behaviours fall out of one structured plan:

* **Proceed with defaults.** When the planner resolves the question with high
  confidence, the resolution is injected into SQL generation as a
  ``<resolved-intent>`` block and the assumptions are surfaced in the result
  payload. The pipeline downstream is unchanged.
* **Clarify when uncertain.** When the question is genuinely ambiguous (the
  canonical "How did OCI do last month?") AND the caller can ask, the planner
  returns a structured clarification — metric / time / slice options — instead
  of guessing. The tool result carries it back to the calling agent LLM, which
  relays it to the user through its existing clarify channel.

Multi-turn state (P1-6): resolved entities, accepted defaults and rejected
interpretations persist per (conversation, KB) in ``nl2sql_turn_state``. A
follow-up turn in the same conversation starts from what was already resolved
("I mean revenue in North America") instead of re-deriving everything.

Design rules this module never breaks:

* **Flag-gated and fail-soft.** ``NL2SQL_PLANNER_ENABLED`` (default False);
  any error degrades to "no plan" — byte-identical legacy behaviour.
* **One bounded LLM call** (temperature 0, small max_tokens, JSON in / JSON
  out). The planner never queries data and never writes SQL.
* **Grounded, not creative.** Metric/entity candidates come from the semantic
  store's VERIFIED tier only; the planner may not invent a metric that the
  glossary cannot vouch for — it marks the slot unresolved instead.
* **State is advisory.** Turn state biases the next turn's resolution; it is
  never a cached answer (rows are always read live — see plan_memory.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.llm_router import LLMEndpoint
from app.services.llm_service import get_model, llm_headers, llm_url

logger = logging.getLogger(__name__)

#: Below this planner confidence the question counts as "genuinely ambiguous"
#: when it also self-reports ambiguity — the clarify gate opens.
_MIN_CONFIDENCE = 0.4
#: Clarification options stay small: this is a question for a human.
_MAX_OPTIONS = 4
#: The <resolved-intent> prompt block budget (shape steering, not an essay).
_MAX_GUIDANCE_CHARS = 1400

_PLANNER_SYSTEM_PROMPT = """You are the planning stage of an enterprise NL2SQL \
system. A user asked a data question against a business database. Before any \
SQL is written, resolve WHAT is being asked.

You receive: the question, and the business vocabulary (glossary terms, metric \
definitions) this database's semantic layer vouches for.

Return ONLY a JSON object:
{
  "intent": "metric | trend | comparison | detail | listing",
  "metric": "<the measure asked for, or null — must be one of the provided terms, else null>",
  "metric_definition": "<the matched term name, or null>",
  "entity": "<the business object the question is about, or null>",
  "time_range": {"type": "relative|explicit|none", "start": "YYYY-MM-DD|null",
                 "end": "YYYY-MM-DD|null", "label": "<e.g. last month>"},
  "slices": ["<dimensions to break down by>"],
  "confidence": 0.0-1.0,
  "ambiguous": true|false,
  "assumptions": ["<each default you applied, one short sentence>"],
  "clarification": {
    "question": "<one targeted question for the user>",
    "options": [{"label": "...", "meaning": "..."}]
  }
}

Rules:
- Resolve relative periods ("last month", "this quarter") to concrete dates \
against today's date: {today}.
- "metric" MUST match a provided vocabulary term. If the question's measure \
is not in the vocabulary, set metric=null and say so in assumptions.
- If ambiguous is true, fill "clarification" with ONE question whose options \
cover the plausible interpretations (metric, time, or slice — whichever is \
actually unclear). If ambiguous is false, set clarification=null.
- Never invent vocabulary. Never guess silently what you could resolve from \
the provided terms."""


async def _chat(
    messages: list[dict],
    endpoint: LLMEndpoint | None = None,
    max_tokens: int = 700,
) -> str:
    """Same single-turn chat contract as nl_answer_service._chat (kept local to
    avoid a circular import; the planner must be importable from anywhere)."""
    if endpoint is not None:
        _model = endpoint.model_id
        _url = endpoint.base_url.rstrip("/") + "/chat/completions"
        _api_key = endpoint.api_key or ""
        _headers = {
            "Authorization": f"Bearer {_api_key}" if _api_key else "",
            "Content-Type": "application/json",
        }
    else:
        _model = get_model()
        _url = llm_url()
        _headers = llm_headers()
    payload = {
        "model": _model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(_url, headers=_headers, json=payload)
        resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()


# ---------------------------------------------------------------------------
# Semantic grounding
# ---------------------------------------------------------------------------

def _grounding_block(
    db: Session,
    kb_id: str,
    question: str,
    project_id: str | None,
) -> str:
    """The VERIFIED vocabulary the planner may resolve against. Empty when the
    store has nothing for this KB — the planner then runs vocabulary-free and
    will mark the metric unresolved rather than invent one."""
    try:
        from app.services.db.semantic_store import get_semantic_store

        store = get_semantic_store(db, [kb_id], project_id=project_id)
        terms = store.glossary(question, limit=10) or []
    except Exception as e:
        logger.debug("planner: semantic store grounding unavailable: %s", e)
        return ""
    lines: list[str] = []
    for t in terms:
        line = f"- {t.term}: {t.definition or '(no definition)'}"
        if getattr(t, "aliases", None):
            line += f" (aliases: {', '.join(t.aliases)})"
        if getattr(t, "unit", None):
            line += f" [unit: {t.unit}]"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-turn state (P1-6)
# ---------------------------------------------------------------------------

def load_turn_state(
    db: Session, conversation_id: str | None, kb_id: str
) -> dict[str, Any] | None:
    """The resolved planning context carried forward in this conversation."""
    if not conversation_id or not kb_id:
        return None
    try:
        from app.models.nl2sql_turn_state import Nl2sqlTurnState

        row = (
            db.query(Nl2sqlTurnState)
            .filter(
                Nl2sqlTurnState.conversation_id == conversation_id,
                Nl2sqlTurnState.kb_id == kb_id,
            )
            .first()
        )
    except Exception as e:
        logger.debug("planner: turn state load skipped: %s", e)
        return None
    if row is None:
        return None
    return {
        "resolved": dict(row.resolved or {}),
        "defaults": dict(row.defaults or {}),
        "rejected": list(row.rejected or []),
    }


def save_turn_state(
    db: Session,
    conversation_id: str | None,
    kb_id: str,
    *,
    resolved: dict | None = None,
    defaults: dict | None = None,
    rejected: list | None = None,
) -> bool:
    """Upsert the planning context for (conversation, KB). Fail-soft."""
    if not conversation_id or not kb_id:
        return False
    try:
        from app.models.nl2sql_turn_state import Nl2sqlTurnState

        row = (
            db.query(Nl2sqlTurnState)
            .filter(
                Nl2sqlTurnState.conversation_id == conversation_id,
                Nl2sqlTurnState.kb_id == kb_id,
            )
            .first()
        )
        if row is None:
            row = Nl2sqlTurnState(
                conversation_id=conversation_id,
                kb_id=kb_id,
                resolved=dict(resolved or {}),
                defaults=dict(defaults or {}),
                rejected=list(rejected or []),
            )
            db.add(row)
        else:
            if resolved:
                merged = dict(row.resolved or {})
                merged.update(resolved)
                row.resolved = merged
            if defaults:
                merged_d = dict(row.defaults or {})
                merged_d.update(defaults)
                row.defaults = merged_d
            if rejected:
                known = list(row.rejected or [])
                for item in rejected:
                    if item not in known:
                        known.append(item)
                row.rejected = known[:20]
        db.commit()
        return True
    except Exception as e:
        logger.debug("planner: turn state save skipped: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _turn_state_block(state: dict[str, Any] | None) -> str:
    if not state:
        return ""
    parts: list[str] = []
    resolved = state.get("resolved") or {}
    if resolved:
        kv = ", ".join(f"{k}={v}" for k, v in resolved.items() if v)
        if kv:
            parts.append(f"- Resolved earlier in this conversation: {kv}")
    rejected = state.get("rejected") or []
    if rejected:
        parts.append(
            "- Interpretations the user already rejected: "
            + "; ".join(str(r) for r in rejected[:5])
        )
    if not parts:
        return ""
    return (
        "Conversation context (structured, from earlier turns):\n"
        + "\n".join(parts)
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _parse_plan_json(text: str) -> dict[str, Any] | None:
    """Extract the planner's JSON object. Tolerates markdown fences."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(
            ln for ln in cleaned.split("\n") if not ln.strip().startswith("```")
        ).strip()
    match = cleaned.find("{")
    end = cleaned.rfind("}")
    if match == -1 or end <= match:
        return None
    try:
        obj = json.loads(cleaned[match : end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _build_guidance(plan: dict[str, Any]) -> str:
    """The <resolved-intent> block injected into SQL generation."""
    parts = ["<resolved-intent>"]
    if plan.get("intent"):
        parts.append(f"- intent: {plan['intent']}")
    metric = plan.get("metric")
    if metric:
        line = f"- metric: {metric}"
        if plan.get("metric_definition"):
            line += f" (per glossary term: {plan['metric_definition']})"
        parts.append(line)
    if plan.get("entity"):
        parts.append(f"- entity: {plan['entity']}")
    tr = plan.get("time_range") or {}
    if isinstance(tr, dict) and tr.get("type") not in (None, "none"):
        window = "/".join(x for x in (tr.get("start"), tr.get("end")) if x)
        parts.append(
            f"- time range: {window or tr.get('label') or 'unresolved'}"
            f" ({tr.get('type')}, from {tr.get('label') or 'question'})"
        )
    slices = [s for s in (plan.get("slices") or []) if s]
    if slices:
        parts.append(f"- break down by: {', '.join(slices)}")
    assumptions = [a for a in (plan.get("assumptions") or []) if a]
    for a in assumptions[:4]:
        parts.append(f"- assumption: {a}")
    parts.append(
        "Write SQL that answers THIS resolved intent. Do not reinterpret the "
        "metric, the entity or the window; do not substitute a different "
        "measure for the one named above."
    )
    block = "\n".join(parts)
    return block[:_MAX_GUIDANCE_CHARS] if len(block) > _MAX_GUIDANCE_CHARS else block


def _norm_clarification(plan: dict[str, Any]) -> dict[str, Any] | None:
    """Validate + shrink the planner's clarification payload."""
    c = plan.get("clarification")
    if not isinstance(c, dict) or not (c.get("question") or "").strip():
        return None
    options: list[dict[str, str]] = []
    for opt in (c.get("options") or [])[:_MAX_OPTIONS]:
        if isinstance(opt, dict) and (opt.get("label") or "").strip():
            options.append(
                {
                    "label": str(opt.get("label")).strip(),
                    "meaning": str(opt.get("meaning") or "").strip(),
                }
            )
    if not options:
        return None
    return {"question": str(c["question"]).strip(), "options": options}


async def plan_question(
    db: Session,
    kb_id: str,
    question: str,
    *,
    endpoint: LLMEndpoint | None = None,
    project_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve the question's business intent before retrieval or generation.

    Returns ``None`` when the flag is off, the input is empty, or the planner
    failed — callers then run the legacy un-planned path unchanged.

    On success::

        {
            "intent": ..., "metric": ..., "metric_definition": ...,
            "entity": ..., "time_range": {...}, "slices": [...],
            "confidence": float, "ambiguous": bool,
            "assumptions": [...],
            "needs_clarification": bool,
            "clarification": {"question", "options"} | None,
            "guidance": "<resolved-intent> block for the SQL prompt",
            "carried_state": bool,   # turn state was available and used
        }
    """
    if not getattr(settings, "NL2SQL_PLANNER_ENABLED", False):
        return None
    if db is None or not (question or "").strip() or not kb_id:
        return None
    try:
        state = load_turn_state(db, conversation_id, kb_id)
        vocabulary = _grounding_block(db, kb_id, question, project_id)
        today = None
        try:
            from datetime import date

            today = date.today().isoformat()
        except Exception:
            today = "unknown"

        system = _PLANNER_SYSTEM_PROMPT.replace("{today}", today)
        user_parts = [f"Question: {question}"]
        if vocabulary:
            user_parts.append(f"\nBusiness vocabulary for this database:\n{vocabulary}")
        else:
            user_parts.append(
                "\nNo verified business vocabulary is available for this "
                "database: set metric=null unless the measure is explicit in "
                "the question itself."
            )
        state_block = _turn_state_block(state)
        if state_block:
            user_parts.append(f"\n{state_block}")
        text = await _chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
            endpoint=endpoint,
        )
        raw = _parse_plan_json(text)
        if not raw:
            logger.debug("planner: unparseable planner output, skipping")
            return None

        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        clarification = _norm_clarification(raw)
        ambiguous = bool(raw.get("ambiguous")) and confidence < _MIN_CONFIDENCE
        needs_clarification = bool(
            ambiguous
            and clarification is not None
            and getattr(settings, "NL2SQL_CLARIFY_ON_UNCERTAIN_ENABLED", False)
        )
        carried = bool(state and (state.get("resolved") or state.get("defaults")))
        plan: dict[str, Any] = {
            "intent": raw.get("intent"),
            "metric": raw.get("metric"),
            "metric_definition": raw.get("metric_definition"),
            "entity": raw.get("entity"),
            "time_range": raw.get("time_range")
            if isinstance(raw.get("time_range"), dict)
            else {},
            "slices": [s for s in (raw.get("slices") or []) if s][:4],
            "confidence": round(confidence, 2),
            "ambiguous": ambiguous,
            "assumptions": [a for a in (raw.get("assumptions") or []) if a][:6],
            "needs_clarification": needs_clarification,
            "clarification": clarification,
            "guidance": _build_guidance(raw),
            "carried_state": carried,
        }
        logger.info(
            "NL2SQL planner: kb=%s intent=%s metric=%r confidence=%.2f "
            "ambiguous=%s clarify=%s carried=%s",
            kb_id, plan["intent"], plan["metric"], plan["confidence"],
            ambiguous, needs_clarification, carried,
        )
        return plan
    except Exception as e:
        logger.debug("planner: plan_question failed (fail-soft): %s", e)
        return None
