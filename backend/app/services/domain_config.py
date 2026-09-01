"""Per-app domain configuration (de-hardcoding layer).

The platform is industry-agnostic: backend/app code carries ZERO table names,
column names, product keys, or agent-specific hints (the "HOW not WHAT" rule).
All app-specific knowledge lives in per-app domain config files loaded here:

    backend/app/domain_configs/<agent_name>.json   (or .yaml)

Shape (all keys optional — empty config = fully generic behavior):

    {
      "schema_linker_allowlist_enabled": true,
      "schema_linker_table_allowlist": ["sales_orders", "contracts", ...],
      "static_routes": [
        {
          "patterns": ["contract performance", "合同"],
          "table": "contract_execution",
          "hint_columns": ["customer_name", "contract_quantity", ...],
          "fallback_tables": ["contracts"],
          "date_hint": "..."           # optional instruction block
        }
      ],
      "data_agent_hint": "...",        # extra sub-agent prompt block
      "product_keys": {...},           # product id -> labels for market dashboards
      "agent_prompt_overrides": {...}  # optional system-prompt fragments
    }

When no config exists for an agent, every getter returns its empty default —
the platform behaves as a generic enterprise system (pure schema discovery,
no pinned tables, no allowlist).
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default search dir. Override with env ZHL_DOMAIN_CONFIG_DIR (comma-separated
# for multiple dirs; first hit wins per agent name).
_DEFAULT_DIRS = [
    Path(__file__).resolve().parent.parent / "domain_configs",
]

# normalize agent name -> config file base name (lowercase, non-alnum -> _)
def _safe_name(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def _config_dirs() -> list[Path]:
    raw = os.environ.get("ZHL_DOMAIN_CONFIG_DIR", "")
    dirs = [Path(p.strip()) for p in raw.split(",") if p.strip()]
    return dirs + list(_DEFAULT_DIRS)


def _load_file_for(agent_name: str) -> dict[str, Any]:
    """Load the domain config JSON for an agent (cached, fail-soft)."""
    base = _safe_name(agent_name)
    if not base:
        return {}
    for d in _config_dirs():
        for ext in (".json", ".yaml", ".yml"):
            p = d / f"{base}{ext}"
            if not p.exists():
                continue
            try:
                if ext == ".json":
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                else:
                    import yaml  # optional dep
                    with open(p, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                if isinstance(data, dict):
                    logger.info("domain_config: loaded %s for agent %s", p, agent_name)
                    return data
            except Exception as e:  # noqa: BLE001
                logger.warning("domain_config: failed to load %s (non-fatal): %s", p, e)
    return {}


@lru_cache(maxsize=256)
def get_domain_config(agent_name: str | None) -> dict[str, Any]:
    """Return the (cached) domain config dict for an agent. Empty = generic."""
    if not agent_name:
        return {}
    return _load_file_for(agent_name)


def get_static_routes(agent_name: str | None = None) -> list[dict[str, Any]]:
    """Static query routes for this agent. Empty list = no pinning (generic)."""
    return list((get_domain_config(agent_name) or {}).get("static_routes") or [])


def get_schema_allowlist(agent_name: str | None = None) -> list[str] | None:
    """Table allowlist for this agent. None = no restriction (generic)."""
    cfg = get_domain_config(agent_name) or {}
    if not cfg.get("schema_linker_allowlist_enabled", False):
        return None
    allow = cfg.get("schema_linker_table_allowlist") or []
    return list(allow) if allow else None


def get_data_agent_hint(agent_name: str | None = None) -> str:
    """Extra sub-agent prompt block for this agent. '' = generic default."""
    return (get_domain_config(agent_name) or {}).get("data_agent_hint") or ""


def get_product_keys(agent_name: str | None = None) -> dict[str, Any]:
    """Product-key overrides for market dashboards. {} = generic."""
    return dict((get_domain_config(agent_name) or {}).get("product_keys") or {})


def clear_cache() -> None:
    get_domain_config.cache_clear()
