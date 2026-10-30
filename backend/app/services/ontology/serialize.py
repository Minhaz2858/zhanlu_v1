"""Ontology serializers (P0-2)."""

from __future__ import annotations

from app.models.ontology import (
    AgentDomainBinding,
    OntologyAction,
    OntologyDomain,
    OntologyLink,
    OntologyObject,
)


def domain_to_dict(d: OntologyDomain) -> dict:
    return {
        "id": d.id, "code": d.code, "name": d.name,
        "description": d.description, "sort_order": d.sort_order,
    }


def object_to_dict(o: OntologyObject) -> dict:
    return {
        "id": o.id, "domain_id": o.domain_id, "name": o.name,
        "display_name": o.display_name, "description": o.description,
        "backing_kind": o.backing_kind, "backing_ref": o.backing_ref,
        "properties": o.properties_json or [], "is_active": o.is_active,
        # P1 evidence taxonomy (canonical statuses: evidence.py)
        "evidence_status": o.evidence_status,
        "confidence": o.confidence,
        "provenance": o.provenance_json,
    }


def link_to_dict(l: OntologyLink) -> dict:
    return {
        "id": l.id, "from_object_id": l.from_object_id,
        "to_object_id": l.to_object_id, "link_type": l.link_type,
        "cardinality": l.cardinality, "description": l.description,
        "evidence_status": l.evidence_status,
        "confidence": l.confidence,
        "provenance": l.provenance_json,
    }


def action_to_dict(a: OntologyAction) -> dict:
    return {
        "id": a.id, "object_id": a.object_id, "name": a.name,
        "description": a.description, "tool_name": a.tool_name,
        "risk_level": a.risk_level,
        "requires_confirmation": a.requires_confirmation,
        "params": a.params_json or {}, "is_active": a.is_active,
        "evidence_status": a.evidence_status,
        "confidence": a.confidence,
        "provenance": a.provenance_json,
    }


def binding_to_dict(b: AgentDomainBinding) -> dict:
    return {
        "id": b.id, "agent_name": b.agent_name, "domain_id": b.domain_id,
        "permission_scope": b.permission_scope,
    }
