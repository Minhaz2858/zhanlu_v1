"""Shared evidence exchange for intelligent agent collaboration (P2 Phase 2, Part 3).

Agents collaborate by REFERENCE, not by copying datasets. A specialist that
produces a numeric finding (from a query, a metric, a snapshot) registers an
``EvidenceItem`` once and passes its ``evidence_id`` to peers. Peers fetch the
*reference* (provenance + compact findings + validation status) — never the raw
rows — so a large dataset is materialised in exactly one place.

The store reuses the same Redis-or-in-process fallback as ``governance_store``:
if Redis is reachable AND authenticated it backs the evidence registry (so
evidence survives across workers in one conversation window); otherwise it
degrades to an in-process dict (dev / tests). Either way the contract — store by
id, fetch by id, search by topic — is identical.

Honesty rule (enforced by callers, not here): an agent's unsupported textual
statement must NEVER become verified evidence merely because another agent
repeats it. Evidence is only ever created by the agent that *produced* it
(``provide_evidence``), and its ``validation_status`` reflects what that producer
actually did — not a peer's endorsement.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.database import get_redis

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 3600  # evidence lives for the duration of a conversation window

# validation_status vocabulary (mirrors ontology/evidence.py where present)
VALIDATION_VALIDATED = "validated"
VALIDATION_UNVALIDATED = "unvalidated"
VALIDATION_CONTRADICTED = "contradicted"
VALIDATION_PENDING = "pending"
_VALID_STATUSES = {
    VALIDATION_VALIDATED,
    VALIDATION_UNVALIDATED,
    VALIDATION_CONTRADICTED,
    VALIDATION_PENDING,
}


@dataclass
class EvidenceItem:
    """A single, referenceable piece of collaborative evidence.

    Carries everything a reviewer or a peer needs to *trust* a finding without
    re-running the underlying query:
      * where it came from (source / datasource / query provenance / metric defs)
      * what the numbers actually were (compact ``findings`` — not raw rows)
      * how fresh / how certain it is (freshness, assumptions, limitations)
      * whether it has been checked (validation_status)
      * what other evidence it builds on (references)
    """

    evidence_id: str
    producer_agent: str
    source_id: Optional[str] = None          # datasource / KB id
    datasource_id: Optional[str] = None      # explicit datasource identifier
    query_provenance: dict = field(default_factory=dict)  # {sql, execution_id, nl, rows}
    metric_definition_ids: list = field(default_factory=list)
    findings: Any = None                     # compact numeric/structured result
    freshness: dict = field(default_factory=dict)  # {as_of, state}
    assumptions: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    validation_status: str = VALIDATION_UNVALIDATED
    references: list = field(default_factory=list)   # evidence_ids this depends on
    created_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "producer_agent": self.producer_agent,
            "source_id": self.source_id,
            "datasource_id": self.datasource_id,
            "query_provenance": self.query_provenance,
            "metric_definition_ids": self.metric_definition_ids,
            "findings": self.findings,
            "freshness": self.freshness,
            "assumptions": self.assumptions,
            "limitations": self.limitations,
            "validation_status": self.validation_status,
            "references": self.references,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceItem":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def reference(self) -> dict:
        """Compact reference a peer receives — NO raw query results."""
        return {
            "evidence_id": self.evidence_id,
            "producer_agent": self.producer_agent,
            "source_id": self.source_id,
            "datasource_id": self.datasource_id,
            "metric_definition_ids": self.metric_definition_ids,
            "findings_summary": _summarize(self.findings),
            "freshness": self.freshness,
            "validation_status": self.validation_status,
            "references": self.references,
        }


def _summarize(findings: Any, limit: int = 400) -> Any:
    """Keep references small — never ship full result sets between agents."""
    if findings is None:
        return None
    if isinstance(findings, str):
        return findings[:limit]
    try:
        s = json.dumps(findings, ensure_ascii=False, default=str)
        return s[:limit]
    except (TypeError, ValueError):
        return str(findings)[:limit]


class EvidenceRegistry:
    """Abstract registry. Concrete: ``_RedisStore`` / ``_InProcessStore``."""

    def store(self, conv_id: str, item: EvidenceItem) -> str:
        raise NotImplementedError

    def get(self, conv_id: str, evidence_id: str) -> Optional[EvidenceItem]:
        raise NotImplementedError

    def list(self, conv_id: str) -> list:
        raise NotImplementedError

    def list_items(self, conv_id: str) -> list:
        """Return EvidenceItem objects (not dicts) for fine-grained filtering."""
        raise NotImplementedError

    def find_by_topic(self, conv_id: str, topic: str) -> list:
        """Loose match over producer/source/findings text — for request_evidence."""
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class _InProcessStore(EvidenceRegistry):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict = {}  # conv_id -> {evidence_id: EvidenceItem}

    def store(self, conv_id: str, item: EvidenceItem) -> str:
        with self._lock:
            self._data.setdefault(conv_id, {})[item.evidence_id] = item
        return item.evidence_id

    def get(self, conv_id: str, evidence_id: str):
        with self._lock:
            return self._data.get(conv_id, {}).get(evidence_id)

    def list(self, conv_id: str) -> list:
        with self._lock:
            return [i.to_dict() for i in self._data.get(conv_id, {}).values()]

    def list_items(self, conv_id: str) -> list:
        with self._lock:
            return list(self._data.get(conv_id, {}).values())

    def find_by_topic(self, conv_id: str, topic: str) -> list:
        topic_l = (topic or "").lower()
        out = []
        with self._lock:
            for i in self._data.get(conv_id, {}).values():
                hay = " ".join([
                    i.producer_agent or "",
                    i.source_id or "",
                    i.datasource_id or "",
                    json.dumps(i.findings, ensure_ascii=False, default=str),
                    " ".join(i.assumptions),
                ]).lower()
                if topic_l in hay:
                    out.append(i.reference())
        return out

    def reset(self) -> None:
        with self._lock:
            self._data.clear()


class _RedisStore(EvidenceRegistry):
    def __init__(self, client) -> None:
        self._r = client
        self._lock = threading.Lock()

    def _key(self, conv_id: str) -> str:
        return f"collab:evidence:{conv_id}"

    def _idx_key(self, conv_id: str) -> str:
        return f"collab:evidence:idx:{conv_id}"

    def store(self, conv_id: str, item: EvidenceItem) -> str:
        try:
            self._r.hset(self._key(conv_id), item.evidence_id, json.dumps(item.to_dict(), default=str))
            self._r.expire(self._key(conv_id), DEFAULT_TTL_S)
            self._r.sadd(self._idx_key(conv_id), item.evidence_id)
            self._r.expire(self._idx_key(conv_id), DEFAULT_TTL_S)
            return item.evidence_id
        except Exception as e:  # pragma: no cover - network
            logger.warning("evidence: redis store failed, dropping (non-fatal): %s", e)
            return item.evidence_id

    def get(self, conv_id: str, evidence_id: str):
        try:
            raw = self._r.hget(self._key(conv_id), evidence_id)
            return EvidenceItem.from_dict(json.loads(raw)) if raw else None
        except Exception:  # pragma: no cover
            return None

    def list(self, conv_id: str) -> list:
        try:
            raws = self._r.hgetall(self._key(conv_id)) or {}
            return [EvidenceItem.from_dict(json.loads(v)).to_dict() for v in raws.values()]
        except Exception:  # pragma: no cover
            return []

    def list_items(self, conv_id: str) -> list:
        try:
            raws = self._r.hgetall(self._key(conv_id)) or {}
            return [EvidenceItem.from_dict(json.loads(v)) for v in raws.values()]
        except Exception:  # pragma: no cover
            return []

    def find_by_topic(self, conv_id: str, topic: str) -> list:
        topic_l = (topic or "").lower()
        try:
            raws = self._r.hgetall(self._key(conv_id)) or {}
        except Exception:  # pragma: no cover
            return []
        out = []
        for v in raws.values():
            try:
                item = EvidenceItem.from_dict(json.loads(v))
            except (json.JSONDecodeError, TypeError):
                continue
            hay = " ".join([
                item.producer_agent or "",
                item.source_id or "",
                item.datasource_id or "",
                json.dumps(item.findings, ensure_ascii=False, default=str),
                " ".join(item.assumptions),
            ]).lower()
            if topic_l in hay:
                out.append(item.reference())
        return out

    def reset(self) -> None:
        try:
            for k in self._r.scan_iter(match="collab:evidence:*"):
                self._r.delete(k)
        except Exception as e:  # pragma: no cover
            logger.warning("evidence: redis reset failed: %s", e)


_STORE: "EvidenceRegistry | None" = None
_STORE_LOCK = threading.Lock()


def get_registry() -> EvidenceRegistry:
    """Process-wide evidence registry; Redis-backed if reachable+authenticated."""
    global _STORE
    if _STORE is not None:
        return _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        client = None
        try:
            client = get_redis()
            if client is not None:
                try:
                    if not client.ping():
                        client = None
                except Exception as e:  # pragma: no cover - auth/connect error
                    logger.warning("evidence: redis reachable but unusable, in-process: %s", e)
                    client = None
        except Exception as e:  # pragma: no cover
            logger.warning("evidence: redis unavailable, in-process fallback: %s", e)
            client = None
        _STORE = _RedisStore(client) if client else _InProcessStore()
        return _STORE


def reset_registry() -> None:
    """Test helper — drop cached registry and clear its state."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            try:
                _STORE.reset()
            except Exception:
                pass
        _STORE = None


