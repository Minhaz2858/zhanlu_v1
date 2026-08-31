"""Tool handlers package — imports register all tools in the registry.

Importing this package (or just the modules) registers each tool in
the ToolRegistry singleton. The registration happens at module import
time, so adding `import app.services.tool_handlers` anywhere in the
startup path makes all tools available.

Phase 1 (hermes port) adds:
  - infrastructure helpers (budget_config, tool_result_storage, ...)
  - admin tools (update_env_config, docker_compose_restart)
  - The hermes adapter / missing-config helpers (private modules)

Phase 2 (quick wins) adds:
  - url_safety, fuzzy_match, path_security, patch_parser
  - process_registry (list/tail/kill)
  - env_passthrough, credential_files, kanban, cronjob
  - clarify, slash_confirm, skill_provenance, skill_usage
  - interrupt, checkpoint_manager, session_search, approval
  - osv_check, tirith_security, mixture_of_agents

Existing CRUD tools are NOT registered here — they live in
agent_tools._CRUD_DISPATCH and agent_prompts.py static lists.
"""

import logging
logger = logging.getLogger(__name__)

# Helpers / infra (no registry.register calls, but exposed for reuse)
# (budget_config, tool_result_storage, tool_output_limits, schema_sanitizer,
#  ansi_strip, tool_backend_helpers, binary_extensions, debug_helpers,
#  lazy_deps) are imported by other tool modules, not by the registry.

# Private adapter (hermes-style handler -> zhanlu-style handler)
from app.services.tool_handlers import _hermes_adapter  # noqa: F401
from app.services.tool_handlers import _missing_config  # noqa: F401

# Existing tool modules — trigger registration of web_search, memory, etc.
from app.services.tool_handlers import web_search_tool  # noqa: F401
from app.services.tool_handlers import web_extract_tool  # noqa: F401
from app.services.tool_handlers import memory_tool  # noqa: F401
from app.services.tool_handlers import todo_tool  # noqa: F401
from app.services.tool_handlers import file_tools  # noqa: F401
from app.services.tool_handlers import image_generation_tool  # noqa: F401
from app.services.tool_handlers import code_execution_tool  # noqa: F401
from app.services.tool_handlers import delegate_tool  # noqa: F401
from app.services.tool_handlers import db_tools  # noqa: F401
from app.services.tool_handlers import delegation_tools  # noqa: F401
from app.services.tool_handlers import enterprise_data_tools  # noqa: F401  # Phase 1C: collect_enterprise_data
from app.services.tool_handlers import sandbox_tool  # noqa: F401
from app.services.tool_handlers import sandbox_code  # noqa: F401

# Phase 1: admin tools (env config + docker restart)
from app.services.tool_handlers import env_config_tool  # noqa: F401

# Phase 2: pure-utility tools (no external deps)
from app.services.tool_handlers import url_safety_tool  # noqa: F401
from app.services.tool_handlers import fuzzy_match_tool  # noqa: F401
from app.services.tool_handlers import path_security_tool  # noqa: F401
from app.services.tool_handlers import patch_parser_tool  # noqa: F401
from app.services.tool_handlers import process_registry_tool  # noqa: F401
from app.services.tool_handlers import env_passthrough_tool  # noqa: F401
from app.services.tool_handlers import credential_files_tool  # noqa: F401
from app.services.tool_handlers import kanban_tool  # noqa: F401
from app.services.tool_handlers import cronjob_tool  # noqa: F401
from app.services.tool_handlers import clarify_tool  # noqa: F401
from app.services.tool_handlers import slash_confirm_tool  # noqa: F401
from app.services.tool_handlers import skill_provenance_tool  # noqa: F401
from app.services.tool_handlers import skill_usage_tool  # noqa: F401
from app.services.tool_handlers import checkpoint_manager_tool  # noqa: F401
from app.services.tool_handlers import session_search_tool  # noqa: F401
from app.services.tool_handlers import interrupt_tool  # noqa: F401
from app.services.tool_handlers import osv_check_tool  # noqa: F401
from app.services.tool_handlers import tirith_security_tool  # noqa: F401
from app.services.tool_handlers import approval_tool  # noqa: F401
from app.services.tool_handlers import mixture_of_agents_tool  # noqa: F401

