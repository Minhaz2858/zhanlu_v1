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

import contextvars
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Tenant override for the legacy ``get_domain_config`` callers. Set by
#: long-lived non-request entry points that KNOW the tenant (automation
#: executor → task.org_id). When set to a non-default org, the legacy
#: getter resolves tenant-aware instead of falling back to default.json —
#: threading org through all ~29 consumers without touching them.
#: Request paths that don't set it keep legacy behavior (fine for the
#: default-org deployment; per-request org threading rides on RLS work).
_ACTIVE_ORG: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "domain_config_active_org", default=None
)


def set_domain_config_org(org_id: str | None) -> None:
    """Scope subsequent legacy get_domain_config reads to this tenant."""
    _ACTIVE_ORG.set((org_id or "").strip() or None)

# Default search dir. Override with env ZHL_DOMAIN_CONFIG_DIR (comma-separated
# for multiple dirs; first hit wins per agent name).
_DEFAULT_DIRS = [
    Path(__file__).resolve().parent.parent / "domain_configs",
]

# Config base name loaded when no agent name is supplied. The seeding /
# bootstrap callers (``seed_forecast_targets(db)``, ``discover_and_seed_sku_targets(db)``)
# always pass an empty agent name, so an empty name means "the app's default
# domain config". Absent that file, behaviour is unchanged (empty = generic).
_DEFAULT_CONFIG_BASENAME = "default"

# normalize agent name -> config file base name (lowercase, non-alnum -> _)
def _safe_name(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def _config_dirs() -> list[Path]:
    raw = os.environ.get("ZHL_DOMAIN_CONFIG_DIR", "")
    dirs = [Path(p.strip()) for p in raw.split(",") if p.strip()]
    return dirs + list(_DEFAULT_DIRS)


@lru_cache(maxsize=256)
def _load_file_for(agent_name: str) -> dict[str, Any]:
    """Load the domain config JSON (cached, fail-soft).

    ``agent_name`` may carry one subdirectory level (``orgs/<org>``) —
    each path segment is sanitized separately so the separator survives.
    """
    parts = [_safe_name(seg) for seg in str(agent_name).split("/")]
    if any(not p for p in parts) or len(parts) > 2:
        return {}
    base = "/".join(parts)
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


def get_domain_config(agent_name: str | None) -> dict[str, Any]:
    """Return the (cached) domain config dict for an agent.

    An empty/absent agent name resolves the app's DEFAULT config
    (``domain_configs/default.json``); when that file is absent the result is
    ``{}`` — i.e. the platform behaves as a generic enterprise system.

    Multi-tenant note (2026-10-13, updated 2026-10-14 T3): the ~29 legacy
    callers keep this entry point, and it now honors the tenant override
    set by non-request entry points (automation executor → task.org_id):
    when an active org is set, reads resolve tenant-aware. Request paths
    that set no org keep the deployment-default behavior. Tenant-aware
    call sites may still use :func:`get_domain_config_for_org` directly.
    """
    return _resolve_legacy(agent_name)


def _resolve_legacy(agent_name: str | None) -> dict[str, Any]:
    """Legacy resolution with the tenant override applied (T3)."""
    active_org = _ACTIVE_ORG.get()
    if active_org and active_org != "default-org":
        return get_domain_config_for_org(active_org, agent_name)
    if not agent_name:
        return _load_file_for(_DEFAULT_CONFIG_BASENAME)
    return _load_file_for(agent_name)


@lru_cache(maxsize=256)
def get_domain_config_for_org(org_id: str | None, agent_name: str | None) -> dict[str, Any]:
    """Tenant-aware domain config (tenancy T2, 2026-10-13).

    Precedence:
      1. explicit agent config — ``<agent_name>.json``
      2. ORG config — ``domain_configs/orgs/<org_id>.json``
      3. the deployment default — ``default.json`` — ONLY for the
         default-org deployment itself
      4. ``{}`` — fully generic behavior

    The old behavior (default.json as an implicit global fallback for ANY
    tenant) is gone: a new org gets a generic, schema-agnostic platform
    unless it ships its own config — the coupling flagged in the tenancy
    audit. orgs/ directory config wins over default.json even for
    default-org when both exist (explicit beats implicit).
    """
    if agent_name:
        cfg = _load_file_for(agent_name)
        if cfg:
            return cfg

    if org_id:
        cfg = _load_file_for(f"orgs/{_safe_name(org_id)}")
        if cfg:
            return cfg

    if not org_id or org_id == "default-org":
        return _load_file_for(_DEFAULT_CONFIG_BASENAME)
    return {}


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
    _load_file_for.cache_clear()
    get_domain_config_for_org.cache_clear()
