"""Authorization provider ABC + core types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class ResourceType(Enum):
    """Types of resources that RBAC can govern."""
    TOOL = "tool"
    MODEL = "model"
    SKILL = "skill"
    SANDBOX = "sandbox"
    MCP_SERVER = "mcp_server"
    ROUTE = "route"


@dataclass
class AuthorizationDecision:
    """Result of an authorization check."""
    allowed: bool
    reason: str = ""
    denied_resources: list[str] = field(default_factory=list)


class AuthorizationProvider(ABC):
    """Abstract base for authorization providers. Fail-closed."""

    @abstractmethod
    def can_access(self, *, role, resource_type, resource_id, action="use"):
        ...

    @abstractmethod
    def filter_resources(self, *, role, resource_type, resource_ids):
        ...
