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
import re
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.agent_app import AgentApp
from app.models.knowledge_base import KnowledgeBase
from app.models.project import Project
from app.models.project_memory import ProjectMemory
from app.services.tool_registry import registry
from app.services import workspace_settings_service

logger = logging.getLogger(__name__)

# ── Demo/test source detection ─────────────────────────────────────────────
# A bound KnowledgeBase whose name or database name matches these markers is
# treated as a DEMO / TEST / SAMPLE source. When a conversation has BOTH a
# demo source AND a real (non-demo) database source, the demo source is
# EXCLUDED from the agent's bound set so the LLM can never mistake demo data
# for production data (observed: supply-chain snapshot built from
# "Demo E2E PostgreSQL" while the real ERP was bound — user saw fake-looking
# customer names and reported "fabricated data"). Demo sources remain usable
# ONLY when they are the sole bound source (i.e. a demo-only workspace).
# Overridable via settings.DEMO_SOURCE_MARKERS (comma-separated).
_DEMO_SOURCE_MARKERS: tuple[str, ...] = ("demo", "test", "sample", "e2e", "sandbox", "staging")


def _demo_source_markers() -> tuple[str, ...]:
    """Return the configured demo/test marker substrings (lowercase)."""
    raw = getattr(settings, "DEMO_SOURCE_MARKERS", None)
    if isinstance(raw, str) and raw.strip():
        return tuple(m.strip().lower() for m in raw.split(",") if m.strip())
    return _DEMO_SOURCE_MARKERS


def is_demo_source_name(name: str | None, database_name: str | None = None) -> bool:
    """True when a KB name / database name carries a demo/test marker.

    DB-agnostic: matches substrings on metadata only (never table/column
    names, never hardcoded ids). Case-insensitive.
    """
    markers = _demo_source_markers()
    if not markers:
        return False
    haystacks = [s for s in (name, database_name) if s]
    for marker in markers:
        for hay in haystacks:
            if marker in hay.lower():
                return True
    return False


def split_demo_and_real_kb_ids(
    kb_ids: list[str],
    name_map: dict[str, tuple[str | None, str | None]] | None = None,
) -> tuple[list[str], list[str]]:
    """Partition KB ids into (demo_ids, real_ids) by id/name/database markers.

    ``name_map`` maps kb_id → (name, database_name) so a KB whose NAME (not
    just its id) carries a demo marker is classified correctly. Pure
    metadata partition — no DB access. ``real_ids`` keeps ordering.
    """
    demo_ids: list[str] = []
    real_ids: list[str] = []
    for kb_id in kb_ids:
        name, db_name = (name_map or {}).get(kb_id, (None, None))
        if is_demo_source_name(kb_id, None) or is_demo_source_name(name, db_name):
            demo_ids.append(kb_id)
        else:
            real_ids.append(kb_id)
    return demo_ids, real_ids


def exclude_demo_sources_when_real_present(
    bound_ids: list[str],
    name_map: dict[str, tuple[str | None, str | None]] | None = None,
) -> list[str]:
    """Drop demo-marked KB ids when at least one non-demo id is also bound.

    The LLM then only sees real sources — it cannot pick the demo database
    for a production deliverable. When ALL bound ids are demo (a demo-only
    workspace), the list is returned unchanged so the demo still works.
    """
    demo_ids, real_ids = split_demo_and_real_kb_ids(bound_ids, name_map=name_map)
    if real_ids and demo_ids:
        return real_ids
    return bound_ids


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


# ── Layer-2 provenance assertion (artifact pipeline backstop) ──────────────
# Layer 1 (prepare_data_source_runtime) removes demo sources from the agent's
# prompt. This layer catches citations that still leak into an artifact:
# historical ask_data results, orchestrator fallback paths that carry no
# bound_kb_ids, or the LLM copying a demo source name verbatim into
# payload["source"]. DB-agnostic: matches metadata (name/database_name) only.


def _resolve_kb_name_map(db: Session, kb_ids: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """Fetch {kb_id: (name, database_name)} for a set of ids (best-effort)."""
    name_map: dict[str, tuple[str | None, str | None]] = {}
    if not kb_ids:
        return name_map
    try:
        rows = db.query(KnowledgeBase).filter(
            KnowledgeBase.id.in_(kb_ids),
            KnowledgeBase.is_deleted == False,  # noqa: E712
        ).all()
        for kb in rows:
            name_map[kb.id] = (kb.name, kb.database_name)
    except Exception:  # pragma: no cover - defensive
        pass
    return name_map


def _cited_source_tokens(payload: Any, execution: Any = None) -> list[dict]:
    """Extract cited source tokens from an artifact payload.

    Returns a list of {"name", "id", "database"} dicts — the citation
    carriers an artifact can express. Handles the flat ``source`` string,
    ``sources``/``data_sources`` lists (strings or dicts), the nested
    ``report_card_payload.source``, explicit ``source_kb_id``/``source_id``,
    and the cached DataExecution result's source block.
    """
    tokens: list[dict] = []

    def _add(name=None, sid=None, database=None):
        name = (str(name or "").strip()) or None
        sid = (str(sid or "").strip()) or None
        database = (str(database or "").strip()) or None
        if name or sid or database:
            tokens.append({"name": name, "id": sid, "database": database})

    if isinstance(payload, dict):
        _add(
            payload.get("source"),
            payload.get("source_kb_id") or payload.get("source_id"),
        )
        for key in ("sources", "data_sources"):
            raw = payload.get(key)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)
                    elif isinstance(item, dict):
                        _add(
                            item.get("name"),
                            item.get("id") or item.get("kb_id") or item.get("source_id"),
                            item.get("database_name") or item.get("database"),
                        )
        rcp = payload.get("report_card_payload")
        if isinstance(rcp, dict):
            _add(rcp.get("source"))
    if execution is not None:
        result = getattr(execution, "result", None)
        if isinstance(result, dict):
            src = result.get("source") if isinstance(result.get("source"), dict) else {}
            _add(
                src.get("name") or result.get("source_name"),
                src.get("id") or result.get("source_id"),
                src.get("database_name") or src.get("database"),
            )
    return tokens


