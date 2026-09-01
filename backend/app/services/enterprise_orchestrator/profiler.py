"""Dynamic Resource & Intent Profiler — LLM-driven facet planner.

Replaces a static keyword router: a single LLM call (structured-JSON output,
temperature=0, fail-open) decomposes ANY enterprise question into a
3-6 facet data-collection plan, using the cached DB schema slice to bind
facets to real warehouse resources.

Design spec reference: §6 Dynamic Resource & Intent Profiler.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain vocabulary (must match domain_labels.py keys).
# ---------------------------------------------------------------------------
VALID_DOMAINS = frozenset(
    {
        "supply_chain",
        "financial_performance",
        "sales_operations",
        "risk_management",
        "logistics",
        "hr",
        "procurement",
        "generic",
    }
)

# Services the executor may invoke by reflection. Anything else emitted by
# the LLM is flagged unavailable at execution time (enforced in code, never
# trusted to the LLM). Currently empty: no reflected service classes are
# registered on the generic platform — every facet goes through ad_hoc_query.
SERVICE_WHITELIST = frozenset()

MIN_FACETS = 3
MAX_FACETS = 6
PROFILER_TIMEOUT_S = 30.0
MAX_TOKENS = 1500

_JSON_SCHEMA_BLOCK = """{
  "domain": "financial_performance" | "supply_chain" | "sales_operations"
            | "risk_management" | "logistics" | "hr" | "procurement"
            | "generic",
  "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} | null,
  "primary_metric": "<one short label, e.g. 'gross_margin_pct'>",
  "segments": ["<filter_expr>", ...],
  "facets": [
    {
      "facet_id": "<short_snake_case_id>",
      "kind": "service_call" | "ad_hoc_query",
      "service": "<fully.qualified.method>",
      "args": {<kwargs>},
      "natural_language": "<NL query>",
      "suggested_tables": ["<table_name>", ...],
      "purpose": "primary" | "auxiliary" | "contextual"
    }
  ]
}"""

_PROFILER_PROMPT = """You are a business-data facet planner. Given a user's enterprise query
and the available database schema slice, output a JSON plan for a
multi-facet data-collection pipeline. Do NOT execute queries — just plan.

USER QUERY:
{user_message}

AVAILABLE DATABASE SCHEMA SLICE:
{schema_slice}

Output JSON matching EXACTLY this schema:
{schema}

Rules:
- Prefer service_call facets when the requested metric is covered by a
  registered whitelisted service (see SERVICE_WHITELIST). Use ad_hoc_query
  for everything else.
- Always include at least one "primary" facet for the main metric.
- For "why is X dropping" questions, include both the metric facet AND
  a decomposition facet (by region / product / customer).
- For margin/profit questions, include both revenue AND cost facets.
- For supply-chain questions, include inventory + market events facets.
- Emit between 3 and 6 facets total.
- Output ONLY the JSON. No prose, no markdown fences."""


class FacetSpec(TypedDict):
    """One planned data-collection facet."""

    facet_id: str
    kind: str  # "service_call" | "ad_hoc_query"
    service: str  # only for kind=service_call
    args: dict  # only for kind=service_call
    natural_language: str  # only for kind=ad_hoc_query
    suggested_tables: list[str]  # only for kind=ad_hoc_query
    purpose: str  # "primary" | "auxiliary" | "contextual"


class EnterpriseIntent(TypedDict):
    """Normalized output of the profiler."""

    domain: str
    period: tuple[str, str] | None
    primary_metric: str
    segments: list[str]
    facets: list[FacetSpec]


def build_profiler_prompt(user_message: str, schema_slice: str) -> str:
    """Build the profiler prompt without brace-collision issues."""
    return _PROFILER_PROMPT.format(
        user_message=(user_message or "").strip()[:2000],
        schema_slice=(schema_slice or "(no schema slice available)")[:3000],
        schema=_JSON_SCHEMA_BLOCK,
    )


def _repair_json(raw: str) -> dict | None:
    """Tolerantly parse an LLM JSON response: strip fences, find first {...}."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_period(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", end
    ):
        return (start, end)
    return None


