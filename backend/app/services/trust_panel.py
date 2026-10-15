"""TrustPanel — the standard trust contract for every answer (P1-5, G8).

IBM ecommerce finding: TRUST is the #1 success factor. This platform
already produces all the ingredients (citations from tool calls,
claim verification, data-source freshness) but nothing standardizes
them into one surfaceable contract.

The contract (attached to the v3 ``done`` frame as ``trust``):
    {
      "citations":      [{source_id, source_name, rows?}...]   (≤ 8)
      "confidence":     float | None                           (0..1)
      "data_freshness": {table: {last_data_date, stale_days}} | None
      "grade":          "verified" | "sourced" | "unsourced"
    }

grade is HONEST: "verified" only when claims were verified against the
data, "sourced" when citations exist, "unsourced" otherwise — never a
made-up middle value.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_CITATIONS = 8


def _normalize_citations(sources) -> list[dict]:
    out: list[dict] = []
    for s in (sources or [])[:MAX_CITATIONS]:
        if isinstance(s, str):
            out.append({"source_name": s})
        elif isinstance(s, dict):
            entry = {
                "source_id": s.get("source_id") or s.get("sourceId"),
                "source_name": s.get("source_name") or s.get("sourceName")
                or s.get("title") or s.get("url"),
            }
            if s.get("url"):
                entry["url"] = s.get("url")
            if s.get("rows") is not None:
                entry["rows"] = s.get("rows")
            if entry.get("source_name") or entry.get("source_id"):
                out.append(entry)
    return out


def build_trust_panel(
    sources=None,
    confidence: float | None = None,
    freshness: dict | None = None,
    claims_verified: bool | None = None,
) -> dict:
    """Assemble the contract. Never raises — a trust panel must not be
    able to break an answer."""
    try:
        citations = _normalize_citations(sources)
        conf = None
        try:
            if confidence is not None:
                conf = round(float(confidence), 3)
        except (TypeError, ValueError):
            conf = None
        grade = "verified" if claims_verified is True else (
            "unsourced" if not citations else "sourced")
        return {
            "citations": citations,
            "confidence": conf,
            "data_freshness": freshness or None,
            "grade": grade,
        }
    except Exception as exc:  # noqa: BLE001 — never break an answer
        logger.warning("trust_panel build failed (non-fatal): %s", exc)
        return {"citations": [], "confidence": None,
                "data_freshness": None, "grade": "unsourced"}


def trust_panel_for_message(assistant_msg: dict | None) -> dict:
    """Build the panel from a persisted assistant message dict: its
    ``sources`` (citations extracted from tool calls) and any confidence
    recorded on the message."""
    msg = assistant_msg or {}
    verified = None
    trace = msg.get("trace") or []
    if isinstance(trace, list):
        verified = any(
            isinstance(step, dict) and step.get("claims_verified") is True
            for step in trace
        ) or None
    return build_trust_panel(
        sources=msg.get("sources"),
        confidence=msg.get("confidence") or msg.get("confidence_score"),
        claims_verified=verified,
    )


def freshness_for_kb_tables(db, kb_id: str, tables: list[str]) -> dict:
    """Data-freshness block for a tenant KB's tables (uses the knowledge
    graph freshness service with its TTL cache). Best-effort."""
    try:
        from app.services.knowledge_graph.freshness import (
            annotate_tables,
            stale_flag,
        )

        out = {}
        for t in annotate_tables(db, kb_id, [{"name": n} for n in tables]):
            out[t.get("name")] = {
                "last_data_date": t.get("last_data_date"),
                "stale_days": t.get("stale_days"),
                "flag": stale_flag(t),
            }
        return out
    except Exception as exc:  # noqa: BLE001 — advisory data only
        logger.warning("trust_panel freshness failed (non-fatal): %s", exc)
        return {}


__all__ = [
    "build_trust_panel",
    "trust_panel_for_message",
    "freshness_for_kb_tables",
]
