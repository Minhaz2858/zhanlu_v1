"""SchemaGraph — runtime structural view + join planning for candidate tables.

Builds a focused graph over the catalog-selected candidate tables only,
reusing ``SchemaService`` (TTL-cached introspection) for column metadata and
the connector for sample rows + row-count estimates. Join edges are loaded
from ``kb_table_relation`` (declared FK + inferred VALUE_OVERLAP/NAME_MATCH)
and rendered with kind + confidence so the LLM can reason about joins.

Zero business keywords — pure structure (types, keys, counts, samples, edges).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, KBTableRelation
from app.services.db.connector_factory import get_connector
from app.services.db.schema_service import SchemaService

logger = logging.getLogger(__name__)

# Edge kind ranking (higher wins) for get_related_tables ordering.
_EDGE_KIND_RANK = {"FK": 3, "VALUE_OVERLAP": 2, "NAME_MATCH": 1}


@dataclass
class TableNode:
    name: str
    columns: list[dict] = field(default_factory=list)
    row_count_approx: int | None = None
    sample_rows: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    # entity_master | fact | dimension | bridge | unknown — structural role
    # classified at catalog-index time (kb_table_meta.table_role) so the LLM
    # knows which tables to query FIRST (masters) vs. which to filter via them.
    table_role: str = "unknown"


def _quote_ident(conn: Any, name: str) -> str:
    if getattr(conn, "dialect", "") == "mysql":
        return f"`{name}`"
    return f'"{name}"'


def _map_dialect(db_type: str | None) -> str:
    """Map a kb.db_type to a sqlglot dialect string."""
    dt = (db_type or "").lower()
    if dt in ("postgres", "postgresql"):
        return "postgres"
    if dt in ("mysql", "mariadb"):
        return "mysql"
    if dt in ("mssql", "sqlserver", "sql_server"):
        return "tsql"
    if dt == "sqlite":
        return "sqlite"
    if dt == "oracle":
        return "oracle"
    return dt or "mysql"


class SchemaGraph:
    def __init__(self, db: Session, kb_id: str):
        self.db = db
        self.kb_id = kb_id
        self.kb: KnowledgeBase | None = None
        self.db_type: str = ""
        self.dialect: str = "mysql"
        self.nodes: dict[str, TableNode] = {}

    # -- build -----------------------------------------------------------

    def _load_kb(self) -> KnowledgeBase | None:
        kb = self.db.query(KnowledgeBase).filter(
            KnowledgeBase.id == self.kb_id,
            KnowledgeBase.is_deleted == False,
        ).first()
        return kb

    def build(self, candidate_tables: list[str]) -> "SchemaGraph":
        kb = self._load_kb()
        if not kb:
            logger.warning("schema_graph: KB %s not found", self.kb_id)
            return self
        self.kb = kb
        self.db_type = kb.db_type or ""
        self.dialect = _map_dialect(self.db_type)

        service = SchemaService(self.db)
        sample_n = max(1, settings.SCHEMA_GRAPH_SAMPLE_ROWS)
        with get_connector(kb) as conn:
            for table in candidate_tables:
                try:
                    desc = service.describe_table(self.kb_id, table)
                    columns = desc.get("columns", []) or []
                except Exception as exc:
                    logger.debug("schema_graph: describe %s failed: %s", table, exc)
                    columns = []
                self.nodes[table] = TableNode(
                    name=table,
                    columns=columns,
                    row_count_approx=self._row_count_approx(conn, table),
                    sample_rows=self._sample_rows(conn, table, sample_n),
                )
        self._load_edges()
        return self

    def _row_count_approx(self, conn: Any, table: str) -> int | None:
        try:
            if self.dialect == "mysql":
                rows = conn.execute(
                    "SELECT TABLE_ROWS FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t",
                    params={"t": table}, max_rows=1, timeout_s=5,
                )
            elif self.dialect == "postgres":
                rows = conn.execute(
                    "SELECT c.reltuples::bigint FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relname = :t",
                    params={"t": table}, max_rows=1, timeout_s=5,
                )
            else:
                return None
            if rows:
                v = next(iter(rows[0].values()), None)
                return int(v) if v is not None else None
        except Exception as exc:
            logger.debug("schema_graph: row count %s failed: %s", table, exc)
        return None

    def _sample_rows(self, conn: Any, table: str, n: int) -> list[dict]:
        try:
            return conn.execute(
                f"SELECT * FROM {_quote_ident(conn, table)} LIMIT {n}",
                max_rows=n, timeout_s=5,
            ) or []
        except Exception as exc:
            logger.debug("schema_graph: sample %s failed: %s", table, exc)
            return []

    # -- edges -----------------------------------------------------------

    def _load_edges(self) -> None:
        metas = self.db.query(KBTableMeta).filter(
            KBTableMeta.kb_id == self.kb_id,
            KBTableMeta.table_name.in_(list(self.nodes.keys())),
        ).all()
        id_to_name = {m.id: m.table_name for m in metas}
        for m in metas:
            node = self.nodes.get(m.table_name)
            if node is not None and getattr(m, "table_role", None):
                node.table_role = m.table_role
        ids = list(id_to_name.keys())
        if not ids:
            return

        rels = self.db.query(KBTableRelation).filter(
            KBTableRelation.kb_id == self.kb_id,
            or_(
                KBTableRelation.source_table_meta_id.in_(ids),
                KBTableRelation.target_table_meta_id.in_(ids),
            ),
        ).all()

        for r in rels:
            src = id_to_name.get(r.source_table_meta_id)
            tgt = id_to_name.get(r.target_table_meta_id)
            if not src or not tgt or src == tgt:
                continue
            kind = r.relation_type or "FK"
            confidence = r.confidence if r.confidence is not None else 1.0
            src_cols = r.source_columns or []
            tgt_cols = r.target_columns or []
            if src in self.nodes:
                self.nodes[src].edges.append({
                    "target_table": tgt,
                    "source_columns": src_cols,
                    "target_columns": tgt_cols,
                    "kind": kind,
                    "confidence": confidence,
                })
            if tgt in self.nodes:
                self.nodes[tgt].edges.append({
                    "target_table": src,
                    "source_columns": tgt_cols,
                    "target_columns": src_cols,
                    "kind": kind,
                    "confidence": confidence,
                })

    # -- query helpers ---------------------------------------------------

    def get_related_tables(self, table_name: str) -> list[dict]:
        """Ranked related tables: FK > VALUE_OVERLAP > NAME_MATCH, then conf."""
        node = self.nodes.get(table_name)
        if not node:
            return []
        return sorted(
            node.edges,
            key=lambda e: (
                _EDGE_KIND_RANK.get(e.get("kind"), 0),
                e.get("confidence", 0.0),
            ),
            reverse=True,
        )

    def find_master_for_fk(self, table: str, fk_col: str) -> tuple[str, str, str] | None:
        """Return ``(master_table, master_join_col, fk_col)`` if an
        ``entity_master``-role target connects via an edge whose source columns
        include ``fk_col``; else ``None``.

        Reuses ``get_related_tables`` edges + ``node.table_role ==
        "entity_master"``. Structural only — no hardcoded table/column names.
        """
        if not fk_col:
            return None
        for edge in self.get_related_tables(table):
            src_cols = edge.get("source_columns") or []
            if fk_col not in src_cols:
                continue
            target = edge.get("target_table")
            target_node = self.nodes.get(target)
            if target_node and target_node.table_role == "entity_master":
                tgt_cols = edge.get("target_columns") or []
                # Aligned lists: use the column matching fk_col's index.
                join_col = None
                try:
                    join_col = tgt_cols[src_cols.index(fk_col)]
                except (ValueError, IndexError):
                    join_col = tgt_cols[0] if tgt_cols else None
                if not join_col:
                    continue
                return (target, join_col, fk_col)
        return None

    # -- rendering -------------------------------------------------------

    def to_llm_context(
        self, focus_table: str | None = None, token_budget: int | None = None
    ) -> str:
        """Render a compact, token-budgeted graph context block.

        ``focus_table`` (if given) is rendered first; budget trimming drops
        sample rows first, then connected-table detail.
        """
        budget = token_budget or settings.SCHEMA_GRAPH_TOKEN_BUDGET
        ordered = list(self.nodes.keys())
        if focus_table and focus_table in self.nodes:
            ordered = [focus_table] + [t for t in ordered if t != focus_table]

        blocks: list[str] = []
        for t in ordered:
            blocks.append(self._render_node(self.nodes[t]))
        full = "\n\n".join(blocks)
        if self._est_tokens(full) <= budget:
            return full

        # Budget exceeded — render nodes without sample rows first.
        lean = "\n\n".join(self._render_node(self.nodes[t], with_samples=False) for t in ordered)
        if self._est_tokens(lean) <= budget:
            return lean

        # Still over — keep only focus table + edge summary.
        if focus_table and focus_table in self.nodes:
            return self._render_node(self.nodes[focus_table], with_samples=False)
        return lean[: budget * 4]

    @staticmethod
    def _est_tokens(text: str) -> int:
        return len(text) // 4

    def _render_node(self, node: TableNode, with_samples: bool = True) -> str:
        lines = [f"Table: {node.name}"]
        lines.append(f"  table_role: {node.table_role or 'unknown'}")
        if node.row_count_approx is not None:
            lines.append(f"  rows_approx: {node.row_count_approx}")
        if node.columns:
            lines.append("  columns:")
            for c in node.columns:
                name = c.get("name", "?")
                ctype = c.get("type", "?")
                pk = " PK" if c.get("pk") else ""
                lines.append(f"    - {name} {ctype}{pk}")
        if node.edges:
            lines.append("  related_tables:")
            for e in node.edges:
                src = ",".join(e.get("source_columns", []) or ["?"])
                tgt = ",".join(e.get("target_columns", []) or ["?"])
                conf = e.get("confidence", 0.0)
                lines.append(
                    f"    - {e.get('target_table')} via {src} -> {tgt} "
                    f"({e.get('kind', 'FK')}, conf={conf:.2f})"
                )
        if with_samples and node.sample_rows:
            lines.append(f"  sample_rows ({len(node.sample_rows)}):")
            for row in node.sample_rows:
                items = ", ".join(f"{k}={v}" for k, v in row.items())
                lines.append(f"    - {items}")
        return "\n".join(lines)
