"""Join-partner guarantee — retrieval hardening for JOIN questions (2026-09-22).

OCI "Table Retrieval" says retrieval must select "likely tables, fields and
validated join paths BEFORE SQL generation starts". The measured failure on
non-Ecisco warehouses: a question like "sales by customer" retrieves only the
sales table; the customer table never enters the DDL slice; the model then
invents a join to a table it never saw (or filters on a column that does not
exist) — the turn fails or fabricates.

The catalog already knows table relations (declared FKs confidence 1.0,
inferred VALUE_OVERLAP / NAME_MATCH edges lower). This module closes the gap
between what the catalog knows and what the selector picked: when a selected
table has a relation whose partner was NOT selected, the best partner is
pulled into the selection before the DDL slice is built.

Rules this module never breaks:

* **Never removes a table** — only adds partners. The selector's choices and
  every upstream guard (coverage reroute, static route) stay intact.
* **Bounded** — at most ``max_added`` partners per call, so a hub table
  cannot balloon the DDL slice.
* **Confidence floor** — relations below ``min_confidence`` are ignored
  outright; ``min_confidence`` is a hard, caller-tunable floor. Lower it to
  admit weak edges (e.g. NAME_MATCH / VALUE_OVERLAP), raise it to require
  only declared FKs. There is no implicit fallback to a below-floor edge.
* **Fail-soft** — any DB error returns the selection unchanged; the guarantee
  must never be the reason a turn fails.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy.orm import Session, aliased

logger = logging.getLogger(__name__)


def ensure_join_partners(
    db: Session,
    kb_id: str,
    selected_tables: list[str] | None,
    *,
    min_confidence: float = 0.5,
    max_added: int = 2,
) -> tuple[list[str], list[str]]:
    """Pull missing relation partners into a table selection.

    Args:
        db: session.
        kb_id: the knowledge base whose relations apply (strictly kb-scoped).
        selected_tables: the selector's table names (garbage entries dropped).
        min_confidence: relations below this are ignored.
        max_added: upper bound on partners added per call.

    Returns ``(expanded_selection, added_partners)``. Never raises.
    """
    try:
        tables = [str(t).strip() for t in (selected_tables or []) if t]
        if not tables or not kb_id:
            return tables, []
        sel = set(tables)

        from app.models.knowledge_catalog import KBTableMeta, KBTableRelation

        Src = aliased(KBTableMeta)
        Tgt = aliased(KBTableMeta)
        rows = (
            db.query(KBTableRelation, Src.table_name, Tgt.table_name)
            .join(Src, KBTableRelation.source_table_meta_id == Src.id)
            .join(Tgt, KBTableRelation.target_table_meta_id == Tgt.id)
            .filter(KBTableRelation.kb_id == kb_id)
            .all()
        )
        if not rows:
            return tables, []

        adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for rel, src_name, tgt_name in rows:
            conf = float(rel.confidence or 0.0)
            if conf < min_confidence:
                continue
            adjacency[src_name].append((tgt_name, conf))
            adjacency[tgt_name].append((src_name, conf))

        added: list[str] = []
        for table in tables:
            if len(added) >= max_added:
                break
            edges = adjacency.get(table) or []
            if not edges:
                continue  # no knowledge at all — nothing to add
            if any(p in sel for p, _ in edges):
                continue  # already has an in-selection partner
            best_name, _conf = max(edges, key=lambda e: e[1])
            sel.add(best_name)
            added.append(best_name)
            logger.info(
                "join_partner: %s had no selected partner — pulled in %r",
                table, best_name,
            )

        out = tables + [a for a in added if a not in set(tables)]
        return out, added
    except Exception as e:
        logger.debug("join_partner: guarantee skipped (fail-soft): %s", e)
        return list(selected_tables or []), []
