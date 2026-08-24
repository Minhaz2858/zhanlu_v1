"""Deterministic query-purpose classification for the v3 agent loop.

Every query result is tagged before it can feed the deliverable:

    probe      — shape/coverage probes (bare aggregates, tiny LIMIT samples,
                 metadata-only rows, empty results). Never a deliverable.
    auxiliary  — reference lookups / supporting context (entity masters,
                 dimensions, bridges). Recorded, never the deliverable.
    answer     — business data rows that may feed the deliverable + synthesis.

Rules (deterministic, database-agnostic, no LLM):
1. Shape-based probe rules run first and override everything
   (bare MIN/MAX/COUNT aggregates without GROUP BY; small LIMIT samples
   without WHERE/GROUP BY; ``is_metadata_only_rows``; empty results).
2. Otherwise the catalog role decides: any referenced table whose role is
   ``fact`` → answer; all-non-fact roles (entity_master / dimension /
   bridge) → auxiliary.
3. Fail-open: unknown role (or no role info at all) → answer, so a missing
   catalog entry never blocks a legitimate deliverable.

TableRoleResolver memoizes per-turn catalog role lookup (KBTableMeta +
per-project ProjectCatalogOverlay override, overlay wins).
"""

from __future__ import annotations

import logging
import re
from typing import List, Mapping, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.knowledge_catalog import KBTableMeta, ProjectCatalogOverlay
from app.services.goal_contract import (
    extract_tables_from_sql,
    is_effective_empty,
    is_metadata_only_rows,
)

logger = logging.getLogger(__name__)

# ── Purpose vocabulary ───────────────────────────────────────────────────
PROBE = "probe"
AUXILIARY = "auxiliary"
ANSWER = "answer"

Purpose = str  # Literal["probe", "auxiliary", "answer"]

# Catalog roles that are never the primary answer data on their own.
NON_ANSWER_ROLES = {"entity_master", "dimension", "bridge"}
# Roles that make a query an answer. ``unknown`` is deliberately included
# (fail-open: a missing catalog entry never blocks a deliverable).
ANSWER_ROLES = {"fact", "unknown"}

# Small LIMIT cap: a query fetching at most this many rows WITHOUT a WHERE
# filter is a sampling probe, not an answer.
PROBE_LIMIT_ROWS = 20