def assert_artifact_source_provenance(
    db: Session,
    *,
    payload: Any = None,
    execution: Any = None,
    context: dict | None = None,
) -> dict:
    """Layer-2 demo-source guard: reject artifacts that CITE a demo/test
    source while a real source was available to the agent.

    Returns:
        {
          "ok": bool, "reason": str,
          "demo_cited": [{"name", "id", "database"}, ...],
          "real_present": bool, "bound_ids": [...],
          "mode": "reject" | "warn" | "off",
        }

    ``mode`` comes from settings.ARTIFACT_PROVENANCE_GUARD. In "warn" mode
    the caller persists the artifact but records ``_provenance_warning`` on
    the payload; in "reject" mode the caller must abort creation. Demo-only
    workspaces always pass (mirrors layer-1 semantics).
    """
    mode = (getattr(settings, "ARTIFACT_PROVENANCE_GUARD", "reject") or "reject").strip().lower()
    if mode == "off":
        return {
            "ok": True, "reason": "guard_off", "demo_cited": [],
            "real_present": False, "bound_ids": [], "mode": mode,
        }

    # 1. Resolve the effective bound set. Prefer the post-layer-1 set from
    #    the tool context; recompute from the agent when absent (orchestrator
    #    marker/fallback paths only pass conversation_id + agent_app_id).
    bound_ids: list[str] = []
    ctx_bound = (context or {}).get("bound_kb_ids")
    if isinstance(ctx_bound, list):
        bound_ids = [str(x) for x in ctx_bound if x]
    if not bound_ids:
        agent_app = None
        agent_app_id = (context or {}).get("agent_app_id")
        if agent_app_id:
            try:
                agent_app = db.query(AgentApp).filter(AgentApp.id == agent_app_id).first()
            except Exception:  # pragma: no cover - defensive
                agent_app = None
        bound_ids = get_bound_data_source_ids(agent_app)
        try:
            bound_ids = _extend_with_project_kbs(
                db, agent_app, bound_ids,
                project_id=(context or {}).get("project_id"),
            )
        except Exception:  # pragma: no cover - defensive
            pass

    name_map = _resolve_kb_name_map(db, bound_ids)
    _, real_ids = split_demo_and_real_kb_ids(bound_ids, name_map=name_map)
    real_present = bool(real_ids)

    # 2. Classify every citation in the payload.
    demo_cited: list[dict] = []
    for token in _cited_source_tokens(payload, execution):
        tid = token.get("id")
        if tid and tid in name_map:
            kb_name, kb_db = name_map[tid]
            if is_demo_source_name(kb_name, kb_db):
                demo_cited.append({
                    "name": kb_name or token.get("name"),
                    "id": tid,
                    "database": kb_db or token.get("database"),
                })
                continue
        if is_demo_source_name(token.get("name"), token.get("database")):
            demo_cited.append(token)

    if demo_cited and real_present:
        return {
            "ok": False, "reason": "demo_source_citation",
            "demo_cited": demo_cited, "real_present": True,
            "bound_ids": bound_ids, "mode": mode,
        }
    return {
        "ok": True, "reason": "ok",
        "demo_cited": demo_cited, "real_present": real_present,
        "bound_ids": bound_ids, "mode": mode,
    }


