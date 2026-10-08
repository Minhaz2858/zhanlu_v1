"""Coverage expansion + self-check — fixing the 25-vs-215 failure (2026-09-22).

Measured live: "How many contracts expire this month?" was answered with 25
(purchase contracts, erp_t_pur_contract) while the warehouse held 215 — 190
sales contracts sat in erp_t_crm_contract, a table table retrieval never
selected. Nothing in the architecture asked "are there OTHER tables about
this entity?". The interactive agent the user compared against found all of
them because it iterates.

Two layers, both generic (name/token based — zero per-database code):

* **Selection expansion** (before SQL generation): a NON-selected catalog
  table whose name shares an entity token with a selected table is pulled
  into the selection, so both contract tables at least appear in the DDL
  slice and the model may UNION them.
* **Uncovered siblings** (after execution): the tables the executed SQL
  never touched but that share an entity token with the tables it DID
  touch. The self-check turns this list into bounded supplementary queries
  (see nl_answer_service) instead of silently answering from a partial
  entity set.

Rules this module never breaks:

* **Token hygiene** — generic prefix noise (erp/t/v/bd/…) is dropped, so
  "shares a token" means a real domain word (contract, order, material).
* **Bounded** — additions are capped; the DDL slice and the supplementary
  loop can never balloon.
* **Fail-soft** — every DB error returns the input unchanged; coverage
  must never be the reason a turn fails.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Name parts that carry no entity meaning. Kept MINIMAL and structural:
#: erp/t/v/bd are inventory prefixes; domain parts (crm, pur, sal) stay.
_STOP_TOKENS = frozenset({
    "erp", "t", "v", "tb", "vw", "tmp", "temp", "zz", "test", "bak",
    "view", "table", "data", "info",
})

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")

def _strip_inventory_prefix(name: str) -> str:
    """erp_t_crm_contract -> 'crm_contract' (leading inventory prefixes off)."""
    return re.sub(r"^(?:erp_|t_|v_|vw_|tb_)+", "", (name or "").lower())


def _same_family(sib: str, touched: list[str]) -> bool:
    """True when the sibling's name is a string EXTENSION of a touched
    table's name (or vice versa) — i.e. a header/detail pair of the SAME
    family. ERP tables concatenate words (crm_contractentry), so token
    splitting cannot see 'entry'; substring extension can.
    Measured 2026-09-22: crm_contractentry/-f pushed erp_t_pur_contract out
    of the self-check, so the answer stayed "sales contracts only"."""
    sc = _strip_inventory_prefix(sib)
    if not sc:
        return False
    for t in touched:
        tc = _strip_inventory_prefix(t)
        if tc and sc != tc and (sc.startswith(tc) or tc.startswith(sc)):
            return True
    return False


def entity_tokens(table_name: str) -> set[str]:
    """Meaningful name parts of a table name, lowercased.

    ``erp_t_crm_contract`` → {crm, contract}; the prefix noise is dropped so
    two contract tables share the token 'contract' regardless of their module
    prefix (crm / pur / sal).
    """
    parts = [p for p in _TOKEN_SPLIT_RE.split((table_name or "").lower()) if p]
    return {p for p in parts if p not in _STOP_TOKENS and len(p) >= 2}


def _shared_token(a: str, b: str) -> bool:
    return bool(entity_tokens(a) & entity_tokens(b))


def expand_entity_selection(
    db: Session | None,
    kb_id: str,
    selected: list[str] | None,
    *,
    scored_candidates: list[tuple[str, float]] | None = None,
    max_added: int = 2,
) -> tuple[list[str], list[str]]:
    """Pull entity siblings of the selected tables into the selection.

    Args:
        db: optional session — when ``scored_candidates`` is not given, the
            full catalog table list for the KB is loaded from it.
        kb_id: strictly kb-scoped.
        selected: the selector's table names.
        scored_candidates: optional ``(table_name, score)`` ranking (from
            table retrieval) — expansions are preferred in score order.
        max_added: cap on added tables.

    Returns ``(expanded_selection, added_names)``. Never raises.
    """
    try:
        tables = [str(t).strip() for t in (selected or []) if t]
        if not tables or not kb_id:
            return tables, []
        sel = set(tables)

        if scored_candidates:
            pool = [
                (str(n).strip(), float(sc))
                for n, sc in scored_candidates
                if str(n).strip()
            ]
        elif db is not None:
            from app.models.knowledge_catalog import KBTableMeta

            pool = [
                (r.table_name, 1.0)
                for r in db.query(KBTableMeta.table_name)
                .filter(KBTableMeta.kb_id == kb_id)
                .all()
            ]
        else:
            return tables, []

        added: list[str] = []
        for cand_name, score in sorted(pool, key=lambda x: -x[1]):
            if len(added) >= max_added:
                break
            if cand_name in sel:
                continue
            if any(_shared_token(cand_name, s) for s in tables):
                sel.add(cand_name)
                added.append(cand_name)
                logger.info(
                    "coverage: entity sibling %r added to the selection "
                    "(shares an entity token with %s)",
                    cand_name, tables,
                )
        return tables + added, added
    except Exception as e:
        logger.debug("coverage: selection expansion skipped (fail-soft): %s", e)
        return list(selected or []), []


def find_uncovered_siblings(
    db: Session,
    kb_id: str,
    touched_tables: list[str] | None,
    *,
    max_tables: int = 2,
) -> list[str]:
    """Catalog tables sharing an entity token with the touched tables but
    NEVER touched by the executed SQL — the coverage self-check's target
    list. Bounded and fail-soft; empty on any problem."""
    try:
        touched = [str(t).strip() for t in (touched_tables or []) if t]
        if not touched or not kb_id:
            return []
        touched_tokens: set[str] = set()
        for t in touched:
            touched_tokens |= entity_tokens(t)
        if not touched_tokens:
            return []

        from app.models.knowledge_catalog import KBTableMeta

        # Base tables only: views frequently UNION/aggregate the base tables
        # and would double-count them in the supplementary snapshot
        # (measured 2026-09-22: erp_v_contract inflated 190+25 to 287).
        names = [
            r[0]
            for r in db.query(KBTableMeta.table_name)
            .filter(
                KBTableMeta.kb_id == kb_id,
                KBTableMeta.table_type == "TABLE",
            )
            .all()
        ]
        touched_set = {t.lower() for t in touched}
        sibs: list[str] = []
        for name in names:
            if not name or name.lower() in touched_set:
                continue
            toks = entity_tokens(name)
            if not (toks & touched_tokens):
                continue
            # Skip same-family header/detail pairs (crm_contractentry vs
            # erp_t_crm_contract) — they add no new entity family.
            if _same_family(name, touched):
                continue
            sibs.append(name)
            if len(sibs) >= max_tables:
                break
        return sibs
    except Exception as e:
        logger.debug("coverage: uncovered-sibling lookup skipped: %s", e)
        return []