_SELECT_FROM_RE = re.compile(r"\bselect\s+(.*?)\bfrom\b", re.IGNORECASE | re.DOTALL)
_GROUP_BY_RE = re.compile(r"\bgroup\s+by\b", re.IGNORECASE)
_WHERE_RE = re.compile(r"\bwhere\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)", re.IGNORECASE)
# A SELECT list where EVERY comma-separated element is a bare aggregate.
_BARE_AGG_SELECT_RE = re.compile(
    r"^\s*(?:count\s*\(\s*(?:\*\s*)?\)|"
    r"(?:min|max|sum|avg|count)\s*\([^)]*\)\s*)"
    r"(?:\s*,\s*(?:count\s*\(\s*(?:\*\s*)?\)|"
    r"(?:min|max|sum|avg|count)\s*\([^)]*\)\s*))*\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _is_bare_aggregate_select(sql: str) -> bool:
    """True when the SELECT list contains ONLY aggregate functions and the
    query has no GROUP BY (a freshness/coverage probe, not business rows)."""
    if _GROUP_BY_RE.search(sql):
        return False
    m = _SELECT_FROM_RE.search(sql)
    if not m:
        return False
    return bool(_BARE_AGG_SELECT_RE.match(m.group(1)))


def _is_small_limit_sample(sql: str) -> bool:
    """True for a tiny LIMIT sample with no WHERE / GROUP BY / aggregation —
    the classic 'take a quick look at the table' probe."""
    if _WHERE_RE.search(sql) or _GROUP_BY_RE.search(sql):
        return False
    m = _LIMIT_RE.search(sql)
    if not m:
        return False
    try:
        return int(m.group(1)) <= PROBE_LIMIT_ROWS
    except ValueError:
        return False


def _is_single_column_sample(sql: str) -> bool:
    """True for a SINGLE bare column SELECT with a LIMIT and no WHERE /
    GROUP BY / aggregation — an ID/name lookup sample, never a business
    answer. Production shape (2026-08-21): ``SELECT product_id FROM
    some_table LIMIT 80`` returned 80 bare IDs; the small-
    LIMIT rule (cap 20) missed it and the unknown-role fail-open tagged it
    ``answer``, which would have built a garbage one-column report card.
    A single column cannot carry a business answer (no measure × dimension
    pair), and the explicit LIMIT marks it as a sample regardless of size.
    """
    if _WHERE_RE.search(sql) or _GROUP_BY_RE.search(sql):
        return False
    if not _LIMIT_RE.search(sql):
        return False
    m = _SELECT_FROM_RE.search(sql)
    if not m:
        return False
    select_list = m.group(1)
    # Single projection element only (no comma-separated list), and not an
    # aggregate (COUNT/MIN/MAX/SUM/AVG are handled by the bare-aggregate
    # rule, but keep this rule self-contained).
    if "," in select_list:
        return False
    if re.search(r"\b(?:count|min|max|sum|avg)\s*\(", select_list, re.IGNORECASE):
        return False
    return bool(select_list.strip())


def classify_query_purpose(
    sql: Optional[str],
    rows: Optional[Sequence[dict]],
    table_roles: Mapping[str, str],
) -> Purpose:
    """Tag a query result as probe / auxiliary / answer.

    Args:
        sql: the SQL text (may be None for non-SQL sources).
        rows: the query result rows (may be None / empty).
        table_roles: table_name -> catalog role (from TableRoleResolver).

    Returns:
        "probe" | "auxiliary" | "answer"
    """
    # 1) Shape-based probes override everything (deterministic, no catalog).
    if is_metadata_only_rows(rows) or is_effective_empty(rows):
        return PROBE
    if sql:
        if _is_bare_aggregate_select(sql):
            return PROBE
        if _is_small_limit_sample(sql):
            return PROBE
        if _is_single_column_sample(sql):
            return PROBE

    # 2) Role-based decision from catalog metadata.
    tables = extract_tables_from_sql(sql)
    if not tables:
        # No parseable table reference → fail-open answer (rows have signal).
        return ANSWER

    roles = {t: (table_roles.get(t) or "unknown") for t in tables}
    if any(roles.get(t) in ANSWER_ROLES for t in tables):
        return ANSWER
    return AUXILIARY


class TableRoleResolver:
    """Per-turn memoized catalog role lookup.

    Resolves ``kb_table_meta.table_role`` for the turn's KBs, overridden by
    per-project ``ProjectCatalogOverlay.table_role`` (overlay wins). Role
    lookups are loaded once per resolver instance (one indexed query per KB),
    then served from memory — safe to construct once per agent turn.
    """

    def __init__(
        self,
        db: Session,
        kb_ids: Optional[Sequence[str]] = None,
        project_id: Optional[str] = None,
    ) -> None:
        self.db = db
        self.kb_ids = list(kb_ids or [])
        self.project_id = project_id
        self._roles: Optional[dict] = None

    def _load(self) -> dict:
        roles: dict = {}
        if self.kb_ids:
            metas = (
                self.db.query(KBTableMeta)
                .filter(
                    KBTableMeta.kb_id.in_(self.kb_ids),
                    KBTableMeta.is_deleted.is_(False),
                )
                .all()
            )
            for m in metas:
                if m.table_name:
                    roles[m.table_name] = m.table_role or "unknown"
        if self.project_id:
            overlays = (
                self.db.query(ProjectCatalogOverlay)
                .filter(
                    ProjectCatalogOverlay.project_id == self.project_id,
                    ProjectCatalogOverlay.table_role.isnot(None),
                    ProjectCatalogOverlay.is_deleted.is_(False),
                )
                .all()
            )
            for o in overlays:
                if o.table_name and o.table_role:
                    roles[o.table_name] = o.table_role
        return roles

    def roles_for(self, tables: Sequence[str]) -> dict:
        """Resolve ``{table_name: role}`` for the given tables (unknown →
        ``"unknown"``). Case-insensitive fallback for safety."""
        if self._roles is None:
            self._roles = self._load()
        out: dict = {}
        for t in tables:
            role = self._roles.get(t)
            if role is None:
                role = self._roles.get(t.lower())
            out[t] = role or "unknown"
        return out