# The prominent anti-hallucination directive, prepended to the system prompt
# (BEFORE the base content) so it's the first thing the LLM reads. This is
# the top-level guardrail — the detailed "Bound Data Sources" section is still
# appended at the end for reference.
#
# When `has_weekly_report` is True, an exception clause is appended so
def _build_anti_hallucination_directive(has_weekly_report: bool = False) -> str:
    directive = (
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
    return directive


# Keep the legacy constant for backward compatibility with any external
# references (e.g. tests that import it for inspection).
_LEGACY_DIRECTIVE = (
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


def _load_catalog_meta(db: Session, kb_ids: list[str]) -> dict:
    """Build ``{column_name: [sample_values]}`` for the bound KBs (best-effort).

    Feeds the answer-verification gate's catalog validation (Fix 1b): a
    candidate dimension token is only kept when it names a real catalog
    column or appears inside a column's sampled values.  Any failure returns
    ``{}`` — the gate then degrades to lexical-only behavior.
    """
    if not kb_ids:
        return {}
    try:
        from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

        rows = (
            db.query(
                KBColumnMeta.column_name,
                KBColumnMeta.sample_values,
                KBTableMeta.kb_id,
            )
            .join(KBTableMeta, KBTableMeta.id == KBColumnMeta.table_meta_id)
            .filter(KBTableMeta.kb_id.in_(kb_ids))
            .all()
        )
    except Exception:  # noqa: BLE001 — catalog is best-effort
        logger.info("catalog_meta load failed; answer-verification uses lexical mode")
        return {}
    meta: dict = {}
    for col_name, samples, _kb_id in rows:
        key = str(col_name or "")
        if not key:
            continue
        values: list[str] = []
        if isinstance(samples, dict):
            # {value: count} — column name match alone is already the primary
            # signal; sampled values are a secondary vocabulary bridge.
            values = [str(v) for v in samples.keys() if str(v).strip()]
        elif isinstance(samples, list):
            values = [str(v) for v in samples if str(v).strip()]
        meta.setdefault(key, values)
    return meta


_NUMERIC_TYPE_HINTS = (
    "int", "float", "double", "decimal", "number", "numeric",
    "bigint", "smallint", "tinyint", "money", "real",
)


def _looks_like_date_column(col_name: str, data_type: str) -> bool:
    name = (col_name or "").lower()
    if "date" in name or "time" in name or "period" in name or "month" in name:
        return True
    dt = (data_type or "").lower()
    return "date" in dt or "time" in dt or "timestamp" in dt


_INTERNAL_TABLE_RE = re.compile(
    r"^(auth_|access_|cockpit_|dataset_|data_source|alembic_|agent_|chat_|kb_|"
    r"task_|automation_|alert_|report_|artifact|notification|integration|"
    r"model_|llm_|project_|session_|settings|token|audit|job_|sandbox)",
    re.IGNORECASE,
)


def _is_internal_table(name: str) -> bool:
    return bool(_INTERNAL_TABLE_RE.match(name))


def _build_schema_slice(db: Session, kb_ids: list[str]) -> dict[str, str]:
    """Build a compact per-KB ``[schema: ...]`` hint (<800 chars each).

    Format (facts only, deterministic, from the catalog at prompt-build time)::

        [schema: sales_header*(销售出库单; id*,dt=date_col)→sales_entry(出库明细; id*→h.id);
         measures: qty(实发数量),amount(不含税金额)]

    Column descriptions (``description_zh``) are appended in parentheses so the
    calling agent can map business terms like "volume" → ``qty`` without
    needing ``describe_schema``. This saves one full Data Agent iteration
    (~40-50s) per delegation. Any failure returns ``{}`` and the Data Agent
    falls back to its own schema discovery.
    """
    if not kb_ids:
        return {}
    try:
        from app.models.knowledge_catalog import (
            KBColumnMeta,
            KBTableMeta,
            KBTableRelation,
        )

        tables = (
            db.query(KBTableMeta)
            .filter(
                KBTableMeta.kb_id.in_(kb_ids),
                KBTableMeta.table_type == "TABLE",
            )
            .all()
        )
        if not tables:
            return {}
        # Filter out app-internal tables so the limited slice
        # advertises ERP business tables, not auth_user / cockpit_definitions.
        tables = [t for t in tables if not _is_internal_table(t.table_name)]
        if not tables:
            return {}
        table_by_id = {t.id: t for t in tables}

        cols_by_table: dict[str, list[KBColumnMeta]] = {}
        col_rows = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id.in_(list(table_by_id)))
            .order_by(KBColumnMeta.ordinal.asc())
            .all()
        )
        for c in col_rows:
            cols_by_table.setdefault(c.table_meta_id, []).append(c)

        rel_edges: list[tuple[str, str, str, str]] = []
        rel_rows = (
            db.query(KBTableRelation)
            .filter(
                KBTableRelation.source_table_meta_id.in_(list(table_by_id)),
                KBTableRelation.target_table_meta_id.in_(list(table_by_id)),
            )
            .all()
        )
        for r in rel_rows:
            s_tab = table_by_id[r.source_table_meta_id].table_name
            t_tab = table_by_id[r.target_table_meta_id].table_name
            for sc, tc in zip(r.source_columns or [], r.target_columns or []):
                rel_edges.append((s_tab, sc, t_tab, tc))

        out: dict[str, str] = {}
        for kb_id in kb_ids:
            kb_tables = [t for t in tables if t.kb_id == kb_id]
            if not kb_tables:
                continue
            parts: list[str] = []
            measures: list[str] = []
            seen_measures: set[str] = set()
            for t in kb_tables:
                cols = cols_by_table.get(t.id, [])
                pk_cols = [c.column_name for c in cols if c.is_primary_key]
                date_cols = [
                    c.column_name for c in cols
                    if _looks_like_date_column(c.column_name, c.data_type)
                ]
                toks: list[str] = []
                if pk_cols:
                    toks.append(",".join(f"{p}*" for p in pk_cols[:2]))
                if date_cols:
                    toks.append("dt=" + date_cols[0])
                role_mark = "*" if t.table_role in ("fact", "entity_master") else ""
                # Add table description (e.g. "销售出库单") if available
                tab_desc = (t.description_zh or "").strip()
                header = f"{t.table_name}{role_mark}"
                if tab_desc:
                    header += f"({tab_desc};"
                else:
                    header += "("
                if toks:
                    header += ",".join(toks)
                header += ")"
                parts.append(header)
                # Collect measure columns (numeric types) from FACT tables
                # only, excluding primary/foreign-key ids.
                # Include description_zh so the LLM can map business terms.
                if t.table_role in ("fact", "entity_master"):
                    for c in cols:
                        if c.is_primary_key:
                            continue
                        dt = (c.data_type or "").lower()
                        if any(h in dt for h in _NUMERIC_TYPE_HINTS):
                            if c.column_name not in seen_measures:
                                seen_measures.add(c.column_name)
                                col_desc = (c.description_zh or "").strip()
                                if col_desc:
                                    measures.append(f"{c.column_name}({col_desc})")
                                else:
                                    measures.append(c.column_name)

            edges = [
                f"{src}.{sc}→{tgt}.{tc}"
                for src, sc, tgt, tc in rel_edges
            ]
            if edges:
                parts.append("joins:" + ",".join(edges[:3]))
            if measures:
                parts.append("measures:" + ",".join(measures[:8]))

            slice_str = "[schema: " + "; ".join(parts) + "]"
            if len(slice_str) > 800:
                slice_str = slice_str[:800] + "…]"
            out[kb_id] = slice_str
        return out
    except Exception:  # noqa: BLE001 — catalog is best-effort
        logger.info("schema_slice build failed; Data Agent will self-discover", exc_info=True)
        return {}


