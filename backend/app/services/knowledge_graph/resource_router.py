"""Resource Router — deterministic per-question routing over project resources.

Given a user question and the resource types available in the project,
decide WHICH resource should answer it: a database, uploaded documents,
project memory/decisions, a report recipe, or several resources at once
(multi-resource / depth analysis).

Design contract:

- **Deterministic.** Ordered keyword rules; the same input always yields
  the same RouteDecision. No LLM call on the rule path (zero added cost).
- **Never raises.** Any internal error degrades to the conservative
  fallback: ``used_fallback=True`` means "behave exactly as today" —
  consumers must not change behavior for fallback decisions.
- **Availability-gated.** A confident route whose resource type is not
  available in the project degrades to the fallback.

Consumed (flag-gated ``KG_RESOURCE_ROUTER_ENABLED``) by delegation tools;
an optional LLM-assist hint can be passed via ``intent_hint`` (the
:class:`IntentResult.resource_route` passthrough from intent_classifier).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ResourceRoute(str, Enum):
    DATABASE = "database"
    DOCUMENT = "document"
    MEMORY = "memory"
    REPORT = "report"
    MULTI_RESOURCE = "multi_resource"


@dataclass
class RouteDecision:
    """Typed routing verdict consumed by delegation tools."""

    route: ResourceRoute = ResourceRoute.DATABASE
    resource_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0          # rules emit 1.0; LLM assist 0.5; fallback 0.0
    used_fallback: bool = True       # True = conservative default, behave as today


# ── keyword tables ─────────────────────────────────────────────────────────
# CJK terms match by substring; ASCII terms match with word boundaries so
# e.g. "count" does not fire on "account". All terms are generic query
# vocabulary — no domain-specific nouns.

_CJK = {
    "deep": ["为什么", "为啥", "原因", "根因", "深入分析", "深挖"],
    "report": ["报告", "报表", "周报", "月报", "日报", "季报", "年报"],
    "memory": ["之前", "上次", "还记得", "讨论过", "当时的决定", "决定是", "决策"],
    "document": ["文件", "文档", "上传", "附件", "资料里"],
    "database": [
        "多少", "查询", "统计", "平均", "总计", "总量", "总数",
        "最高", "最低", "排名", "趋势", "对比", "数据库", "字段",
    ],
}

_ASCII = {
    "deep": ["why", "root cause", "drill down", "deep dive", "drill into"],
    "report": ["report"],
    "memory": [
        "earlier", "last time", "we discussed", "what did we",
        "decid",  # decide / decided / decision
        "remember when",
    ],
    "document": ["document", "upload", "attachment", "pdf", "docx", "contract text"],
    "database": [
        "how many", "how much", "count", "sum", "total", "average",
        "max", "min", "top", "trend", "compare", "query", "sql",
        "table", "column", "database",
    ],
}

# Bilingual aliases used only to match recipe names against the question.
_RECIPE_TERM_ALIASES = {
    "sales": ["sales", "销售"],
    "inventory": ["inventory", "库存", "存货"],
    "customer": ["customer", "客户"],
    "weekly": ["weekly", "周", "周报", "周度"],
    "monthly": ["monthly", "月", "月报", "月度"],
    "daily": ["daily", "日", "日报"],
}

_ASCII_PATTERNS: dict[str, list[re.Pattern]] = {
    cat: [re.compile(r"\b" + re.escape(term), re.IGNORECASE) for term in terms]
    for cat, terms in _ASCII.items()
}

_ALL_RESOURCE_TYPES = {"database", "document", "memory", "report"}


# ── public API ─────────────────────────────────────────────────────────────

def route_question(
    question: str,
    *,
    available_resources: set[str] | None = None,
    recipe_names: list[str] | None = None,
    intent_hint: str | None = None,
) -> RouteDecision:
    """Route ``question`` to a resource. Never raises.

    Args:
        question: raw user text.
        available_resources: resource types present in the project
            (subset of {"database", "document", "memory", "report"});
            ``None`` means "unknown — assume all available".
        recipe_names: enabled report recipe names (for REPORT resource_ids).
        intent_hint: optional route value from the LLM intent classifier
            (used only when no rule fires; confidence 0.5).
    """
    try:
        return _route(
            question,
            available_resources=available_resources,
            recipe_names=recipe_names,
            intent_hint=intent_hint,
        )
    except Exception as e:  # never propagate — conservative fallback
        logger.debug("resource_router: routing failed (%s) — fallback", e)
        return RouteDecision(route=ResourceRoute.DATABASE, used_fallback=True)


def available_resources_from_context(
    context: dict[str, Any],
    db: Any = None,
) -> set[str]:
    """Derive available resource types from a tool-context extras dict.

    Looks at ``bound_kb_ids`` (KB rows distinguish databases from document
    stores), ``project_id`` (project memory), and enabled report recipes.
    Defensive: any lookup failure simply omits that resource type.
    """
    avail: set[str] = set()
    try:
        bound_kb_ids = list((context or {}).get("bound_kb_ids") or [])
        if bound_kb_ids and db is not None:
            from app.models.knowledge_base import KnowledgeBase

            rows = (
                db.query(KnowledgeBase.source_kind, KnowledgeBase.db_type)
                .filter(KnowledgeBase.id.in_(bound_kb_ids))
                .all()
            )
            for source_kind, db_type in rows:
                if db_type or (source_kind or "") == "db":
                    avail.add("database")
                else:
                    avail.add("document")
        elif bound_kb_ids:
            avail.add("database")
    except Exception as e:
        logger.debug("resource_router: KB availability lookup failed: %s", e)

    try:
        if (context or {}).get("project_id"):
            avail.add("memory")
    except Exception:
        pass

    try:
        from app.config import settings

        if getattr(settings, "REPORT_RECIPES_ENABLED", False) and avail is not None:
            avail.add("report")
    except Exception:
        pass

    return avail


# ── internals ──────────────────────────────────────────────────────────────

def _route(
    question: str,
    *,
    available_resources: set[str] | None,
    recipe_names: list[str] | None,
    intent_hint: str | None,
) -> RouteDecision:
    q = (question or "").strip()
    avail = (
        set(available_resources)
        if available_resources is not None
        else set(_ALL_RESOURCE_TYPES)
    )
    if not q:
        return _fallback(avail)

    hits = {cat for cat in (*_CJK.keys(),) if _matches(q, cat)}

    # Optional LLM assist: only when no rule fired.
    if not hits and intent_hint:
        try:
            hinted = ResourceRoute(str(intent_hint).strip().lower())
        except ValueError:
            hinted = None  # type: ignore[assignment]
        if hinted is not None and _route_available(hinted, avail):
            return RouteDecision(
                route=hinted,
                resource_ids=[],
                confidence=0.5,
                used_fallback=False,
            )
        return _fallback(avail)

    if not hits:
        return _fallback(avail)
    if "deep" in hits:
        route = ResourceRoute.MULTI_RESOURCE
    elif len(hits) >= 2:
        route = ResourceRoute.MULTI_RESOURCE
    elif "report" in hits:
        route = ResourceRoute.REPORT
    elif "memory" in hits:
        route = ResourceRoute.MEMORY
    elif "document" in hits:
        route = ResourceRoute.DOCUMENT
    else:  # database
        route = ResourceRoute.DATABASE

    if not _route_available(route, avail):
        return _fallback(avail)

    resource_ids: list[str] = []
    if route == ResourceRoute.REPORT and recipe_names:
        resource_ids = _match_recipes(q, recipe_names)

    return RouteDecision(
        route=route,
        resource_ids=resource_ids,
        confidence=1.0,
        used_fallback=False,
    )


def _matches(q: str, category: str) -> bool:
    for term in _CJK.get(category, []):
        if term in q:
            return True
    for pat in _ASCII_PATTERNS.get(category, []):
        if pat.search(q):
            return True
    return False


def _route_available(route: ResourceRoute, avail: set[str]) -> bool:
    if route == ResourceRoute.MULTI_RESOURCE:
        return len(avail & _ALL_RESOURCE_TYPES) >= 2
    return route.value in avail


def _fallback(avail: set[str]) -> RouteDecision:
    for pref in ("database", "document", "memory"):
        if pref in avail:
            return RouteDecision(route=ResourceRoute(pref), used_fallback=True)
    return RouteDecision(route=ResourceRoute.DATABASE, used_fallback=True)


def _match_recipes(q: str, recipe_names: list[str]) -> list[str]:
    """Score recipe names against the question via bilingual term aliases."""
    scored: list[tuple[int, str]] = []
    for name in recipe_names:
        parts = [p for p in re.split(r"[_\-\s]+", name.lower()) if p]
        score = 0
        for part in parts:
            aliases = _RECIPE_TERM_ALIASES.get(part, [part])
            if any(a in q for a in aliases if any("\u4e00" <= ch <= "\u9fff" for ch in a)) or any(
                re.search(r"\b" + re.escape(a), q, re.IGNORECASE)
                for a in aliases
                if not any("\u4e00" <= ch <= "\u9fff" for ch in a)
            ):
                score += 1
        if score:
            scored.append((score, name))
    scored.sort(key=lambda x: -x[0])
    return [name for _, name in scored]


__all__ = [
    "ResourceRoute",
    "RouteDecision",
    "route_question",
    "available_resources_from_context",
]
