"""Static intent resolver for known business questions.

DE-HARDCODED (2026-08-27): this module carries NO table names, column names,
or business hints. It is fully generic — an industry-agnostic platform core.

How routing works now:
  - Platform default: NO static routes. The NL model selects tables via
    schema discovery (TOC / vector linker) exactly like any other question.
  - Per-app knowledge: an app can ship a domain config file
    (backend/app/domain_configs/<agent_name>.json) whose "static_routes"
    array pins well-known intents to that app's OWN tables. The route
    resolver below loads routes from that file at runtime; when the app has
    no config, it returns None (generic behavior).

The NL model still writes the SQL; we only ANCHOR the FROM/JOIN target when
a per-app route exists.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_COMPILED_CACHE: dict[str, list[dict]] = {}


def _compile_route(route: dict) -> dict | None:
    """Compile one route dict (patterns → regexes). Returns None if invalid."""
    patterns = route.get("patterns") or []
    regexes = []
    for p in patterns:
        try:
            regexes.append(re.compile(p, re.IGNORECASE))
        except re.error:
            logger.warning("query_router: bad route pattern %r ignored", p)
    if not regexes or not route.get("table"):
        return None
    return {
        "table": route["table"],
        "hint_columns": route.get("hint_columns", []),
        "fallback_tables": route.get("fallback_tables", []),
        "date_hint": route.get("date_hint"),
        "regexes": regexes,
    }


def _routes_for(agent_name: str | None = None) -> list[dict]:
    """Return compiled routes for an agent (cached). Empty = fully generic."""
    from app.services.domain_config import get_static_routes

    key = (agent_name or "")
    if key in _COMPILED_CACHE:
        return _COMPILED_CACHE[key]
    compiled: list[dict] = []
    for r in get_static_routes(agent_name):
        c = _compile_route(r)
        if c:
            compiled.append(c)
    _COMPILED_CACHE[key] = compiled
    return compiled


def resolve_static_route(question: str, agent_name: str | None = None) -> dict | None:
    """Return the pinned route dict when the question matches a known intent.

    Returns ``None`` when no route matches or the agent has no domain config
    (the caller then proceeds with normal schema linking). Route dict shape::

        {"table": str, "hint_columns": list[str], "fallback_tables": list[str]}

    Pure function: no DB, no I/O — unit-testable.
    """
    if not question or not question.strip():
        return None
    q = question.strip()
    for r in _routes_for(agent_name):
        for rx in r["regexes"]:
            if rx.search(q):
                logger.info(
                    "query_router: static route hit table=%s (question=%.60s)",
                    r["table"], q,
                )
                return r
    return None


def clear_route_cache() -> None:
    _COMPILED_CACHE.clear()