def _build_concept_catalog(db: Session, kb_ids: list[str]) -> dict[str, str]:
    """Build a per-KB concept→table mapping from catalog descriptions.

    Format::

        - **Sales (销售)**: sales_header + sales_entry
          Volume→qty, Revenue excl.tax→amount
        - **Inventory (库存)**: inventory_view
          Qty→qty

    This lets the parent LLM map business terms ("volume", "revenue")
    directly to column names without needing the Data Agent to discover
    them via ``describe_schema``, saving ~40-50s per query.
    """
    if not kb_ids:
        return {}
    try:
        from app.models.knowledge_catalog import (
            KBColumnMeta,
            KBTableMeta,
            KBTableRelation,
        )

        tables = (
            db.query(KBTableMeta)
            .filter(
                KBTableMeta.kb_id.in_(kb_ids),
                KBTableMeta.table_type == "TABLE",
                KBTableMeta.table_role.in_(("fact", "entity_master")),
            )
            .all()
        )
        if not tables:
            return {}
        tables = [t for t in tables if not _is_internal_table(t.table_name)]
        if not tables:
            return {}
        table_by_id = {t.id: t for t in tables}

        cols_by_table: dict[str, list[KBColumnMeta]] = {}
        col_rows = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id.in_(list(table_by_id)))
            .order_by(KBColumnMeta.ordinal.asc())
            .all()
        )
        for c in col_rows:
            cols_by_table.setdefault(c.table_meta_id, []).append(c)

        # Find child tables (FK pointing to a fact/entity_master)
        child_map: dict[str, list[str]] = {}
        rel_rows = (
            db.query(KBTableRelation)
            .filter(
                KBTableRelation.source_table_meta_id.in_(list(table_by_id)),
            )
            .all()
        )
        for r in rel_rows:
            src = table_by_id.get(r.source_table_meta_id)
            if src:
                child_map.setdefault(src.table_name, []).append(
                    r.target_table_meta_id
                )

        out: dict[str, str] = {}
        for kb_id in kb_ids:
            kb_tables = [t for t in tables if t.kb_id == kb_id]
            if not kb_tables:
                continue
            # FIX 2026-08-22: separate described from undescribed tables
            # so we can cap the catalog size and avoid blowing the
            # subagent's 20s prompt budget.  Emitting a full column dump
            # for every table in a 100+ table warehouse made every
            # subagent call hit 20.1s exactly (timeout) and return no
            # data, so the post-loop fallback fired.
            described_lines: list[str] = []
            undescribed_lines: list[str] = []
            for t in kb_tables:
                tab_desc_zh = (t.description_zh or "").strip()
                tab_desc_en = (t.description_en or "").strip()
                # Use Chinese description as primary label, English as fallback
                label = tab_desc_zh or tab_desc_en or t.table_name
                cols = cols_by_table.get(t.id, [])
                # Collect measure columns with descriptions
                measure_parts: list[str] = []
                # Only collect column names for tables with a description.
                # The LLM can't usefully map a query to a table without
                # a description anyway, so undumped column lists for
                # undescribed tables lose nothing — and skipping them
                # saves a lot of prompt tokens.
                all_col_names: list[str] = []
                _has_description = bool(tab_desc_zh or tab_desc_en)
                if _has_description:
                    for c in cols:
                        if c.is_primary_key:
                            continue
                        dt = (c.data_type or "").lower()
                        all_col_names.append(c.column_name)
                        if any(h in dt for h in _NUMERIC_TYPE_HINTS):
                            col_desc = (c.description_zh or "").strip()
                            col_desc_en = (c.description_en or "").strip()
                            desc = col_desc or col_desc_en
                            if desc:
                                measure_parts.append(f"{desc}→{c.column_name}")
                # Collect date columns
                date_cols = [
                    c.column_name for c in cols
                    if _looks_like_date_column(c.column_name, c.data_type)
                ]
                # Find child table names via FK
                child_names: list[str] = []
                for cid in child_map.get(t.table_name, []):
                    child_t = table_by_id.get(cid)
                    if child_t:
                        child_names.append(child_t.table_name)
                table_list = t.table_name
                if child_names:
                    table_list += " + " + " + ".join(child_names)
                line = f"- **{label}**: {table_list}"
                if measure_parts:
                    line += "\n  Measures: " + ", ".join(measure_parts[:10])
                # Only emit column list for described tables (see above)
                if all_col_names:
                    shown = all_col_names[:30]
                    suffix = f" (+{len(all_col_names)-30} more)" if len(all_col_names) > 30 else ""
                    line += f"\n  Columns: {', '.join(shown)}{suffix}"
                if date_cols:
                    line += f"\n  Date column: {date_cols[0]}"
                if _has_description:
                    described_lines.append(line)
                else:
                    undescribed_lines.append(line)
            # Emit described tables first (most relevant), then undescribed,
            # with a hard cap of 30 tables so the catalog never blows the
            # subagent budget.
            _CATALOG_MAX_TABLES = 30
            combined = described_lines + undescribed_lines
            concept_lines = combined[:_CATALOG_MAX_TABLES]
            if len(combined) > _CATALOG_MAX_TABLES:
                omitted = len(combined) - _CATALOG_MAX_TABLES
                concept_lines.append(
                    f"\n... (+{omitted} more tables — use "
                    f"`describe_schema(table_name)` to inspect specific tables)"
                )
            if concept_lines:
                out[kb_id] = "\n".join(concept_lines)
        return out
    except Exception:  # noqa: BLE001 — catalog is best-effort
        logger.info("concept_catalog build failed; skipping", exc_info=True)
        return {}


