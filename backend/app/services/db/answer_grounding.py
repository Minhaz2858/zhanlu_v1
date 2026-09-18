"""Figure provenance — every number in the final answer must exist in the tool results.

Why this exists: every SQL-side guard stops at the rows. The agent loop's only
anti-hallucination check (`_check_hallucination_guardrail` in routers/agents.py)
catches exactly one case — a data question answered WITHOUT calling a data tool
at all. Once `ask_data_agent` appears in the turn's tool calls, whatever prose
follows was accepted unverified. So the surviving failure mode is invented
*derivation*: a comparison the SQL never computed ("revenue up 12% MoM"), a total
nobody summed, a unit swap (元 vs 万元, 含税 vs 不含税), or a figure carried over
from an earlier turn and presented as current. None of those leave a trace.

Design rules, borrowed from the SQL-side contract (`db/answer_contract.py`): a
checker that flags CORRECT output is worse than no checker. So this module is
permissive about legitimate derivation and strict about invention.

A figure counts as grounded when any of these hold:

* it equals a number in the tool results after normalisation (0.5% relative
  tolerance, or equality once the grounded value is rounded to the figure's own
  precision — "≈3.46亿" for 345,678,912);
* it is a sum / min / max / mean of any numeric column of the returned rows
  (a model that says "total revenue 1.2M" across monthly rows is right);
* it is a ratio of two grounded numbers, for percentages;
* it appears in the user's own question (echoing "top 10 customers").

Numbers that are not data claims are deliberately ignored: bare integers below
100 (list counts, "3 recommendations"), years 1900-2100, ordinals, step numbers.
Without that filter the real findings drown in noise.

Nothing here blocks an answer. The caller retries once with prescriptive
feedback; a figure that survives the retry is logged loudly, never silently
accepted as fact.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ── normalisation -----------------------------------------------------------

_SCALES = {
    "万": 1e4,
    "亿": 1e8,
    "千": 1e3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "B": 1e9,
}

# Magnitude of a "yuan" style currency prefix — stripped, never scaled.
_CURRENCY = "¥￥$€£"

# A figure: grouped or plain number, optional scale suffix, optional percent.
_FIGURE_RE = re.compile(
    r"(?<![\w.,])"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>\s*(?:%|％|万|亿|千|[kKMB]))?"
)

# Bare integers below this are not treated as data claims.
_MIN_BARE_INT = 100

# Bound the work: a payload bigger than this is truncated before parsing.
_MAX_PAYLOAD_CHARS = 400_000
_MAX_ROWS_SCANNED = 5_000
_MAX_RATIO_VALUES = 200


@dataclass
class Figure:
    """One numeric mention in the answer text."""

    raw: str
    value: float
    context: str
    is_percent: bool = False


@dataclass
class GroundingReport:
    """Outcome of checking an answer against the turn's tool results."""

    figures_checked: int = 0
    grounded_size: int = 0
    unverified: list[Figure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unverified

    def feedback(self, limit: int = 6) -> str:
        """Instruction the model can act on, naming the offending figures."""
        shown = self.unverified[:limit]
        parts = [f"{f.raw} (in \"{f.context}\")" for f in shown]
        more = len(self.unverified) - len(shown)
        text = (
            "FIGURE CHECK FAILED: these figures in your answer do not appear in "
            "the tool results: " + "; ".join(parts)
        )
        if more > 0:
            text += f" (+{more} more)"
        return (
            text
            + ". State only figures you can read from the tool results above. If a "
            "comparison, total or percentage was not computed, either compute it "
            "with another query or say plainly that it was not computed — do not "
            "estimate it."
        )


# ── extraction --------------------------------------------------------------

def _is_ordinal(text: str, start: int, end: int) -> bool:
    """True for list markers / step numbers ("1.", "2)", "步骤 3")."""
    after = text[end:end + 2]
    if after[:1] in (".", ")", "、"):          # "1." / "1)" / "1、"
        return True
    before = text[max(0, start - 6):start]
    return bool(re.search(r"(?:step|步骤|第|phase|阶段)\s*$", before, re.I))


def _is_year(value: float, raw: str, has_suffix: bool) -> bool:
    return (not has_suffix) and 1900 <= value <= 2100 and "." not in raw


def extract_figures(text: str | None, *, min_bare_int: int = _MIN_BARE_INT) -> list[Figure]:
    """Pull the figures that are plausibly DATA claims out of some prose."""
    if not text:
        return []
    out: list[Figure] = []
    for m in _FIGURE_RE.finditer(text):
        raw_num = m.group("num")
        suffix = (m.group("suffix") or "").strip()
        try:
            base = float(raw_num.replace(",", ""))
        except ValueError:  # pragma: no cover - regex guarantees a number
            continue
        if _is_ordinal(text, m.start(), m.end()):
            continue
        is_percent = suffix in ("%", "％")
        scale = _SCALES.get(suffix, 1.0) if not is_percent else 1.0
        value = base * scale
        has_marker = bool(suffix) or "," in raw_num or "." in raw_num
        if not has_marker:
            if _is_year(base, raw_num, has_marker):
                continue
            if base < min_bare_int:
                # Not a data claim: a count of list items, a section number, a
                # "3 of 5" — pervasive in executive prose and not worth flagging.
                continue
        elif _is_year(base, raw_num, bool(suffix)):
            continue
        ctx_start = max(0, m.start() - 28)
        ctx_end = min(len(text), m.end() + 28)
        out.append(Figure(
            raw=(text[m.start():m.end()].strip()),
            value=value,
            context=re.sub(r"\s+", " ", text[ctx_start:ctx_end]).strip(),
            is_percent=is_percent,
        ))
    return out


# ── grounding ---------------------------------------------------------------

def _numeric_leaves(obj: Any, acc: list[float], depth: int = 0) -> None:
    """Collect every number anywhere in a decoded tool result."""
    if depth > 8:
        return
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        acc.append(float(obj))
        return
    if isinstance(obj, str):
        for m in _FIGURE_RE.finditer(obj):
            try:
                acc.append(float(m.group("num").replace(",", "")))
            except ValueError:
                pass
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _numeric_leaves(v, acc, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _numeric_leaves(v, acc, depth + 1)


def _column_aggregates(acc: list[float], obj: Any, depth: int = 0) -> None:
    """Add sum/min/max/mean of every numeric column of every row list.

    "Total revenue 1.2M" over twelve monthly rows is a correct answer that no
    single cell contains — a checker without this would flag it.
    """
    if depth > 8:
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _column_aggregates(acc, v, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        rows = [r for r in obj if isinstance(r, dict)]
        if rows and len(rows) == len(obj):
            columns: dict[str, list[float]] = {}
            for row in rows[:_MAX_ROWS_SCANNED]:
                for k, v in row.items():
                    if isinstance(v, bool):
                        continue
                    if isinstance(v, (int, float)):
                        columns.setdefault(k, []).append(float(v))
            for _col, vals in columns.items():
                if not vals:
                    continue
                acc.append(sum(vals))
                acc.append(min(vals))
                acc.append(max(vals))
                acc.append(sum(vals) / len(vals))
        for v in obj:
            _column_aggregates(acc, v, depth + 1)


def grounded_numbers(payloads: Iterable[str]) -> set[float]:
    """Every number the tool results make available, plus column aggregates."""
    acc: list[float] = []
    for payload in payloads or []:
        if not isinstance(payload, str) or not payload:
            continue
        text = payload[:_MAX_PAYLOAD_CHARS]
        try:
            decoded = json.loads(text)
        except Exception:
            _numeric_leaves(text, acc)
            continue
        _numeric_leaves(decoded, acc)
        _column_aggregates(acc, decoded)
    return {v for v in acc if v == v}  # drop NaN


def collect_tool_payloads(messages: list | None) -> list[str]:
    """Pull every tool-result body out of an agent message list.

    Conversation-scoped on purpose: this check answers "did the model invent a
    number", not "is the number current" — freshness and period coverage are the
    job of the SQL-side contract and the catalog freshness flags.
    """
    out: list[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            out.append(content)
        elif content is not None:
            try:
                out.append(json.dumps(content, default=str))
            except Exception:
                pass
    return out


def _matches(claim: float, grounded: float, tol: float) -> bool:
    """Claim ≈ grounded, allowing scale-free rounding and small tolerance."""
    if grounded == 0:
        return claim == 0
    if claim == grounded:
        return True
    if claim == round(grounded) and abs(claim) >= 1:
        return True
    return abs(claim - grounded) / abs(grounded) <= tol


def _percent_grounded(claim: float, grounded: set[float], tol: float) -> bool:
    """A percentage is grounded if stored as-is, as a fraction, or as a ratio."""
    for v in grounded:
        if _matches(claim, v, tol) or _matches(claim / 100.0, v, tol):
            return True
    values = sorted((v for v in grounded if v), key=abs, reverse=True)[:_MAX_RATIO_VALUES]
    for a in values:
        for b in values[:60]:
            if b == 0:
                continue
            if _matches(claim, a / b * 100.0, tol):
                return True
    return False


def check_answer_grounding(
    answer_text: str | None,
    payloads: Iterable[str] | None,
    question: str | None = None,
    *,
    tolerance: float = 0.005,
) -> GroundingReport:
    """Check every DATA figure in ``answer_text`` against the tool results."""
    report = GroundingReport()
    figures = extract_figures(answer_text)
    if not figures:
        return report
    grounded = grounded_numbers(payloads or [])
    # The question's own numbers are legitimate to repeat ("top 10 customers").
    for f in extract_figures(question, min_bare_int=1):
        grounded.add(f.value)
        if f.is_percent:
            grounded.add(f.value / 100.0)
    report.grounded_size = len(grounded)
    for f in figures:
        report.figures_checked += 1
        if f.is_percent:
            if not _percent_grounded(f.value, grounded, tolerance):
                report.unverified.append(f)
            continue
        if not any(_matches(f.value, g, tolerance) for g in grounded):
            report.unverified.append(f)
    return report


def log_ungrounded(report: GroundingReport, *, conversation_id: str | None = None) -> None:
    """Record a failed grounding check loudly — never swallow it."""
    if report.ok:
        return
    logger.warning(
        "Figure grounding: %d/%d figure(s) unverified after retries (conv=%s): %s",
        len(report.unverified), report.figures_checked, conversation_id,
        ", ".join(f.raw for f in report.unverified[:8]),
    )
