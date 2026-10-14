"""Tenant-aware domain config tests (tenancy T2, 2026-10-13).

Precedence: agent config → org config (domain_configs/orgs/<org>.json) →
default.json ONLY for the default-org deployment → {}.
default.json is no longer an implicit global fallback for other tenants.
"""
import json
from pathlib import Path

import pytest

from app.services import domain_config as dc


@pytest.fixture(autouse=True)
def _isolate_cfg_dir(tmp_path, monkeypatch):
    """Point the config loader at a temp dir and clear caches."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("ZHL_DOMAIN_CONFIG_DIR", str(cfg_dir))
    dc.clear_cache()
    yield cfg_dir
    dc.clear_cache()


def _write(cfg_dir, name, data):
    """Write into the ACTIVE loader dir (the fixture's ZHL_DOMAIN_CONFIG_DIR)."""
    import os

    active = Path(os.environ["ZHL_DOMAIN_CONFIG_DIR"].split(",")[0])
    active.mkdir(parents=True, exist_ok=True)
    (active / f"{name}.json").write_text(json.dumps(data))


def test_default_org_resolves_default_json(tmp_path):
    _write(tmp_path, "default", {"data_agent_hint": "escisco hint"})
    cfg = dc.get_domain_config_for_org("default-org", None)
    assert cfg["data_agent_hint"] == "escisco hint"


def test_other_org_gets_generic_when_no_org_config(tmp_path):
    """THE fix: default.json is no longer an implicit global fallback."""
    _write(tmp_path, "default", {"data_agent_hint": "escisco hint"})
    cfg = dc.get_domain_config_for_org("org-beta", None)
    assert cfg == {}


def _write_orgs(org_id, data):
    """Write an org config into the ACTIVE loader dir's orgs/ subdir
    (sanitized like the loader does)."""
    import os
    import re

    active = Path(os.environ["ZHL_DOMAIN_CONFIG_DIR"].split(",")[0])
    orgs_dir = active / "orgs"
    orgs_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9]+", "_", (org_id or "").lower()).strip("_")
    (orgs_dir / f"{safe}.json").write_text(json.dumps(data))


def test_org_config_wins_for_other_org(tmp_path):
    _write(tmp_path, "default", {"data_agent_hint": "escisco hint"})
    _write_orgs("org-beta", {"data_agent_hint": "beta hint"})
    cfg = dc.get_domain_config_for_org("org-beta", None)
    assert cfg["data_agent_hint"] == "beta hint"


def test_agent_config_beats_org_config(tmp_path):
    _write(tmp_path, "default", {"data_agent_hint": "escisco hint"})
    _write(tmp_path, "sales_agent", {"data_agent_hint": "agent hint"})
    cfg = dc.get_domain_config_for_org("default-org", "sales_agent")
    assert cfg["data_agent_hint"] == "agent hint"


def test_default_org_with_own_org_config_prefers_org(tmp_path):
    """Explicit org config beats the implicit default for default-org too."""
    _write_orgs("default-org", {"data_agent_hint": "explicit"})
    _write(tmp_path, "default", {"data_agent_hint": "implicit"})
    cfg = dc.get_domain_config_for_org("default-org", None)
    assert cfg["data_agent_hint"] == "explicit"


def test_system_prompt_uses_org_scoped_config(tmp_path):
    """The agent_prompts consumer resolves per org (no cross-tenant leak)."""
    _write(tmp_path, "default", {
        "agent_prompt_overrides": {"schema_hint": "ESCISCO-SCHEMA-HINT"},
    })

    from types import SimpleNamespace

    from app.services.agent_prompts import get_system_prompt

    # A user agent from org-beta: must NOT receive the default-org hint.
    app_beta = SimpleNamespace(org_id="org-beta", name="Sales Agent",
                               agent_name="sales_agent")
    prompt = get_system_prompt("sales_agent", agent_app=app_beta)
    assert "ESCISCO-SCHEMA-HINT" not in prompt

    # A user agent from default-org: receives it.
    app_default = SimpleNamespace(org_id="default-org", name="Sales Agent",
                                  agent_name="sales_agent")
    prompt2 = get_system_prompt("sales_agent", agent_app=app_default)
    assert "ESCISCO-SCHEMA-HINT" in prompt2