# Phase 3: skills platform
from app.services.tool_handlers import skills_tool  # noqa: F401
from app.services.tool_handlers import skills_hub_tool  # noqa: F401
from app.services.tool_handlers import skills_sync_tool  # noqa: F401
from app.services.tool_handlers import skills_guard_tool  # noqa: F401
from app.services.tool_handlers import skill_manager_tool  # noqa: F401

# FIX 2026-08-29: load_skill_body was never imported here — the module
# registers at import time, so the tool silently did NOT exist in the
# running backend while the system prompt instructed agents to call it
# ("Unknown tool: load_skill_body" in real chat traces). Importing it
# registers the tool AND lets system_agents._tools_in_registry keep it
# in general_assistant's enabled_tools.
from app.services.tool_handlers import load_skill_body_tool  # noqa: F401

# FIX 2026-08-29: the `Skill` meta-tool was only registered in main.py
# lifespan — AFTER system_agents built its tool_configs at import time,
# so `_tools_in_registry` silently dropped it from enabled_tools.
# Register it here at import time (idempotent: registry.register
# overwrites the same name). Wrapped in try/except so a circular-import
# edge cannot break tool registration; lifespan registration remains as
# a backstop.
try:
    from app.services.skill_routing.meta_tool import register_skill_meta_tool
    register_skill_meta_tool()
except Exception:  # pragma: no cover - defensive
    pass

# Phase 4: LLM/auxiliary
from app.services.tool_handlers import openrouter_client_tool  # noqa: F401
from app.services.tool_handlers import xai_http_tool  # noqa: F401
from app.services.tool_handlers import x_search_tool  # noqa: F401

# Phase 5: media + artifacts
from app.services.tool_handlers import tts_tool  # noqa: F401
from app.services.tool_handlers import artifact_tool  # noqa: F401
from app.services.tool_handlers import deck_edit_tool  # noqa: F401
from app.services.tool_handlers import video_generation_tool  # noqa: F401
from app.services.tool_handlers import transcription_tool  # noqa: F401
from app.services.tool_handlers import vision_tool  # noqa: F401
from app.services.tool_handlers import voice_mode_tool  # noqa: F401

# Phase 6: browser (Playwright replaced by agent-browser CLI wrapper)
from app.services.tool_handlers import agent_browser_tool  # noqa: F401
from app.services.tool_handlers import computer_use_tool  # noqa: F401

# Phase 7: communication
from app.services.tool_handlers import discord_tool  # noqa: F401
from app.services.tool_handlers import feishu_doc_tool  # noqa: F401
from app.services.tool_handlers import feishu_drive_tool  # noqa: F401
from app.services.tool_handlers import send_message_tool  # noqa: F401
from app.services.tool_handlers import yuanbao_tool  # noqa: F401
from app.services.tool_handlers import homeassistant_tool  # noqa: F401
from app.services.tool_handlers import microsoft_graph_tool  # noqa: F401
from app.services.tool_handlers import microsoft_graph_auth_tool  # noqa: F401

# Phase 8: MCP
from app.services.tool_handlers import mcp_tool  # noqa: F401
from app.services.tool_handlers import mcp_oauth_tool  # noqa: F401
from app.services.tool_handlers import mcp_oauth_manager_tool  # noqa: F401

# Phase 9: multi-agent swarm tools
# swarm_tools.py self-registers all 5 swarm_* tools via
# `register_swarm_tools()` at module bottom; a bare import suffices.
# Guarded so a failure here cannot break tool-registry startup.
try:
    from app.services.tool_handlers import swarm_tools  # noqa: F401