def _sanitize_facet(raw: Any) -> FacetSpec | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in ("service_call", "ad_hoc_query"):
        return None
    facet_id = raw.get("facet_id")
    if not isinstance(facet_id, str) or not facet_id.strip():
        return None
    purpose = raw.get("purpose") if isinstance(raw.get("purpose"), str) else "auxiliary"
    if purpose not in ("primary", "auxiliary", "contextual"):
        purpose = "auxiliary"
    spec: FacetSpec = {
        "facet_id": facet_id.strip()[:80],
        "kind": kind,
        "service": raw.get("service") if isinstance(raw.get("service"), str) else "",
        "args": raw.get("args") if isinstance(raw.get("args"), dict) else {},
        "natural_language": (
            raw.get("natural_language") if isinstance(raw.get("natural_language"), str) else ""
        ),
        "suggested_tables": [
            t for t in raw.get("suggested_tables", []) if isinstance(t, str)
        ][:10],
        "purpose": purpose,
    }
    if kind == "service_call" and not spec["service"]:
        return None
    if kind == "ad_hoc_query" and not spec["natural_language"].strip():
        return None
    return spec


def _normalize_intent(data: dict) -> EnterpriseIntent | None:
    """Validate + clamp the raw LLM plan into a usable EnterpriseIntent."""
    domain = data.get("domain")
    if not isinstance(domain, str) or domain not in VALID_DOMAINS:
        domain = "generic"

    raw_facets = data.get("facets")
    if not isinstance(raw_facets, list) or not raw_facets:
        logger.info("profiler: no facets planned; fail-open to existing path")
        return None
    facets = [
        f for f in (_sanitize_facet(x) for x in raw_facets) if f is not None
    ]
    if not facets:
        return None
    facets = facets[:MAX_FACETS]
    if not any(f["purpose"] == "primary" for f in facets):
        facets[0]["purpose"] = "primary"

    primary_metric = data.get("primary_metric")
    if not isinstance(primary_metric, str) or not primary_metric.strip():
        primary_metric = "core_metric"

    segments = data.get("segments")
    if not isinstance(segments, list):
        segments = []
    segments = [s for s in segments if isinstance(s, str)][:10]

    return EnterpriseIntent(
        domain=domain,
        period=_parse_period(data.get("period")),
        primary_metric=primary_metric.strip()[:120],
        segments=segments,
        facets=facets,
    )


def profile_enterprise_intent(
    user_message: str,
    schema_slice: str = "",
    llm_caller: Callable[[str], dict] | None = None,
) -> EnterpriseIntent | None:
    """Plan 3-6 data-collection facets for an enterprise query.

    Fail-open contract (design spec §6.3):
      - empty/missing user message           → None
      - LLM call failure / timeout / 5xx      → None
      - malformed JSON (unrepairable)         → None
      - 0 facets or > 6 facets                → None / clamped to 6
    The caller MUST fall through to the existing single-query path when
    this returns ``None`` (zero regression for non-business queries).

    ``llm_caller`` is injectable for tests: ``(prompt: str) -> dict``.
    The default uses ``chat_completion_json_sync`` (30s timeout, provider
    failover, returns ``{}`` on total failure).
    """
    if not user_message or not user_message.strip():
        return None

    prompt = build_profiler_prompt(user_message, schema_slice)

    if llm_caller is None:
        try:
            from app.services.llm_service import chat_completion_json_sync

            def _default_caller(p: str) -> dict:
                return chat_completion_json_sync(p, temperature=0.0)

            llm_caller = _default_caller
        except Exception as exc:  # pragma: no cover - import-time guard
            logger.error("profiler: failed to import LLM caller: %s", exc)
            return None

    try:
        data = llm_caller(prompt)
    except Exception as exc:
        logger.warning("profiler: LLM call failed (%s); fail-open to existing path", exc)
        return None
    if not data:
        return None

    # chat_completion_json_sync may return {"response": "<raw text>"} when the
    # provider lacks native JSON mode — repair the embedded text if needed.
    if "facets" not in data and isinstance(data.get("response"), str):
        repaired = _repair_json(data["response"])
        if repaired is not None:
            data = repaired
    if "facets" not in data:
        repaired = _repair_json(json.dumps(data, ensure_ascii=False))
        if repaired is not None:
            data = repaired

    return _normalize_intent(data)
