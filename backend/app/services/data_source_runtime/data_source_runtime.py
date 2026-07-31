"""Data source runtime helpers — wire bound KnowledgeBases into the agent chat.

This module owns the rule:

    If an agent has data sources bound, auto-inject `ask_data_agent`
    and a "Bound Data Sources" section into the system prompt.

The 4 granular DB tools (`list_data_sources`, `describe_schema`,
`execute_query`, `answer_from_database`) remain registered in the tool
registry because the `data_agent` subagent calls them internally when
answering a delegated question. They are NOT auto-injected onto the
calling agent's tool list — the calling agent always delegates to the
Data Agent.

The function `prepare_data_source_runtime()` is the single entry point
the chat router calls before talking to the LLM.

Org-level opt-in
----------------
The workspace setting ``auto_bind_all_datasources`` (default OFF) lets
the user grant every agent in the workspace access to every connected
database KB. When ON, the agent's bound list is unioned with every
``KnowledgeBase`` row in the same (org, app) where ``source_kind ==
'database'`` and ``is_deleted == False``. DATA-CORE-3 still holds
because the user explicitly opts in at the workspace level — it's not
a per-agent decision.

The flag lookup uses ``workspace_settings_service.get_bool`` which is
cached for 5 seconds so a tight agent loop doesn't hammer the DB.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.agent_app import AgentApp
from app.models.knowledge_base import KnowledgeBase
from app.models.project import Project
from app.models.project_memory import ProjectMemory
from app.services.tool_registry import registry
from app.services import workspace_settings_service

logger = logging.getLogger(__name__)


def get_bound_data_source_ids(agent_app: AgentApp | None) -> list[str]:
    """Return the list of KB IDs the agent has bound.

    Reads from `agent_app.knowledge_bases` (a JSON list). Returns []
    when the agent has none.
    """
    if agent_app is None:
        return []
    raw = agent_app.knowledge_bases or []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


# The prominent anti-hallucination directive, prepended to the system prompt
# (BEFORE the base content) so it's the first thing the LLM reads. This is
# the top-level guardrail — the detailed "Bound Data Sources" section is still
# appended at the end for reference.
_CRITICAL_ANTI_HALLUCINATION_DIRECTIVE = (
    "CRITICAL RULE: You have bound data sources (database and/or document). "
    "For ANY data question, you MUST call the `ask_data_agent` tool FIRST. "
    "Do NOT fabricate data, invent customer names, or generate data tables "
    "without calling `ask_data_agent`. Once you receive real data from "
    "`ask_data_agent`, summarize the key findings clearly: name the top "
    "performers, cite totals and shares, note the time period, and highlight "
    "any notable patterns. Every number you mention must come from the tool "
    "result — but you SHOULD produce a substantive narrative, not just a "
    "one-line handoff."
)


def _load_bound_kb_meta(db: Session, kb_ids: list[str]) -> list[dict]:
    """Fetch name + db_type for the bound KBs (used in the system prompt)."""
    if not kb_ids:
        return []
    rows = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id.in_(kb_ids), KnowledgeBase.is_deleted == False)  # noqa: E712
        .all()
    )
    return [
        {
            "id": kb.id,
            "name": kb.name,
            "db_type": kb.db_type or "",
            "database_name": kb.database_name or "",
            "source_kind": kb.source_kind or "",
            "file_type": kb.file_type or "",
            "indexing_status": kb.indexing_status,
            "chunk_count": kb.chunk_count or 0,
        }
        for kb in rows
    ]


def _build_data_source_prompt_section(bound_meta: list[dict]) -> str:
    """Compose the 'Bound Data Sources' section appended to the system prompt.

    Describes both database and document (file) sources. Always names the
    LITERAL function name ``ask_data_agent`` the LLM must use in its
    tool_call, lists the parameter signature, and warns that there is no
    alternative path to the data — the Data Agent picks the right internal
    tool (SQL for databases, vector retrieval for documents) based on each
    source's ``source_kind``.
    """
    if not bound_meta:
        return ""

    db_meta = [k for k in bound_meta if k["source_kind"] == "database"]
    file_meta = [k for k in bound_meta if k["source_kind"] == "file"]

    lines = ["## Bound Data Sources", ""]

    if db_meta:
        lines.append("### Database sources")
        for kb in db_meta:
            label = f"- **{kb['name']}** (id=`{kb['id']}`, db_type=`{kb['db_type']}`"
            if kb["database_name"]:
                label += f", database=`{kb['database_name']}`"
            label += ")"
            lines.append(label)
        lines.append("")

    if file_meta:
        lines.append("### Document sources")
        for kb in file_meta:
            status = kb.get("indexing_status") or "unknown"
            cc = kb.get("chunk_count") or 0
            label = (
                f"- **{kb['name']}** (id=`{kb['id']}`, file_type=`{kb['file_type']}`, "
                f"status=`{status}`, chunks={cc})"
            )
            lines.append(label)
        lines.append("")

    lines.extend([
        "**MANDATORY TOOL FOR DATA ACCESS — call the function whose name is exactly `ask_data_agent`.**",
        "This is the ONLY way to reach the bound data sources (database OR document).",
        "You do not have direct SQL or vector-search access; you delegate to the Data",
        "Agent, which picks the right internal tool (SQL for databases, vector",
        "retrieval for documents) based on each source's `source_kind`.",
        "",
        "**Function signature (use the exact `name` field when calling):**",
        "```",
        "ask_data_agent(",
        "    question: str,                # required — the natural-language question to answer",
        "    data_source_id: str = None,   # optional — id of a bound source; omit to let the agent pick",
        "    max_iterations: int = 6,      # optional — cap on subagent tool-calling rounds (max 10)",
        ")",
        "```",
        "",
        "**When to call:**",
        "- Any time the user asks about, references, or implies data from the bound sources.",
        "- Any time you need to know what tables/columns exist (the Data Agent can introspect the schema for you).",
        "- Any time a downstream step (report, chart, summary) requires real numbers — fetch them via `ask_data_agent` first.",
        "- Any time the user asks about content in an uploaded document (PDF, DOCX, CSV, etc.).",
        "",
        "**Workflow for any data question:**",
        "1. Call `ask_data_agent` with a clear natural-language `question`.",
        "2. Read the returned payload: it includes `answer` (prose), `rows` (data), `sql` (what was run),",
        "   `source_id` / `source_name` (which source), and `citations` (tables/columns or file passages used).",
        "3. Compose your reply from that payload. Cite `source_name` and the relevant columns or passages.",
        "4. If the payload indicates an error or empty rows, say so explicitly — do not invent data.",
        "",
        "**Anti-patterns to avoid:**",
        "- Do NOT pretend to query the database, describe steps you intend to take, or narrate a workflow",
        "  without actually invoking `ask_data_agent`. Reasoning traces that list steps like 'Query schema',",
        "  'Run SQL', 'Present results' are hallucinations when no tool call was emitted.",
        "- Do NOT call `list_data_sources`, `describe_schema`, `execute_query`, `answer_from_database`,",
        "  `search_documents`, or `answer_from_documents` — those are internal to the Data Agent and are",
        "  not on your tool list.",
        "- Do NOT generate SQL in your reply text; the SQL lives inside the `ask_data_agent` payload.",
    ])
    return "\n".join(lines)


def _maybe_extend_with_workspace_auto_bind(
    db: Session,
    agent_app: AgentApp | None,
    bound_ids: list[str],
) -> list[str]:
    """If the workspace opt-in is on, union in every connected DB KB.

    Returns the (possibly extended) bound id list, preserving the
    agent's explicit bindings first and appending the auto-bound ones
    in stable name order so the result is deterministic.
    """
    if agent_app is None:
        return bound_ids
    # Pull the flag. ``get_bool`` is cached for 5s so this is cheap
    # inside an agent loop.
    try:
        auto_bind = workspace_settings_service.get_bool(
            db,
            workspace_settings_service.KEY_AUTO_BIND_ALL_DATASOURCES,
            org_id=getattr(agent_app, "org_id", "default-org"),
            app_id=getattr(agent_app, "app_id", "default-app"),
        )
    except Exception as e:  # pragma: no cover — service should not raise
        logger.debug("workspace_settings lookup failed (non-fatal): %s", e)
        return bound_ids

    if not auto_bind:
        return bound_ids

    org_id = getattr(agent_app, "org_id", "default-org")
    app_id = getattr(agent_app, "app_id", "default-app")

    # Every connected KB (database or file) in the same workspace.
    rows = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.is_deleted == False,  # noqa: E712
            KnowledgeBase.org_id == org_id,
            KnowledgeBase.app_id == app_id,
        )
        .order_by(KnowledgeBase.name.asc())
        .all()
    )
    extra_ids = [kb.id for kb in rows if kb.id not in set(bound_ids)]
    if not extra_ids:
        return bound_ids
    logger.info(
        "auto_bind_all_datasources=ON: extending agent %s bound_kb_ids with %d additional DBs",
        getattr(agent_app, "id", "<unknown>"),
        len(extra_ids),
    )
    return list(bound_ids) + extra_ids


def _normalize_project_name(project_name: str | None) -> str | None:
    """Return the legacy project name usable as a binding, or None.

    The legacy ``project`` column defaults to the literal string
    ``"global"`` — that is a label meaning "no project", never a binding.
    Empty/None are likewise not bindings.
    """
    name = (project_name or "").strip()
    if not name or name.lower() == "global":
        return None
    return name


def _extend_with_project_kbs(
    db: Session,
    agent_app: AgentApp | None,
    bound_ids: list[str],
    project_id: str | None,
    project_name: str | None = None,
) -> list[str]:
    """Union in every KB scoped to the *selected* project.

    Contextual scoping: the agent only inherits a project's data sources
    when the user explicitly picked that project for the current
    conversation — instead of auto-inheriting KBs from every project the
    agent is a member of. Returns the (possibly extended) bound id list,
    preserving the agent's explicit bindings first.

    Dual-column parity with the UI: the Resources panel matches KBs via
    EITHER ``project_id`` (FK) OR the legacy ``project`` name string
    (frontend ``ProjectDetail.jsx`` queries both). The backend matches
    the same set — ``project_id == <id> OR lower(project) == lower(<name>)``
    — so a source the UI shows as connected also resolves here. The
    literal name ``"global"`` is the default label, never a binding.
    """
    legacy_name = _normalize_project_name(project_name)
    if not project_id and not legacy_name:
        return bound_ids
    org_id = getattr(agent_app, "org_id", "default-org")
    app_id = getattr(agent_app, "app_id", "default-app")

    clauses = []
    if project_id:
        clauses.append(KnowledgeBase.project_id == project_id)
    if legacy_name:
        clauses.append(func.lower(KnowledgeBase.project) == legacy_name.lower())

    rows = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.is_deleted == False,  # noqa: E712
            or_(*clauses),
            KnowledgeBase.org_id == org_id,
            KnowledgeBase.app_id == app_id,
        )
        .order_by(KnowledgeBase.name.asc())
        .all()
    )
    bound_set = set(bound_ids)
    extra_ids = [kb.id for kb in rows if kb.id not in bound_set]
    if not extra_ids:
        return bound_ids
    logger.info(
        "project_context_scoping: extending agent %s with %d KBs from "
        "selected project %s",
        getattr(agent_app, "id", "<unknown>"),
        len(extra_ids),
        project_id,
    )
    return list(bound_ids) + extra_ids


# Cap on how many shared-memory entries we inject into the prompt. Keeps
# the system prompt bounded even for projects with hundreds of memories;
# the most important / recent entries are surfaced first.
_PROJECT_MEMORY_INJECT_LIMIT = 50


def _build_project_context_block(
    db: Session,
    project_id: str,
    agent_app: AgentApp | None,
) -> str:
    """Build a "Project Context" prompt section for the selected project.

    Includes the project's name + description and a capped slice of its
    shared ``ProjectMemory`` entries (facts, decisions, insights). Returns
    an empty string when the project does not exist (e.g. was deleted) so
    the caller can append the result unconditionally.

    This gives the agent project awareness — independent of whether it has
    any bound data sources — so non-data questions are still grounded in
    the project's established knowledge.
    """
    try:
        project = (
            db.query(Project)
            .filter(
                Project.id == project_id,
                Project.is_deleted == False,  # noqa: E712
            )
            .first()
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("project context lookup failed (non-fatal): %s", e)
        return ""
    if project is None:
        return ""

    org_id = getattr(agent_app, "org_id", "default-org")
    app_id = getattr(agent_app, "app_id", "default-app")

    lines = ["## Project Context", ""]
    lines.append(f"You are operating within the **{project.name}** project.")
    if project.description:
        lines.append("")
        lines.append(f"Description: {project.description}")

    try:
        mem_rows = (
            db.query(ProjectMemory)
            .filter(
                ProjectMemory.project_id == project_id,
                ProjectMemory.is_deleted == False,  # noqa: E712
                ProjectMemory.org_id == org_id,
                ProjectMemory.app_id == app_id,
            )
            .order_by(
                ProjectMemory.importance.desc(),
                ProjectMemory.created_date.desc(),
            )
            .limit(_PROJECT_MEMORY_INJECT_LIMIT)
            .all()
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("project memory lookup failed (non-fatal): %s", e)
        mem_rows = []

    if mem_rows:
        lines.append("")
        lines.append(
            "Shared project memory (facts, decisions, and insights already "
            "established in this project):"
        )
        for m in mem_rows:
            tag = (m.entry_type or "fact").strip()
            lines.append(f"- [{tag}] {m.content}")

    lines.append("")
    lines.append(
        "Use this project context to ground your answers. Prefer "
        "project-specific knowledge when it is relevant to the user's question."
    )
    return "\n".join(lines)


def prepare_data_source_runtime(
    db: Session,
    agent_app: AgentApp | None,
    base_tools: list[dict],
    base_system_prompt: str,
    selected_project_id: str | None = None,
    selected_project_name: str | None = None,
) -> tuple[list[dict], str, dict]:
    """Augment the agent's tool list + system prompt for data source support.

    Contextual scoping rule:

        * No project selected (``selected_project_id`` is None) — the agent
          reads ONLY its own bound data sources (plus any workspace-level
          opt-in via ``auto_bind_all_datasources``).
        * A project is selected — the agent reads its own bound data sources
          UNION the selected project's data sources, AND a "Project Context"
          block (project description + shared project memory) is injected
          into the system prompt so the agent is aware of the project.

    Single-path behavior: if the agent ends up with any bound database KBs,
    inject ``ask_data_agent`` (idempotently) and append the "Bound Data
    Sources" prompt section. Otherwise, return the base tools unchanged
    (project context is still injected when a project is selected).

    Returns:
        (tools, system_prompt, ctx_extras)

    `ctx_extras` is what the caller should merge into the per-tool-call
    `context` dict passed to `execute_tool` — it carries `bound_kb_ids`
    so the tool handlers can scope queries correctly.
    """
    bound_ids = get_bound_data_source_ids(agent_app)
    bound_ids = _maybe_extend_with_workspace_auto_bind(db, agent_app, bound_ids)
    # Contextual project scoping: the agent only inherits a project's data
    # sources when the user explicitly selected that project for THIS
    # conversation. No project selected → agent's own data sources only.
    # Dual-column parity with the UI: KBs bound via the legacy ``project``
    # name column count too (see _extend_with_project_kbs).
    if selected_project_id or _normalize_project_name(selected_project_name):
        bound_ids = _extend_with_project_kbs(
            db, agent_app, bound_ids, selected_project_id,
            project_name=selected_project_name,
        )

    # Inject project context (description + shared memory) whenever a
    # project is selected — independent of whether the agent has any bound
    # data sources, so non-data questions are still project-aware.
    project_block = ""
    if selected_project_id:
        project_block = _build_project_context_block(
            db, selected_project_id, agent_app
        )

    if not bound_ids:
        # No data sources, but still surface project context if selected.
        new_prompt = base_system_prompt + (
            "\n\n" + project_block if project_block else ""
        )
        return base_tools, new_prompt, {}

    # Idempotent injection: don't add `ask_data_agent` if it's already in
    # the base list (e.g. the user explicitly added it via enabled_tools).
    existing_names = {
        t.get("function", {}).get("name") for t in base_tools
    }
    augmented_tools = list(base_tools)
    if "ask_data_agent" not in existing_names:
        entry = registry.get_entry("ask_data_agent")
        if entry:
            augmented_tools.append(entry.schema)

    bound_meta = _load_bound_kb_meta(db, bound_ids)
    section = _build_data_source_prompt_section(bound_meta)

    # PREPEND the critical anti-hallucination directive so it's the first
    # thing the LLM reads. The detailed "Bound Data Sources" section and
    # the optional "Project Context" block are appended at the end.
    new_prompt = (
        _CRITICAL_ANTI_HALLUCINATION_DIRECTIVE
        + "\n\n"
        + base_system_prompt
        + ("\n\n" + section if section else "")
        + ("\n\n" + project_block if project_block else "")
    )

    # Carry `project_id` into the tool context so handlers (create_automation,
    # list_knowledge_bases) can scope their results to the current project
    # without needing the LLM to pass `project_id` itself.
    ctx_extras: dict = {
        "bound_kb_ids": bound_ids,
        "project_id": selected_project_id,
    }

    return augmented_tools, new_prompt, ctx_extras