def provide_evidence(
    conv_id: str,
    producer_agent: str,
    *,
    source_id: Optional[str] = None,
    datasource_id: Optional[str] = None,
    query_provenance: Optional[dict] = None,
    metric_definition_ids: Optional[list] = None,
    findings: Any = None,
    freshness: Optional[dict] = None,
    assumptions: Optional[list] = None,
    limitations: Optional[list] = None,
    validation_status: str = VALIDATION_UNVALIDATED,
    references: Optional[list] = None,
) -> str:
    """Create one evidence item and return its id. Fails closed on bad status."""
    if validation_status not in _VALID_STATUSES:
        validation_status = VALIDATION_UNVALIDATED
    item = EvidenceItem(
        evidence_id=uuid.uuid4().hex,
        producer_agent=producer_agent,
        source_id=source_id,
        datasource_id=datasource_id,
        query_provenance=query_provenance or {},
        metric_definition_ids=metric_definition_ids or [],
        findings=findings,
        freshness=freshness or {},
        assumptions=assumptions or [],
        limitations=limitations or [],
        validation_status=validation_status,
        references=references or [],
    )
    return get_registry().store(conv_id, item)


def request_evidence_references(conv_id: str, topic: str) -> list:
    """Return compact REFERENCES for evidence matching ``topic`` (no raw data)."""
    if not conv_id:
        return []
    return get_registry().find_by_topic(conv_id, topic)


def get_evidence(conv_id: str, evidence_id: str):
    if not conv_id:
        return None
    return get_registry().get(conv_id, evidence_id)
