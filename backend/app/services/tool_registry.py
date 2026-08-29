"""Central tool registry — maps tool names to schemas, handlers, and metadata.

This replaces the hardcoded dispatch dict in agent_tools.py and the static
tool lists in agent_prompts.py. Each tool registers its OpenAI-format schema
and a handler function (sync or async). The registry is queried by:
  - get_tools(agent_app) → returns schemas for the agent's enabled tools
  - execute_tool(name, args, db, user_id) → dispatches to the right handler

Existing CRUD tools (create_agent, update_skill, etc.) are registered here
alongside new capability tools (web_search, memory, todo, etc.).

Extension notes (Phase 1 — hermes-style port):
  - Each ToolEntry now carries `toolset`, `check_fn`, `requires_env`,
    `is_async`, `max_result_size_chars`, `dynamic_schema_overrides`. These
    fields are read by the new helpers but existing call sites that only
    use `name`, `schema`, `handler`, `category` keep working unchanged.
  - The runtime defaults to "agent-handled missing config" — tools stay
    visible in the LLM's tool list even when their env/binary checks fail;
    the handler returns a structured `missing_config` response and the agent
    asks the user to provide the missing values.
  - `include_unavailable=True` in `get_definitions()` mirrors this default.
    Pass `include_unavailable=False` if a caller wants hard filtering.
"""

import asyncio
import importlib
import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ToolEntry:
    """A single tool registration record."""
    name: str
    schema: dict                          # OpenAI function-calling format
    handler: Callable                     # sync or async: (args, db, user_id, **ctx) -> dict
    category: str = "general"
    toolset: str = "general"              # Phase 1: hermes-style toolset grouping
    enabled_by_default: bool = True
    requires_config: list[str] = field(default_factory=list)  # config keys that must be non-empty (legacy)
    requires_env: list[str] = field(default_factory=list)      # Phase 1: env var names this tool needs
    is_async: bool = False                # Phase 1: explicit async flag (auto-detected if not set)
    check_fn: Optional[Callable[[], bool]] = None  # Phase 1: env-time availability check
    max_result_size_chars: Optional[int] = None    # Phase 1: per-tool output cap
    dynamic_schema_overrides: Optional[Callable[[], dict]] = None  # Phase 1: runtime schema overrides
    description: str = ""
    emoji: str = ""                       # Phase 1: UI display hint