# P1-5 — structural compression helpers for small-context models.
# These keep the table/column STRUCTURE (the LLM still knows what tables
# exist and how to join them) but drop the expensive parts (sample
# rows, verbose descriptions, exhaustive concept catalog).

# Heuristics for "this line is a sample row, not a column definition".
_SAMPLE_ROW_MARKERS = (
    "sample row", "example:", "examples:", "samples:",
    "```", "e.g.", "for instance",
)
# Heuristics for "this line is a verbose description, not a column".
# A line >200 chars without any SQL/structural token is treated as
# description and dropped in compact mode.
_STRUCTURAL_TOKENS = (
    "CREATE", "PRIMARY", "FOREIGN", "KEY", "VARCHAR", "INT", "DATE",
    "NUMERIC", "BOOLEAN", "TEXT", "TIMESTAMP", "INDEX", "UNIQUE",
    "TABLE", "REFERENCES", "DEFAULT", "NOT NULL", "CHECK",
)


def _compact_schema_hint(full: str) -> str:
    """Drop sample rows + verbose descriptions from a schema hint while
    preserving every column name, type, and constraint.

    Structural rules:
      * Drop any line containing a sample-row marker.
      * Drop any line >200 chars that lacks a structural SQL token
        (catches long description paragraphs).
      * Keep all other lines verbatim.
      * Never slice a line in half — line-level filtering only.
    """
    if not full:
        return ""
    out: list[str] = []
    in_sample_block = False
    for line in full.splitlines():
        low = line.lower()
        # Mark the start of a sample block.
        if any(m in low for m in _SAMPLE_ROW_MARKERS):
            in_sample_block = True
            # If the marker line itself has no column info, drop it.
            if not any(t in line for t in _STRUCTURAL_TOKENS):
                continue
        # While in a sample block, drop lines unless they look structural.
        if in_sample_block:
            if any(t in line for t in _STRUCTURAL_TOKENS):
                out.append(line)
                continue
            if line.strip() == "":
                in_sample_block = False
                continue
            continue
        # Outside a sample block: drop only the over-long description lines.
        if len(line) > 200 and not any(t in line for t in _STRUCTURAL_TOKENS):
            continue
        out.append(line)
    return "\n".join(out)


def _compact_concept_catalog(full: str, max_lines: int = 20) -> str:
    """Keep the first ``max_lines`` lines of a concept catalog."""
    if not full:
        return ""
    return "\n".join(full.splitlines()[:max_lines])