except Exception as _swarm_err:  # pragma: no cover - defensive
    logger.warning("swarm_tools registration failed (non-fatal): %s", _swarm_err)

# Phase 10: design intelligence (ui-ux-pro-max)
# Wraps the upstream BM25+regex search CLI shipped with the skill package.
# Tool is gated by check_fn so missing CLI files never break startup.
try:
    from app.services.tool_handlers import ui_ux_pro_max_tool  # noqa: F401
except Exception as _uiux_err:  # pragma: no cover - defensive
    logger.warning("ui_ux_pro_max_tool registration failed (non-fatal): %s", _uiux_err)

# P3-bis: automation_chat_tool registers the ``execute_automation`` tool
# at import time. Without this, /automation's "Run Now" handoff to the
# chat LLM fails with "I don't have execute_automation" because the
# tool was never added to the registry. Note: this lives in
# ``app.services`` (not ``app.services.tool_handlers``) because it
# shares a lot of state with the automation dispatcher, so we just
# import the module to trigger its module-level ``_register()`` call.
try:
    from app.services import automation_chat_tool  # noqa: F401
except Exception as _ac_err:  # pragma: no cover - defensive
    logger.warning("automation_chat_tool registration failed (non-fatal): %s", _ac_err)

# P2 (2026-08-29): browser toolset — Playwright-backed web automation
# (browser_navigate/click/type/snapshot/screenshot) with scheme + domain
# allowlist guardrails. check_fn gates on playwright availability, so
# environments without playwright start up cleanly.
try:
    from app.services.tool_handlers import browser_tools  # noqa: F401
except Exception as _bt_err:  # pragma: no cover - defensive
    logger.warning("browser_tools registration failed (non-fatal): %s", _bt_err)

# Phase 11: live dashboards (create_dashboard tool)
try:
    from app.services.tool_handlers import dashboard_tools  # noqa: F401
except Exception as _dash_err:  # pragma: no cover - defensive
    logger.warning("dashboard_tools registration failed (non-fatal): %s", _dash_err)

# Phase 12: forecasting engine (forecast_discover / run / get / accuracy / rules)
# Heavy tools (discover, run) are gated via enabled_by_default=False.
# Lightweight reads (get, accuracy) and rules CRUD are user-facing.
# Guarded: missing xgboost/statsmodels should not break tool-registry startup.
try:
    from app.services.tool_handlers import forecast_tool  # noqa: F401
except Exception as _fc_err:  # pragma: no cover - defensive
    logger.warning("forecast_tool registration failed (non-fatal): %s", _fc_err)

# Universal Analytics Engine (P2): 6 tools with enabled_by_default=True.
# Guarded: missing pandas/statsmodels deps must not break tool-registry startup.
try:
    from app.services.universal_analytics import tools  # noqa: F401
except Exception as _ua_err:  # pragma: no cover - defensive
    logger.warning(
        "universal_analytics registration failed (non-fatal): %s", _ua_err
    )

# CAD: Fusion 360 bridge (fusion360_execute_python + fusion360_ping + the
# granular modeling/IO tools). Talks to the local FusionMCP add-in over TCP
# (host.docker.internal:9876 — see fusion360_tool.py) — no external deps.
try:
    from app.services.tool_handlers import fusion360_tool  # noqa: F401
    from app.services.tool_handlers import fusion360_granular  # noqa: F401
    from app.services.tool_handlers import fusion360_advanced  # noqa: F401
    from app.services.tool_handlers import fusion360_io  # noqa: F401
    from app.services.tool_handlers import fusion360_features  # noqa: F401
    from app.services.tool_handlers import fusion360_probe  # noqa: F401
    from app.services.tool_handlers import fusion360_sketch2  # noqa: F401
    from app.services.tool_handlers import fusion360_assembly  # noqa: F401
    from app.services.tool_handlers import fusion360_parametric  # noqa: F401
except Exception as _f360_err:  # pragma: no cover - defensive
    logger.warning("fusion360_tool registration failed (non-fatal): %s", _f360_err)
