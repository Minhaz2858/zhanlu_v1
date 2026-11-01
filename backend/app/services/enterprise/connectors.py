"""Enterprise connector capability registry (Phase 5D).

ONE authoritative, EXPLICIT capability map for every onboardable resource
type. The onboarding pipeline (``app/services/enterprise/onboarding.py``)
consults this registry instead of hardcoding type lists, so a connector's
limits are declared once and reported honestly everywhere.

Closed capability vocabulary (unsupported ⇒ explicit unsupported result,
NEVER a silent success):
- ``inventory``          — the type can be listed/verified in the org inventory
- ``catalog``            — schema/catalog rows exist for the type (KBTableMeta)
- ``metadata``           — the connector exposes resource metadata (counts, status)
- ``evidence``           — the discovery engine can extract ProcessEvidence from it
- ``semantic_discovery`` — the discovery engine can propose ontology mappings
- ``drift``              — a deterministic inventory digest can be computed
- ``deletion_cleanup``   — ``enterprise.cleanup.handle_resource_deleted`` supports it

HONESTY: mcp_server and automation resources are inventory-only today —
their integrations provide NO extractable evidence and NO semantic
discovery. The registry says so explicitly. Never widen a capability here
without the actual integration existing.

PRIVACY: adapters touch ONLY identity/org metadata (id, name, status).
Credentials (KnowledgeBase.password, McpServer.oauth_token_json, ...) are
NEVER read by onboarding and NEVER appear in run steps or audit detail.
"""

from __future__ import annotations

# Closed capability vocabulary.
CAPABILITIES = (
    "inventory",
    "catalog",
    "metadata",
    "evidence",
    "semantic_discovery",
    "drift",
    "deletion_cleanup",
)


class UnsupportedCapabilityError(Exception):
    """Raised when an operation is not supported by a connector's declared
    capabilities — the explicit 'unsupported' result (never silent)."""


#: resource_type → declared capabilities + existence-verification adapter
#: name. ``_EVIDENCE`` marks the types the discovery engine actually
#: supports end-to-end today (datasource/knowledge_base ONLY).
ConnectorRegistry: dict[str, dict] = {
    "datasource": {
        "capabilities": frozenset(CAPABILITIES),
        "existence_model": "knowledge_base",  # a datasource IS a database KB
    },
    "knowledge_base": {
        "capabilities": frozenset(CAPABILITIES),
        "existence_model": "knowledge_base",
    },
    "mcp_server": {
        "capabilities": frozenset({"inventory", "deletion_cleanup"}),
        "existence_model": "mcp_server",
    },
    "automation": {
        "capabilities": frozenset({"inventory"}),
        "existence_model": "automation_task",
    },
}


def get_connector(resource_type: str) -> dict | None:
    """Registry entry for a type, or None when the type is not onboardable
    (unknown connectors are rejected, never invented)."""
    return ConnectorRegistry.get(resource_type)


def capabilities_for(resource_type: str) -> frozenset:
    """Declared capabilities for a type; empty set for unknown types."""
    entry = ConnectorRegistry.get(resource_type)
    return entry["capabilities"] if entry else frozenset()


def has_capability(resource_type: str, capability: str) -> bool:
    return capability in capabilities_for(resource_type)


def assert_capability(resource_type: str, capability: str) -> None:
    """Explicit unsupported check — raises (naming BOTH the type and the
    missing capability) instead of returning a silent falsy success."""
    if resource_type not in ConnectorRegistry:
        raise UnsupportedCapabilityError(
            f"unknown resource type {resource_type!r}: no connector "
            "registered")
    if capability not in capabilities_for(resource_type):
        raise UnsupportedCapabilityError(
            f"connector {resource_type!r} does not support "
            f"{capability!r} — unsupported operations are reported, "
            "never faked")


def existence_model(resource_type: str) -> str | None:
    """The org-scoped model a resource's existence is verified against
    (None = no verifiable row — the type cannot be existence-checked)."""
    entry = ConnectorRegistry.get(resource_type)
    return entry.get("existence_model") if entry else None
