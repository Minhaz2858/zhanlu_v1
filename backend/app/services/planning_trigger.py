"""Planning layer trigger — decides when to invoke the SynexiaFSM.

The SynexiaFSM is the existing cognitive-loop state machine (see
``app/services/synexia/fsm.py``). It wraps the tool-calling loop with
GOAL → PLAN → GATE → ACT → OBSERVE → VERIFY states and is gated by
``settings.SYNEXIA_FSM_ENABLED``.

The gap was that the FSM was never invoked from the chat loop. This
module provides a single function, ``should_trigger_planning``, that
classifies a user message and returns True/False plus a confidence score.
The chat loop should call it at the start of ``add_message_stream`` and
route to the FSM when it returns True.

The classifier has two layers:

1. **Heuristic** (always available, regex-based, English-leaning):
   - Multi-step vocabulary — words like "then", "after", "and then",
     "first", "next" suggest a multi-step workflow.
   - Action-verb density — multiple imperative verbs (create, update,
     send, email, schedule, build, run) suggest several tool calls.
   - Explicit plan keywords — "plan", "workflow", "pipeline", "step
     by step" are near-certain triggers.

2. **LLM classifier** (optional, gated by ``settings.PLANNING_ROUTER_MODE``):
   - When mode is ``"llm"``, an LLM classifies the user message in any
     language via a classify-prompt returning strict JSON
     ``{should_plan, confidence}``. Falls back to heuristic on any
     LLM error or low confidence.
   - When mode is ``"hybrid"`` (default ``"heuristic"``), the heuristic
     runs first; the LLM is consulted only when the heuristic score is
     in a gray band (``0.2 <= score <= 0.6``), where English-only
     regex would otherwise mis-fire.

Single-shot questions (1 action verb, no connective words) bypass the
planning layer for fast response.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


_MULTI_STEP_CONNECTIVES = re.compile(
    r"\b(then|after that|afterwards|and then|next|finally|first[, ]|"
    r"step[- ]by[- ]step|once .* (is )?done|when .* (is )?done)\b",
    re.IGNORECASE,
)

_PLAN_KEYWORDS = re.compile(
    r"\b(plan|workflow|pipeline|orchestrate|sequence|schedule|automate|"
    r"multi[- ]step|set of tasks)\b",
    re.IGNORECASE,
)

_ACTION_VERBS = re.compile(
    r"\b(create|update|delete|send|email|schedule|build|run|deploy|"
    r"generate|export|upload|download|publish|notify|call|fetch|"
    r"compute|calculate|transform|convert|sync|register|invite|"
    r"process|analyze|classify|tag|merge|split)\b",
    re.IGNORECASE,
)


# LLM classifier signature: (user_message: str) -> dict | None
# Default uses the shared LLM service. Tests inject a stub.
LLMClassifyFn = Callable[[str], Optional[dict]]


def _heuristic_score(user_message: str) -> tuple[float, dict]:
    """Return (score in [0,1], raw signal counts). Pure function."""
    if not user_message or not user_message.strip():
        return 0.0, {}

    text = user_message.strip()
    connectives = len(_MULTI_STEP_CONNECTIVES.findall(text))
    plan_kw = len(_PLAN_KEYWORDS.findall(text))
    verbs = len(_ACTION_VERBS.findall(text))

    signals = {
        "connectives": connectives,
        "plan_keywords": plan_kw,
        "action_verbs": verbs,
    }

    score = 0.0
    if connectives >= 1:
        score += 0.4
    if plan_kw >= 1:
        score += 0.4
    if verbs >= 2:
        score += 0.4
    elif verbs == 1 and connectives >= 1:
        score += 0.2
    return min(score, 1.0), signals


def _classify_with_llm(user_message: str, llm_callable: Optional[LLMClassifyFn] = None) -> Optional[dict]:
    """Run the LLM classifier. Returns ``{should_plan, confidence, rationale}``
    or ``None`` on any failure (LLM unavailable, malformed response, timeout).
    """
    if llm_callable is None:
        try:
            from app.services.llm_service import chat_completion_json_sync
        except Exception as e:
            logger.debug("planning_trigger: llm service unavailable (%s); skipping LLM pass", e)
            return None
        llm_callable = chat_completion_json_sync  # type: ignore[assignment]

    prompt = (
        "You are a planning-router classifier. Given a user message, decide "
        "whether the agent should enter multi-step planning mode (which "
        "involves generating a plan, gating it through policy, and then "
        "executing tool calls in sequence) versus answering directly.\n\n"
        "Return strict JSON of the form "
        '{"should_plan": bool, "confidence": float_in_[0,1], "rationale": str}.\n\n'
        "Heuristics to apply:\n"
        "- Multi-step vocabulary (then, after, and then, next, finally, "
        "  step by step, 接着, 然后, 然后再, 之后, 然后再, 步骤, 流程, 计划).\n"
        "- Multiple imperative verbs (create, update, send, email, schedule, "
        "  build, run, 创建, 更新, 发送, 邮件, 安排, 构建, 运行).\n"
        "- Explicit plan keywords (plan, workflow, pipeline, automate, "
        "  multi-step, 计划, 流程, 流水线, 自动化, 多步).\n"
        "- Connective-heavy phrasing (… and then …, … first … then …).\n\n"
        "Be conservative: if the request is a single-shot question, return "
        "should_plan=false. Do not invent. Output ONLY the JSON.\n\n"
        f"USER MESSAGE:\n{user_message!r}\n"
    )
    try:
        out = llm_callable(prompt) or {}
        if not isinstance(out, dict):
            return None
        if "should_plan" not in out or "confidence" not in out:
            return None
        # Normalize types — be permissive on inputs.
        return {
            "should_plan": bool(out.get("should_plan")),
            "confidence": float(out.get("confidence", 0.0)),
            "rationale": str(out.get("rationale", "")),
        }
    except Exception as e:
        logger.warning("planning_trigger: LLM classify failed (non-fatal): %s", e)
        return None


@dataclass
class PlanTrigger:
    """Result of classifying a user message."""
    should_plan: bool
    confidence: float          # 0.0 - 1.0
    signals: dict[str, int]     # counts of each signal that fired
    source: str = "heuristic"  # "heuristic" | "llm" | "hybrid-llm" | "hybrid-heuristic"

    def __bool__(self) -> bool:  # allow `if PlanTrigger(...) and ...`
        return self.should_plan


# Gray band thresholds for the hybrid mode LLM escalation.
_HYBRID_LOW = 0.2
_HYBRID_HIGH = 0.6
# Minimum LLM confidence for the LLM verdict to override the heuristic.
_LLM_OVERRIDE_THRESHOLD = 0.6


def should_trigger_planning(
    user_message: str,
    llm_callable: Optional[LLMClassifyFn] = None,
) -> PlanTrigger:
    """Classify ``user_message`` and decide whether to invoke the planning layer.

    Behavior is governed by ``settings.PLANNING_ROUTER_MODE``:
    - ``"heuristic"`` (default): regex-only, no LLM call.
    - ``"llm"``: try LLM first; on None/low-confidence/exception, fall back to heuristic.
    - ``"hybrid"``: heuristic first; only call LLM when the heuristic score
      is in the gray band ``0.2 <= score <= 0.6`` (where English-only regex
      would otherwise mis-fire on multilingual inputs).

    Args:
        user_message: The raw user message text.
        llm_callable: Optional override for the LLM function (used in tests).

    Returns:
        A :class:`PlanTrigger` with ``should_plan``, ``confidence`` (0-1),
        the raw signal counts in ``signals``, and a ``source`` indicating
        which layer produced the verdict.
    """
    try:
        from app.config import settings
        mode = getattr(settings, "PLANNING_ROUTER_MODE", "heuristic")
    except Exception:
        mode = "heuristic"

    if not user_message or not user_message.strip():
        return PlanTrigger(False, 0.0, {}, source="heuristic")

    score, signals = _heuristic_score(user_message)

    if mode == "heuristic":
        # ── Simple-conversation bypass ────────────────────────────────
        # Short messages with no multi-step signals are always handled by
        # the fluid ReAct loop — never the rigid FSM.  This keeps simple
        # requests ("hello", "what is AI?", "create a report") natural and
        # fast instead of triggering the planning pipeline.
        text = user_message.strip()
        if (
            len(text) < 120
            and signals.get("connectives", 0) == 0
            and signals.get("plan_keywords", 0) == 0
            and signals.get("action_verbs", 0) <= 1
        ):
            return PlanTrigger(False, score, signals, source="heuristic-bypass")
        return PlanTrigger(score >= 0.6, score, signals, source="heuristic")

    if mode == "llm":
        llm = _classify_with_llm(user_message, llm_callable=llm_callable)
        if llm is None or llm["confidence"] < _LLM_OVERRIDE_THRESHOLD:
            # LLM unavailable or unsure — fall back to heuristic.
            return PlanTrigger(score >= 0.6, score, signals, source="heuristic")
        return PlanTrigger(
            should_plan=llm["should_plan"],
            confidence=llm["confidence"],
            signals=signals,
            source="llm",
        )

    if mode == "hybrid":
        if _HYBRID_LOW <= score <= _HYBRID_HIGH:
            llm = _classify_with_llm(user_message, llm_callable=llm_callable)
            if llm and llm["confidence"] >= _LLM_OVERRIDE_THRESHOLD:
                return PlanTrigger(
                    should_plan=llm["should_plan"],
                    confidence=llm["confidence"],
                    signals=signals,
                    source="hybrid-llm",
                )
        return PlanTrigger(score >= 0.6, score, signals, source="hybrid-heuristic")

    # Unknown mode — safest default is heuristic.
    return PlanTrigger(score >= 0.6, score, signals, source="heuristic")


# ---------------------------------------------------------------------------
# Non-data intent detection (for data-bound routing override)
# ---------------------------------------------------------------------------

# Lightweight stopwords for greetings / thanks / meta / capability questions.
# No domain keywords — works for any customer's schema and in Chinese.
_NON_DATA_STOPWORDS_EN: frozenset[str] = frozenset({
    "hello", "hi", "hey", "thanks", "thank", "ok", "okay", "yes",
    "no", "sure", "cool", "nice", "great", "bye", "lol", "hmm",
    "good", "morning", "afternoon", "evening", "please", "help",
    "what can you do", "who are you", "capabilities",
})

# Chinese equivalents — greetings / thanks / meta / capability.
_NON_DATA_STOPWORDS_ZH: frozenset[str] = frozenset({
    "你好", "您好", "嗨", "哈喽", "谢谢", "感谢", "好的", "好", "嗯",
    "再见", "拜拜", "拜", "早上好", "下午好", "晚上好", "请",
    "帮忙", "你能做什么", "你会什么", "你是谁", "功能", "能力",
})

# Combined set for quick membership test (after lower-casing + strip).
_NON_DATA_STOPWORDS: frozenset[str] = _NON_DATA_STOPWORDS_EN | _NON_DATA_STOPWORDS_ZH

# Short non-data patterns — these are always non-data regardless of length.
_NON_DATA_PATTERN_RE = re.compile(
    r"^(hi|hey|hello|你好|您好|thanks|thank you|谢谢|ok|okay|好的"
    r"|bye|goodbye|再见|拜拜|what can you do|你能做什么|你是谁"
    r"|who are you|capabilities)[\s!.?]*$",
    re.IGNORECASE,
)

# Numeric token pattern — if the message contains numbers (dates, amounts),
# it's likely a data question, not a greeting.
_NUMERIC_TOKEN_RE = re.compile(r"\d+")


def _is_non_data_intent(user_message: str) -> bool:
    """Return True for greeting/thanks/help/capability/meta questions.

    Returns False for any message that could possibly be a data question.
    Uses three layers — no domain vocabulary, works in any language/schema:

    Layer 1 — Exact match: message (stripped, lowered) is in stopwords.
    Layer 2 — Pattern match: message matches a short non-data regex.
    Layer 3 — Structural heuristic: very short (≤30 chars), no question
              mark, no numeric tokens → likely non-data.

    This function is deliberately **conservative** — it only returns True
    when it's confident the message is NOT a data question. Any ambiguity
    falls through to False, letting the data-bound FSM pipeline handle it.
    """
    text = user_message.strip()
    if not text:
        return True  # empty message → non-data

    lower = text.lower()

    # Layer 1: exact stopword match (covers both EN and ZH)
    if lower in _NON_DATA_STOPWORDS:
        return True

    # Layer 2: pattern match for common short non-data forms
    if _NON_DATA_PATTERN_RE.match(lower):
        return True

    # Layer 3: structural heuristic — very short, no question, no numbers
    if (
        len(text) <= 30
        and "?" not in text
        and "？" not in text
        and not _NUMERIC_TOKEN_RE.search(text)
    ):
        # Likely a greeting/thanks/acknowledgment
        # But check for data-adjacent words that could be short queries
        _DATA_ADJACENT_RE = re.compile(
            r"(?:数据|报告|销量|库存|客户|产品|表|查询|search|data|report|sales|inventory|customer|product|query|show|get|give|find|list|top)",
            re.IGNORECASE,
        )
        if _DATA_ADJACENT_RE.search(lower):
            return False  # short but data-adjacent → NOT non-data
        return True

    return False


# ---------------------------------------------------------------------------
# Follow-up refinement detection (routing-layer override)
# ---------------------------------------------------------------------------

# Pronouns that reference a prior turn's output ("make IT dark", "change THIS").
# Combined with task_spec_parser._REFINEMENT_VERBS (dark, theme, better, redo…)
# to form the full cue set. Matched on word boundaries for precision.
_FOLLOWUP_PRONOUNS = ("it", "this", "that", "these", "those")
_DASHBOARD_REFINEMENT_CUES = (
    "breakdown", "split by", "by customer", "by product", "by region",
    "by location", "drilldown", "drill down", "margin", "weekly view",
    "monthly view", "add tab", "filter", "segment",
)
_followup_cue_re_cache: Optional[re.Pattern] = None


def _followup_cue_regex() -> re.Pattern:
    """Lazily build (and cache) a word-boundary regex of all follow-up cues.

    Reuses ``task_spec_parser._REFINEMENT_VERBS`` so the verb list stays in
    one place (DRY). The import is deferred to avoid a module-load cycle.
    """
    global _followup_cue_re_cache
    if _followup_cue_re_cache is None:
        try:
            from app.services.synexia.task_spec_parser import _REFINEMENT_VERBS
        except Exception:
            _REFINEMENT_VERBS = ()
        cues = tuple(_REFINEMENT_VERBS) + _FOLLOWUP_PRONOUNS
        _followup_cue_re_cache = re.compile(
            r"\b(" + "|".join(re.escape(c) for c in cues) + r")\b",
            re.IGNORECASE,
        )
    return _followup_cue_re_cache


def is_followup_refinement(user_message: str, conv_ctx: Optional[dict]) -> bool:
    """Detect whether a short message is a follow-up refinement of a prior turn.

    Returns ``True`` when ALL hold:

    * ``conv_ctx`` has something to refine — a recent artifact OR a non-empty
      transcript (so genuine first turns and trivial chats never trigger).
    * the message is short (≤ 300 chars) — long messages are likely new
      requests, not refinements.
    * the message contains a refinement cue: a follow-up pronoun
      ("it"/"this"/"that") OR a refinement verb from ``_REFINEMENT_VERBS``
      ("dark", "theme", "better", "redo", …), matched on word boundaries.

    Used by the chat-loop router to force short refinement messages through
    the SynexiaFSM (which has full follow-up context wiring) instead of the
    legacy ReAct loop, which is context-blind on follow-ups. Non-fatal: any
    error returns ``False`` (degrades to the previous routing behavior).
    """
    try:
        if not conv_ctx:
            return False
        artifacts = conv_ctx.get("recent_artifacts") or []
        transcript = (conv_ctx.get("transcript") or "").strip()
        if not artifacts and not transcript:
            return False
        msg = (user_message or "").strip()
        if not msg or len(msg) > 300:
            return False
        if conv_ctx.get("dashboard_id"):
            lower = msg.lower()
            if any(cue in lower for cue in _DASHBOARD_REFINEMENT_CUES):
                return True
        return bool(_followup_cue_regex().search(msg))
    except Exception as exc:
        logger.debug("is_followup_refinement failed (non-fatal): %s", exc)
        return False
