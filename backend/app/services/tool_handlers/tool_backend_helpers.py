"""Tool backend helpers — small utilities used across multiple tool modules.

In hermes this houses things like ``managed_nous_tools_enabled`` and
``prefers_gateway``. In zhanlu we keep the same module shape but with
zhanlu-native helpers: feature-flag checks, vendor routing decisions, and
shared client construction.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def is_truthy_value(value: Any) -> bool:
    """Best-effort truthy parse for env vars / config strings.

    Returns True for: "1", "true", "yes", "on" (case-insensitive).
    Returns False for everything else, including None and empty strings.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_env(name: str, default: str = "") -> str:
    """Read an env var with a default. Logs when overriding a non-empty default."""
    val = os.environ.get(name, default)
    return val or default


def prefers_gateway(vendor: str) -> bool:
    """Whether requests to a vendor should be routed through the zhanlu gateway.

    Currently a stub: returns False for all vendors. Future: read
    ``settings.VENDOR_GATEWAY_ROUTING`` and route via a managed gateway
    service for vendors that have a managed proxy (e.g. Firecrawl on
    certain plans).
    """
    return False


def managed_vendor_enabled(vendor: str) -> bool:
    """Whether a managed-vendor integration is enabled.

    Stub — returns False. Placeholder for the future zhanlu vendor gateway.
    """
    return False


def get_vendor_api_key(vendor: str) -> Optional[str]:
    """Read a vendor's API key from env.

    Convention: ``<VENDOR>_API_KEY`` in upper-case. Returns None when unset.
    """
    return os.environ.get(f"{vendor.upper()}_API_KEY") or None