def _build_data_source_prompt_section(
    bound_meta: list[dict], has_weekly_report: bool = False,
    schema_slices: dict[str, str] | None = None,
    concept_catalogs: dict[str, str] | None = None,
    compact_mode: bool = False,
    compact_concept_max_lines: int = 20,
) -> str:
    """Compose the 'Bound Data Sources' section appended to the system prompt.

    Describes both database and document (file) sources. Always names the
    LITERAL function name ``ask_data_agent`` the LLM must use in its
    tool_call, lists the parameter signature, and warns that there is no
    alternative path to the data — the Data Agent picks the right internal
    tool (SQL for databases, vector retrieval for documents) based on each
    source's ``source_kind``.

    When ``has_weekly_report`` is True, an exception bullet is added to the
    "When to call" list so the LLM knows weekly-report requests bypass
    `ask_data_agent`.
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
            if is_demo_source_name(kb["name"], kb.get("database_name")):
                label += (
                    " — **DEMO/TEST source — do NOT present its data as real "
                    "business data in a deliverable.**"
                )
            lines.append(label)
        lines.append("")
        if schema_slices:
            for kb in db_meta:
                sl = (schema_slices or {}).get(kb["id"])
                if sl:
                    if compact_mode:
                        sl = _compact_schema_hint(sl)
                    lines.append(
                        f"  Schema hint for `{kb['name']}`: `{sl}`"
                    )
            lines.append("")
        # Data Concepts: business-term → table/column mapping
        if concept_catalogs:
            for kb in db_meta:
                cc = (concept_catalogs or {}).get(kb["id"])
                if cc:
                    if compact_mode:
                        cc = _compact_concept_catalog(
                            cc, max_lines=compact_concept_max_lines
                        )
                    lines.append("  **Data Concepts** (business term → table.column):")
                    for concept_line in cc.splitlines():
                        lines.append(f"  {concept_line}")
                    lines.append("")
                    lines.append(
                        "  Use these mappings to pick the right tables and columns in your "
                        "`ask_data_agent` question. For example, if the user asks for "
                        "\"sales volume\", mention the column from the concept map so the "
                        "Data Agent queries the correct column directly."
                    )
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
        "**FAST PATH: `fetch_data_batch` — Direct Parallel SQL (1-5s, 50-100x faster)**",
        "When the Data Concepts catalog above lists the exact table and column names you need,",
        "use `fetch_data_batch` to run SQL queries directly — no sub-agent loop, no schema",
        "discovery. All queries execute in parallel. This is dramatically faster than",
        "`ask_data_agent` for known schemas.",
        "",
        "⚠ IMPORTANT: Only use REAL table and column names from the Data Concepts catalog.",
        "NEVER use placeholder syntax like [field], [table], [column] in SQL — those are",
        "documentation conventions, not real identifiers. If you don't know the exact column",
        "name, use `ask_data_agent` instead.",
        "",
        "```",
        "fetch_data_batch(",
        "    queries: [",
        "        {sql: \"SELECT SUM(quantity) AS volume FROM sales_table WHERE date >= '2026-07-01'\", label: 'sales_volume'},",
        "        {sql: \"SELECT SUM(revenue) AS revenue FROM sales_table WHERE date >= '2026-07-01'\", label: 'sales_revenue'},",
        "    ]",
        ")",
        "```",
        "",
        "**Note:** The SQL above uses generic placeholder names for illustration only. You MUST",
        "replace `sales_table`, `quantity`, `revenue`, `date` with REAL table and column names",
        "from the Data Concepts catalog above. NEVER use placeholder or invented names.",
        "",
        "**When to use `fetch_data_batch` vs `ask_data_agent`:**",
        "- fetch_data_batch: Data Concepts catalog shows the exact tables/columns, and you can write",
        "  valid SQL using those names. Best for structured reports with 2+ metrics.",
        "- ask_data_agent: Schema is unknown, question is exploratory, or you're unsure which tables",
        "  to query. The Data Agent discovers the schema for you (but takes ~100s).",
        "- **Best practice**: For multi-metric reports (e.g., 'volume, revenue, margin, inventory'),",
        "  combine ALL metrics into FEWER `fetch_data_batch` calls (1-2 calls with multi-column SELECTs)",
        "  rather than one call per metric.",
        "",
        "**SLOW PATH: `ask_data_agent` — Sub-Agent Schema Discovery (~100s)**",
        "When schema is unknown, call the function whose name is exactly `ask_data_agent`.",
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
        "- For monthly/weekly/quarterly report requests, make ONE consolidated `ask_data_agent` call that asks for the full reporting dataset: current period totals, comparison period totals, product/customer breakdowns, available margin/cost fields, and any inventory snapshot needed. Do NOT split one report into serial data-agent calls unless the first call fails.",
        "- If a schema hint (a `[schema: ...]` block) or Data Concepts map is listed above, include the relevant table and column names in your `question` argument. This lets the Data Agent skip schema discovery and run the query directly, cutting response time by ~50%. Example: 'Sales volume and revenue for July 2026 from [table_name from Data Concepts]'",
        "- Keep `question` SHORT (1-2 sentences) and natural-language. Do NOT paste raw SQL, full column lists, or join hints into it — the Data Agent writes its own SQL from the schema hint + your question.",
    ])
    lines.extend([
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


def _build_entity_map_block(db: Session, project_id: str) -> str:
    """Build a ~300-400 token 'Project Data Map' prompt section.

    Lists the project's entities (universal types only) and their linked
    data sources so the LLM knows the project's vocabulary. Returns an
    empty string when no entities exist. Flag-gated by ENTITY_GRAPH_ENABLED.
    """
    try:
        from app.models.knowledge_catalog import (
            ProjectEntity,
            ProjectEntityLink,
        )

        entities = (
            db.query(ProjectEntity)
            .filter(
                ProjectEntity.project_id == project_id,
                ProjectEntity.is_deleted == False,  # noqa: E712
            )
            .order_by(ProjectEntity.entity_type, ProjectEntity.name)
            .limit(30)
            .all()
        )
        if not entities:
            return ""

        entity_ids = [e.id for e in entities]
        links = (
            db.query(ProjectEntityLink)
            .filter(
                ProjectEntityLink.entity_id.in_(entity_ids),
                ProjectEntityLink.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        links_by_entity: dict[str, list] = {}
        for link in links:
            links_by_entity.setdefault(link.entity_id, []).append(link)

        lines = ["## Project Data Map (entities and their data sources)"]
        for e in entities:
            elinks = links_by_entity.get(e.id, [])
            link_str = ", ".join(
                f"{l.target_type}:{l.target_id}" for l in elinks[:3]
            )
            aliases_str = ""
            if e.aliases:
                aliases_str = f" (aliases: {', '.join(e.aliases[:3])})"
            lines.append(
                f"- [{e.entity_type}] {e.name}{aliases_str}"
                f" → {link_str or 'no linked data yet'}"
            )
        lines.append("")
        lines.append(
            "Use these entity names when formulating queries about project data."
        )
        return "\n".join(lines)
    except Exception:
        return ""


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


def _resolve_project_from_message(
    db: Session,
    user_message: str | None,
    org_id: str,
    app_id: str,
) -> tuple[str | None, str | None]:
    """Best-effort: map a project mention inside the user message to a project.

    The global chat (``general_assistant`` with no project selected) has zero
    bound data, but the user may explicitly name a project in the message
    (\"make a c5 c9 market view ppt\" → C5_C9).  When that happens we scope
    the agent to THAT project's data sources instead of leaving it with
    nothing — same anti-leakage wall, but the user's explicit mention is the
    selection.

    Matching is deterministic and conservative:
      * normalize both sides (lowercase, strip non-alphanumeric) so
        \"C5_C9\" / \"c5 c9\" / \"c5-c9\" all match project \"C5_C9\";
      * exact normalized match required — never prefix/partial fuzzy;
      * the literal \"global\" is never a binding.
    Returns (project_id, project_name) or (None, None).
    """
    if not user_message or not str(user_message).strip():
        return None, None
    from app.models.project import Project as _Project

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s.lower())

    needle = _norm(user_message)
    if not needle or "global" in needle and needle == "global":
        return None, None
    try:
        rows = (
            db.query(_Project)
            .filter(_Project.is_deleted == False)  # noqa: E712
            .all()
        )
        for row in rows:
            name = _normalize_project_name(row.name)
            if not name:
                continue
            if _norm(name) and _norm(name) in needle:
                return str(row.id), row.name
    except Exception as e:  # noqa: BLE001 — resolution is best-effort
        logger.debug("_resolve_project_from_message failed (non-fatal): %s", e)
    return None, None


def prepare_data_source_runtime(
    db: Session,
    agent_app: AgentApp | None,
    base_tools: list[dict],
    base_system_prompt: str,
    selected_project_id: str | None = None,
    selected_project_name: str | None = None,
    pinned_data_source_id: str | None = None,
    user_id: str | None = None,
    target_context_window: int | None = None,
    user_message: str | None = None,
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
    if pinned_data_source_id and pinned_data_source_id not in bound_ids:
        pinned = db.get(KnowledgeBase, pinned_data_source_id)
        if pinned is not None and not pinned.is_deleted:
            bound_ids = list(bound_ids) + [pinned_data_source_id]

    # Guard: general_assistant must have zero data source access when no
    # project is selected AND the user message does not name a project.
    # This prevents cross-project data leakage in ungrouped chats even if
    # stale knowledge_bases or workspace auto-bind settings are present in
    # the database.  When the user explicitly names a project in the message
    # (e.g. \"make a c5 c9 market view ppt\" → C5_C9), we scope the agent to
    # THAT project's data sources — the explicit mention IS the selection,
    # and the resolved project's KBs REPLACE any stale own/auto-bindings
    # (cross-project leakage must stay impossible).
    if not selected_project_id and not _normalize_project_name(selected_project_name):
        if agent_app and getattr(agent_app, "name", None) == "general_assistant":
            _msg_pid, _msg_pname = _resolve_project_from_message(
                db, user_message,
                getattr(agent_app, "org_id", "default-org"),
                getattr(agent_app, "app_id", "default-app"),
            )
            if _msg_pid:
                selected_project_id = _msg_pid
                selected_project_name = _msg_pname
                bound_ids = _extend_with_project_kbs(
                    db, agent_app, [], _msg_pid,
                    project_name=_msg_pname,
                )
                logger.info(
                    "general_assistant: resolved project %s (%s) from "
                    "user message → %d KB(s) bound",
                    _msg_pid, _msg_pname, len(bound_ids),
                )
            else:
                bound_ids = []

    # Inject project context (description + shared memory) whenever a
    # project is selected — independent of whether the agent has any bound
    # data sources, so non-data questions are still project-aware.
    project_block = ""
    if selected_project_id:
        project_block = _build_project_context_block(
            db, selected_project_id, agent_app
        )

    # ── Demo/test source guard (2026-08-29) ─────────────────────────────
    # When BOTH a demo/test-marked source AND a real database source are
    # bound, the demo source is excluded so the LLM can never build a
    # deliverable from demo data (observed: supply-chain snapshot used
    # "Demo E2E PostgreSQL" while the real ERP was bound → user reported
    # fabricated data). Uses the KB name + database_name metadata, NOT
    # hardcoded ids. Demo-only workspaces are unaffected.
    if bound_ids:
        _kb_rows = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.id.in_(bound_ids),
                KnowledgeBase.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        _name_map = {
            kb.id: (kb.name, kb.database_name)
            for kb in _kb_rows
        }
        _filtered = exclude_demo_sources_when_real_present(bound_ids, name_map=_name_map)
        if len(_filtered) != len(bound_ids):
            logger.info(
                "demo_source_guard: excluded %d demo/test source(s) from "
                "bound set (%d → %d) — real source present; conv project=%s",
                len(bound_ids) - len(_filtered),
                len(bound_ids), len(_filtered),
                selected_project_id or selected_project_name or "<none>",
            )
        bound_ids = _filtered

    # ── Project Data Map (entity graph, flag-gated) ──
    entity_block = ""
    if selected_project_id and getattr(settings, "ENTITY_GRAPH_ENABLED", False):
        entity_block = _build_entity_map_block(db, selected_project_id)

    if not bound_ids:
        # No data sources, but still surface project context if selected.
        new_prompt = base_system_prompt + (
            "\n\n" + project_block if project_block else ""
        ) + ("\n\n" + entity_block if entity_block else "")
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
            # 2026-08-25: BUGFIX — entry.schema may be in flat form (no
            # 'function' wrapper). DeepSeek rejects this with:
            #   tools[N]: missing field `type` (status 400)
            # Normalize before adding to the tools list.
            from app.services.tool_registry import normalize_tool_schema
            augmented_tools.append(normalize_tool_schema(entry.schema, fallback_name=entry.name))
    if "fetch_data_batch" not in existing_names:
        entry = registry.get_entry("fetch_data_batch")
        if entry:
            from app.services.tool_registry import normalize_tool_schema
            augmented_tools.append(normalize_tool_schema(entry.schema, fallback_name=entry.name))

    # ── Dashboard build toolset (2026-08-27) ────────────────────────────────
    # A data-bound agent must ALSO be able to ship the deliverable the bound
    # data makes possible: the full-stack realtime dashboard. Previously the
    # data tools (ask_data_agent / fetch_data_batch) were injected but the
    # build tools were NOT — so a chat served by an agent whose
    # enabled_tools lacked create_fullstack_dashboard (e.g. the default
    # general_assistant that serves project chats) could explore the schema
    # but could never call the build tool. Every dashboard guard
    # (should_force_create_dashboard / goal-contract force / narration
    # nudge) is inert when the tool is absent, so the turn ended with a
    # "readiness assessment" report instead of a live dashboard (observed
    # conv 3e7fa92b, C5_C9 project, 2026-08-27).
    #
    # Fix: inject the full dashboard pipeline idempotently alongside the
    # data tools when the full-stack pipeline is flag-enabled. Any agent
    # with a bound datasource becomes dashboard-capable — DB-agnostic,
    # no per-agent enabled_tools edits required.
    if getattr(settings, "FULLSTACK_DASHBOARD_ENABLED", False):
        _dash_tools = [
            # Build pipeline
            "create_fullstack_dashboard",
            "update_fullstack_dashboard",
            "revert_fullstack_dashboard",
            "list_fullstack_dashboards",
            # Design intelligence (dashboard turns start with uiux_design_system)
            "uiux_design_system",
            "uiux_search",
            # Schema grounding (the dashboard guards require describe_schema
            # evidence before forcing the build)
            "list_data_sources",
            "describe_schema",
        ]
        _dash_existing = {t.get("function", {}).get("name") for t in augmented_tools}
        for _dash_name in _dash_tools:
            if _dash_name in _dash_existing:
                continue
            _dash_entry = registry.get_entry(_dash_name)
            if _dash_entry is None:
                continue
            from app.services.tool_registry import normalize_tool_schema
            augmented_tools.append(
                normalize_tool_schema(_dash_entry.schema, fallback_name=_dash_entry.name)
            )
            _dash_existing.add(_dash_name)

    bound_meta = _load_bound_kb_meta(db, bound_ids)
    schema_slices = _build_schema_slice(db, bound_ids)
    concept_catalogs = _build_concept_catalog(db, bound_ids)
    # P1-5: when the target model has a small context window, compress the
    # "Bound Data Sources" block structurally.  Default compact threshold
    # is 70,000 (anything ≤ 70k → compact; bigger models keep full).
    # Default-off when target_context_window is None (back-compat).
    _compact_max_ctx = getattr(settings, "DSR_COMPACT_MODE_MAX_CONTEXT", 70_000)
    _compact_mode = (
        target_context_window is not None
        and target_context_window <= _compact_max_ctx
    )
    section = _build_data_source_prompt_section(
        bound_meta, has_weekly_report=False, schema_slices=schema_slices,
        concept_catalogs=concept_catalogs,
        compact_mode=_compact_mode,
    )

    # PREPEND the critical anti-hallucination directive so it's the first
    # thing the LLM reads. The detailed "Bound Data Sources" section and
    # the optional "Project Context" block are appended at the end.
    new_prompt = (
        _build_anti_hallucination_directive(has_weekly_report=False)
        + "\n\n"
        + base_system_prompt
        + ("\n\n" + section if section else "")
        + ("\n\n" + project_block if project_block else "")
        + ("\n\n" + entity_block if entity_block else "")
    )

    # Carry `project_id` + `user_id` + resource scoping into the tool context
    # so handlers (create_automation, list_knowledge_bases, and the DB access
    # policy enforcement layer) can scope their results to the current project
    # and current user without needing the LLM to pass these itself.
    #
    # Resource scoping for access policies: when a project is selected the
    # policy is resolved against the PROJECT share; otherwise against the AGENT
    # share (contextual scoping — mirrors how bound KBs are resolved above).
    if selected_project_id:
        resource_type = "project"
        resource_id = selected_project_id
    elif agent_app is not None and getattr(agent_app, "id", None):
        resource_type = "agent"
        resource_id = agent_app.id
    else:
        resource_type = None
        resource_id = None

    ctx_extras: dict = {
        "bound_kb_ids": bound_ids,
        "project_id": selected_project_id,
        "user_id": user_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "catalog_meta": _load_catalog_meta(db, bound_ids),
    }

    return augmented_tools, new_prompt, ctx_extras