class ToolRegistry:
    """Singleton registry for all agent tools."""

    _instance: "ToolRegistry | None" = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        # Phase 1: TTL-cached check_fn results. _check_fn_cached() uses this
        # so env-var probes don't repeat on every LLM turn.
        self._check_fn_cache: Dict[Callable, tuple[float, bool]] = {}
        self._check_fn_cache_lock = threading.Lock()
        self._lock = threading.RLock()
        # Phase 1: generation counter for downstream memoization.
        self._generation: int = 0
        # Phase 1: toolset registry — maps toolset name to its check function.
        self._toolset_checks: Dict[str, Callable[[], bool]] = {}

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        schema: dict,
        handler: Callable,
        category: str = "general",
        toolset: str | None = None,
        enabled_by_default: bool = True,
        requires_config: list[str] | None = None,
        requires_env: list[str] | None = None,
        is_async: bool | None = None,
        check_fn: Optional[Callable[[], bool]] = None,
        max_result_size_chars: int | None = None,
        dynamic_schema_overrides: Optional[Callable[[], dict]] = None,
        description: str = "",
        emoji: str = "",
    ) -> None:
        """Register a tool. Overwrites if name already exists."""
        # Auto-detect async if not explicitly set
        if is_async is None:
            is_async = asyncio.iscoroutinefunction(handler)
        entry = ToolEntry(
            name=name,
            schema=schema,
            handler=handler,
            category=category,
            toolset=toolset or category or "general",
            enabled_by_default=enabled_by_default,
            requires_config=requires_config or [],
            requires_env=requires_env or [],
            is_async=is_async,
            check_fn=check_fn,
            max_result_size_chars=max_result_size_chars,
            dynamic_schema_overrides=dynamic_schema_overrides,
            description=description or schema.get("function", {}).get("description", ""),
            emoji=emoji,
        )
        with self._lock:
            self._tools[name] = entry
            self._generation += 1
        # Bind toolset check (if any) — first-write-wins
        if check_fn is not None:
            self._toolset_checks.setdefault(entry.toolset, check_fn)
        logger.debug("Registered tool: %s (category=%s, toolset=%s)", name, category, entry.toolset)

    def deregister(self, name: str) -> None:
        """Remove a tool. Mainly for tests / dynamic reload."""
        with self._lock:
            entry = self._tools.pop(name, None)
            if entry is not None:
                self._generation += 1
        if entry is not None:
            logger.debug("Deregistered tool: %s", name)

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_schemas(self, tool_names: list[str], include_unavailable: bool = True) -> list[dict]:
        """Return OpenAI-format tool schemas for the given tool names.

        Skips tools that are not registered. When ``include_unavailable`` is
        True (default — the agent-handled credential flow), tools whose
        ``check_fn()`` returns False are still returned; their handlers are
        responsible for emitting a structured missing-config response.
        """
        # B1: RBAC filtering — strip denied tools before they reach the model
        try:
            from app.services.authz import get_authorizer
            from app.services.authz.base import ResourceType as _RT
            from app.services.tracing import TraceContext
            _role = TraceContext.current_role() or "user"
            tool_names = get_authorizer().filter_resources(
                role=_role, resource_type=_RT.TOOL, resource_ids=tool_names,
            )
        except Exception as _exc:
            logger.debug("RBAC filter skipped (non-fatal): %s", _exc)

        # Per-call cache on top of the TTL cache — saves redundant probes.
        check_results: Dict[Callable, bool] = {}
        with self._lock:
            entries_by_name = dict(self._tools)
        result: list[dict] = []
        for name in tool_names:
            entry = entries_by_name.get(name)
            if not entry:
                continue
            if not include_unavailable and entry.check_fn is not None:
                if entry.check_fn not in check_results:
                    check_results[entry.check_fn] = self._check_fn_cached(entry.check_fn)
                if not check_results[entry.check_fn]:
                    continue
            schema = {**entry.schema}
            if entry.dynamic_schema_overrides is not None:
                try:
                    overrides = entry.dynamic_schema_overrides()
                    if isinstance(overrides, dict):
                        schema.update(overrides)
                except Exception as exc:
                    logger.warning("dynamic_schema_overrides for %s raised %s; static schema", name, exc)
            # 2026-08-25: BUGFIX — OpenAI/DeepSeek API requires every tool
            # to have the structure {"type": "function", "function": {...}}.
            # Some tools (e.g. collect_enterprise_data) were registered with
            # just {"name", "description", "parameters"} and got sent to the
            # LLM as-is, causing a Pydantic validation error:
            #   1 validation error: tools.99.function - Field required
            # Auto-wrap any tool that's missing the function envelope.
            if "function" not in schema and "name" in schema:
                schema = {
                    "type": "function",
                    "function": {
                        "name": schema.get("name", name),
                        "description": schema.get("description", ""),
                        "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            result.append(schema)
        return result

    def get_handler(self, name: str) -> Callable | None:
        """Return the handler function for a tool, or None if not found."""
        with self._lock:
            entry = self._tools.get(name)
        return entry.handler if entry else None

    def get_entry(self, name: str) -> "ToolEntry | None":
        """Return the full ToolEntry for a tool (schema, category, flags)."""
        with self._lock:
            return self._tools.get(name)

    def is_async_handler(self, name: str) -> bool:
        """Check if a tool's handler is a coroutine function."""
        with self._lock:
            entry = self._tools.get(name)
        if not entry:
            return False
        return entry.is_async

    def list_available(self) -> list[str]:
        """List all registered tool names."""
        with self._lock:
            return list(self._tools.keys())

    def list_names(self) -> list[str]:
        """Alias for list_available() — kept for API compatibility."""
        return self.list_available()

    def list_by_category(self, category: str) -> list[str]:
        """List tool names in a given category."""
        with self._lock:
            return [name for name, entry in self._tools.items() if entry.category == category]

    def list_by_toolset(self, toolset: str) -> list[str]:
        """Phase 1: list tool names in a given toolset."""
        with self._lock:
            return sorted(name for name, entry in self._tools.items() if entry.toolset == toolset)

    def get_toolsets(self) -> dict[str, list[str]]:
        """Phase 1: return a snapshot of {toolset_name: [tool_names]}."""
        with self._lock:
            out: dict[str, list[str]] = {}
            for entry in self._tools.values():
                out.setdefault(entry.toolset, []).append(entry.name)
        return {ts: sorted(names) for ts, names in out.items()}

    def is_config_satisfied(self, name: str, config_values: dict[str, Any]) -> bool:
        """Check if a tool's config requirements are met.

        config_values maps config key → current value (from settings).
        """
        with self._lock:
            entry = self._tools.get(name)
        if not entry:
            return False
        for key in entry.requires_config:
            val = config_values.get(key, "")
            if not val:
                return False
        return True

    def get_entry(self, name: str) -> ToolEntry | None:
        """Return the full ToolEntry for a tool."""
        with self._lock:
            return self._tools.get(name)

    def all_entries(self) -> dict[str, ToolEntry]:
        """Return all registered entries (read-only view)."""
        with self._lock:
            return dict(self._tools)

    def get_max_result_size(self, name: str, default: int | None = None) -> int:
        """Phase 1: per-tool result-size cap, falling back to default."""
        with self._lock:
            entry = self._tools.get(name)
        if entry and entry.max_result_size_chars is not None:
            return entry.max_result_size_chars
        if default is not None:
            return default
        from app.services.tool_handlers.budget_config import DEFAULT_RESULT_SIZE_CHARS
        return DEFAULT_RESULT_SIZE_CHARS

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        """Phase 1: return the tool's emoji, or default if unset."""
        with self._lock:
            entry = self._tools.get(name)
        return (entry.emoji if entry and entry.emoji else default)

    # ------------------------------------------------------------------
    # check_fn TTL cache (Phase 1)
    # ------------------------------------------------------------------

    _CHECK_FN_TTL_SECONDS = 30.0

    def _check_fn_cached(self, fn: Callable[[], bool]) -> bool:
        """Return bool(fn()), TTL-cached. Swallows exceptions as False."""
        now = time.monotonic()
        with self._check_fn_cache_lock:
            cached = self._check_fn_cache.get(fn)
            if cached is not None:
                ts, value = cached
                if now - ts < self._CHECK_FN_TTL_SECONDS:
                    return value
        try:
            value = bool(fn())
        except Exception:
            value = False
        with self._check_fn_cache_lock:
            self._check_fn_cache[fn] = (now, value)
        return value

    def invalidate_check_fn_cache(self) -> None:
        """Drop all cached check_fn results. Call after env-var changes."""
        with self._check_fn_cache_lock:
            self._check_fn_cache.clear()

    def check_toolset(self, toolset: str) -> bool:
        """Run a toolset's check_fn (if registered). Returns True when unknown."""
        with self._lock:
            check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            return bool(self._check_fn_cached(check))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Auto-discovery (Phase 1)
    # ------------------------------------------------------------------

    def discover_module(self, module_name: str) -> bool:
        """Import a single tool module by dotted path. Returns True on success.

        Side effect: importing the module triggers its `registry.register(...)`
        call. ImportError / SyntaxError are caught and logged.
        """
        try:
            importlib.import_module(module_name)
            return True
        except Exception as exc:
            logger.warning("Could not import tool module %s: %s", module_name, exc)
            return False

    def discover_package(self, package: str, tools_dir: Path | None = None) -> List[str]:
        """Phase 1: auto-discover all tool modules under a package directory.

        Imports every ``*.py`` file in the directory whose module-level AST
        contains a ``registry.register(...)`` call. Returns the list of
        successfully-imported module names.

        Why AST inspection: helper modules (e.g. ``url_safety.py`` when it
        only exports utility functions) shouldn't be imported as tools —
        we'd waste a name slot for nothing. The AST check ensures we only
        auto-import modules that actually register at import time.
        """
        import ast

        if tools_dir is None:
            # Resolve relative to the package's __init__.py
            try:
                pkg = importlib.import_module(package)
                if not hasattr(pkg, "__file__") or pkg.__file__ is None:
                    logger.warning("discover_package: %s has no __file__", package)
                    return []
                tools_dir = Path(pkg.__file__).resolve().parent
            except Exception as exc:
                logger.warning("discover_package: could not resolve %s: %s", package, exc)
                return []

        if not tools_dir.exists() or not tools_dir.is_dir():
            logger.warning("discover_package: %s does not exist", tools_dir)
            return []

        def _is_register_call(node: ast.AST) -> bool:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                return False
            func = node.value.func
            return (
                isinstance(func, ast.Attribute)
                and func.attr == "register"
                and isinstance(func.value, ast.Name)
                and func.value.id == "registry"
            )

        imported: List[str] = []
        for path in sorted(tools_dir.glob("*.py")):
            if path.name in {"__init__.py", "registry.py", "_hermes_adapter.py"}:
                continue
            # AST check — only import if the module calls registry.register at module level
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            if not any(_is_register_call(stmt) for stmt in tree.body):
                continue
            mod_name = f"{package}.{path.stem}"
            try:
                importlib.import_module(mod_name)
                imported.append(mod_name)
            except Exception as exc:
                logger.warning("Could not import tool module %s: %s", mod_name, exc)
        return imported


# Module-level singleton
registry = ToolRegistry.get_instance()


# ---------------------------------------------------------------------------
# Per-agent tool resolution
# ---------------------------------------------------------------------------

# Default tool sets by agent_name (backward compatibility — used when
# AgentApp.tool_config is null/missing)
DEFAULT_TOOLS_BY_AGENT: dict[str, list[str]] = {
    "agent_builder": [
        "create_agent", "update_agent", "list_tools", "list_market_agents",
    ],
    "skill_agent": [
        "create_skill", "update_skill", "list_tools",
    ],
    "automation_agent": [
        "create_automation", "update_automation", "execute_automation",
        "list_knowledge_bases", "list_data_sources", "clarify",
    ],
    "general_assistant": [
        "web_search", "web_extract", "memory", "todo",
        "read_file", "write_file", "image_generation",
        "execute_code", "delegate_task", "run_sandbox_skill",
        # P3-bis: the "Run Now" button in /automation hands the run
        # off to the LLM by injecting a structured "Run Automation
        # Task: ..." prompt and auto-sending it. The LLM needs
        # execute_automation in its toolset to honour that handoff;
        # without it the LLM falls back to "I don't have this tool"
        # and the run never happens. (The dedicated
        # automation_agent above has create_automation / update_automation
        # but explicitly forbids execute_automation — so users who
        # created their automation via the chat continue to be unable
        # to run it from the same chat unless we also expose the
        # tool on the default chat agent.)
        "execute_automation",
        # Phase 11: live dashboards — let the chat agent build live DB dashboards
        "create_dashboard",
        "update_dashboard",
        "undo_dashboard_edit",
        "uiux_search",
        "uiux_design_system",
        # What-if scenario simulation (Brent/Naphtha shocks) — the chat
        # agent answers "what if brent rises 5%?" with the causal-chain
        # elasticity math (no dashboard UI; scenario questions in chat).
        "forecast_what_if",
        # Phase 9: multi-agent swarm — chat agent can create teams, spawn
        # sub-agents, exchange messages, orchestrate parallel workers.
        "swarm_create_team", "swarm_spawn_agent",
        "swarm_send_message", "swarm_get_messages",
        "swarm_list_teams", "swarm_scratch_set", "swarm_scratch_get",
        "swarm_orchestrate",
    ],
    # Phase 9 will expand this and add a "power_user" agent; kept in sync
    # via DEFAULT_POWER_USER_TOOLS in system_agents.py.
}


# ---------------------------------------------------------------------------
# Skill display-name → tool registry-name mapping
# ---------------------------------------------------------------------------

# Maps the human-readable Tool names (as stored in AgentApp.skills and the
# seeded Tool table) to the internal tool registry names used by
# get_tools() / resolve_tools_for_agent().
SKILL_DISPLAY_TO_TOOL_NAME: dict[str, str] = {
    "Web Search": "web_search",
    "Web Extract": "web_extract",
    "Memory": "memory",
    "Todo": "todo",
    "Read File": "read_file",
    "Write File": "write_file",
    "Image Generation": "image_generation",
    "Code Executor": "execute_code",
    "Delegate Task": "delegate_task",
    # "Database Query" maps to the delegation tool, not the granular DB tools
    # — the granular tools (list_data_sources, describe_schema, execute_query,
    # answer_from_database) are only for the data_agent sub-agent.
    "Database Query": "ask_data_agent",
    "Sandbox Skill": "run_sandbox_skill",
    # Phase 0 multimodal split — dedicated narrow tools per skill
    "Image Gen": "mm_image_gen",
    "Video Gen": "mm_video_gen",
    "3D Gen": "mm_3d_gen",
    "Effects": "mm_effects",
}

# Baseline tools every user-created agent gets when its tool_config is
# missing or empty (create) or when skills change without an explicit
# tool_config (update). "Missing or empty" includes None and {} — see
# `_create_agent`'s `if not tool_config:` branch. An explicit
# `tool_config={"enabled_tools": []}` is treated as a deliberate empty
# choice and does NOT trigger the fallback.
#
# This is a stable contract pinned by
# tests/test_user_agent_tool_fallback.py — changing it is a UX-facing
# change and must update the agent-builder "Tools" panel
# (frontend/src/components/agentbuilder/AgentToolsPanel.jsx).
#
# Current baseline: web_search (look things up), memory (persist context),
# todo (plan multi-step work). All three are safe-by-default and do not
# require user consent.
#
# If you add a tool here, also update the agent-builder "Tools" panel
# to list it.
DEFAULT_USER_AGENT_TOOLS: list[str] = [
    "web_search",
    "web_extract",
    "agent_browser",
    "memory",
    "todo",
    "create_artifact",
    "edit_artifact",
    "load_skill_body",
    "Skill",
    "create_dashboard",
    "update_dashboard",
    "undo_dashboard_edit",
    "uiux_search",
    "uiux_design_system",
]


# 2026-08-25: centralized tool-format normalizer. OpenAI/DeepSeek API
# requires every tool to have the structure:
#   {"type": "function", "function": {"name", "description", "parameters"}}
# Various tool-definition sites (registry, data_source_runtime, etc.) used
# to pass through flat schemas (just {"name", "description", "parameters"})
# which DeepSeek rejected with:
#   tools[N]: missing field `type` (status 400)
# Use this function as the LAST step before sending tools to the LLM.
def normalize_tool_schema(schema: dict, *, fallback_name: str = "") -> dict:
    """Wrap a tool schema in the OpenAI function envelope if not already wrapped.

    Idempotent: if the schema is already wrapped, return it unchanged.
    Also validates the wrapped form has a 'type' field set to 'function'.

    Args:
        schema: The tool schema dict (either wrapped or flat).
        fallback_name: Name to use if the schema has no 'name' field.

    Returns:
        A properly wrapped tool schema dict.
    """
    if not isinstance(schema, dict):
        return schema
    # Already in wrapped form
    if schema.get("type") == "function" and "function" in schema:
        return schema
    # Flat form: wrap it
    if "name" in schema or fallback_name:
        return {
            "type": "function",
            "function": {
                "name": schema.get("name") or fallback_name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
            },
        }
    # Unknown form — pass through
    return schema


def normalize_tools_list(tools: list[dict] | None) -> list[dict] | None:
    """Apply normalize_tool_schema to every tool in a list. Returns a new list."""
    if not tools:
        return tools
    return [normalize_tool_schema(t) for t in tools]



def get_skill_to_tool_map(db=None) -> dict[str, str]:
    """Build a mapping of display names → registry tool names.

    Starts with the hardcoded (static) fallback dict for built-in tools,
    then merges DB rows from the ``tools`` table when a DB session is
    provided.  DB rows where ``skill_md`` is non-empty AND whose ``name``
    is also registered in the ToolRegistry win the merge — they override
    the static entry if the name collides.

    This makes skills truly first-class: a marketplace skill with a
    registered handler is automatically discoverable by
    ``resolve_tools_from_skills()`` without touching this file.
    """
    merged = dict(SKILL_DISPLAY_TO_TOOL_NAME)

    if db is not None:
        try:
            from app.models.tool import Tool

            registered_names = set(registry.list_available())
            rows = (
                db.query(Tool.name, Tool.summary)
                .filter(
                    Tool.skill_md.isnot(None),
                    Tool.skill_md != "",
                    Tool.is_deleted == False,
                )
                .all()
            )
            for name, _summary in rows:
                if not name:
                    continue
                # Only include rows whose name maps to a registered tool
                tool_name = SKILL_DISPLAY_TO_TOOL_NAME.get(name)
                if not tool_name:
                    # Also check by lowercased name for flexible matching
                    name_slug = name.strip().replace(" ", "_").lower()
                    if name_slug in registered_names:
                        tool_name = name_slug
                if tool_name:
                    merged[name] = tool_name
        except Exception:
            pass  # DB not available (e.g. during test bootstrap)

    return merged


def resolve_tools_from_skills(skill_names: list[str], db=None) -> list[str]:
    """Map Tool display names to tool registry names, skipping unmapped entries.

    Args:
        skill_names: List of display names from AgentApp.skills (e.g.
                     ["Web Search", "Database Query", "Chart Generator"]).
        db: Optional DB session for DB-driven mapping (skills first-class).

    Returns:
        List of registry tool names (e.g. ["web_search", "ask_data_agent"]).
        Unmapped names are silently skipped — they may be marketplace skills
        or legacy entries without a registered handler.
    """
    mapping = get_skill_to_tool_map(db)
    result: list[str] = []
    for name in (skill_names or []):
        tool_name = mapping.get(name)
        if tool_name and tool_name not in result:
            result.append(tool_name)
    return result


def resolve_tools_for_agent(
    agent_name: str | None,
    tool_config: dict | None = None,
) -> list[str]:
    """Resolve which tool names are available for a given agent.

    Priority:
      1. If tool_config has 'enabled_tools' list → use that
      2. Else fall back to DEFAULT_TOOLS_BY_AGENT[agent_name]
      3. Else empty list (no tools)

    Args:
        agent_name: The agent's name (e.g. "agent_builder", "general_assistant")
        tool_config: The AgentApp.tool_config JSON field, or None.
                     Expected shape: {"enabled_tools": ["web_search", "memory", ...]}
                     Optional "disabled_tools": [...] to subtract from the list.
    """
    if tool_config and isinstance(tool_config, dict):
        enabled = tool_config.get("enabled_tools")
        if enabled and isinstance(enabled, list):
            tools = list(enabled)
            # Apply disabled_tools filter
            disabled = tool_config.get("disabled_tools", [])
            if isinstance(disabled, list):
                tools = [t for t in tools if t not in disabled]
            return tools

    # Fall back to defaults. P3-bis: when ``agent_name`` is None
    # (e.g. the chat was created via the auto-adopted session in
    # ``_create_automation``, which deliberately leaves
    # ``AgentConversation.agent_name`` unset because runs are
    # driven by ``automation_runtime_agent``, not the chat agent),
    # default to ``general_assistant`` so the chat still has a
    # useful toolset — most importantly ``execute_automation``, which
    # the "Run Now" button needs. Without this fallback the LLM
    # would have zero tools and respond with "I don't have the
    # execute_automation tool".
    if agent_name and agent_name in DEFAULT_TOOLS_BY_AGENT:
        return list(DEFAULT_TOOLS_BY_AGENT[agent_name])
    return list(DEFAULT_TOOLS_BY_AGENT.get("general_assistant", []))
