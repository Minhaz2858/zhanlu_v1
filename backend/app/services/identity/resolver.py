"""P3 — cross-source identity resolution (crosswalks, never merges).

Four strictly separated tiers:
1. exact            — lossless normalization only (trim/case/int↔str);
                      `1001` vs `C1001` NEVER matches here
2. normalization    — a SMALL closed transform set applied to one side,
                      valid ONLY with recorded evidence (transformed
                      value-set containment ≥ threshold); the rule is
                      re-validated against live samples each run
3. semantic         — multi-attribute embedding agreement → human review
4. probabilistic    — weaker single-attribute signals → human review

Rejections are remembered: a pair with status 'rejected' is never
re-proposed. Sources are read-only and bounded; nothing merges.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.models.identity import IdentityMatch

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 500
DEFAULT_MAX_MATCHES = 500

# Tier-2 closed transform set — anything outside this set is NOT allowed.
_NORMALIZERS = {
    "strip_alpha_prefix": lambda v: re.sub(r"^[A-Za-z]+", "", v),
    "strip_leading_zeros": lambda v: v.lstrip("0") or "0",
    "numeric_suffix": (lambda v: (re.search(r"(\d+)$", v) or [None, v])[1]
                       if re.search(r"(\d+)$", v) else v),
}
_NORMALIZATION_THRESHOLD = 0.9
_MIN_EVIDENCE_PAIRS = 3

# Tier-3/4 thresholds.
_SEMANTIC_COSINE = 0.85
_SEMANTIC_MIN_ATTRIBUTES = 2
_PROB_COSINE_MIN = 0.60

_SOURCE_KEY_MAX = 250


def _source_key(source: dict) -> str:
    if source.get("kind") == "file":
        key = f"file:{source.get('kb_id')}:{source.get('sheet')}"
    else:
        key = f"db:{source.get('kb_id')}:{source.get('table')}"
    return key[:_SOURCE_KEY_MAX]


def fetch_source_values(db: Session, org_id: str, source: dict,
                        column: str | None = None,
                        limit: int = DEFAULT_MAX_ROWS) -> list[str]:
    """Bounded, read-only distinct values of ONE column from a source.
    The column may be passed directly or inside `source`. DB sources go
    through QueryService (org-checked); file sources read the P3a staging.
    Returns [] on anything unavailable — honest."""
    column = column or source.get("column")
    from app.models.identity import FileTableRow
    from app.models.knowledge_base import KnowledgeBase

    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == source.get("kb_id"),
                KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if not kb or (kb.org_id or "default-org") != org_id:
        return []  # cross-org is a leak, not a feature

    if source.get("kind") == "file":
        sheet = source.get("sheet") or ""
        rows = (
            db.query(FileTableRow)
            .filter(
                FileTableRow.kb_id == source["kb_id"],
                FileTableRow.sheet_name == sheet,
                FileTableRow.is_deleted == False,  # noqa: E712
            )
            .order_by(FileTableRow.row_idx)
            .limit(limit)
            .all()
        )
        return [r.values_json.get(column) for r in rows
                if r.values_json and r.values_json.get(column) is not None]

    # DB source: bounded distinct sample through the read-only service.
    from app.services.db.query_service import QueryService

    table = source.get("table") or ""
    if not re.match(r"^[A-Za-z0-9_.]+$", table) or \
            not re.match(r"^[A-Za-z0-9_]+$", column):
        return []
    try:
        result = QueryService(db).execute(
            source["kb_id"],
            f"SELECT DISTINCT {column} FROM {table}",
            max_rows=limit,
        )
        return [str(r[column]) for r in (result.get("rows") or [])
                if r.get(column) is not None]
    except Exception as exc:  # noqa: BLE001 — honest failure
        logger.warning("identity sample read failed (%s.%s): %s",
                       table, column, exc)
        return []


def fetch_source_rows(db: Session, org_id: str, source: dict,
                      columns: list[str], limit: int = DEFAULT_MAX_ROWS) -> list[dict]:
    """Bounded row fetch (pk + attribute columns) for tier 3/4."""
    from app.models.identity import FileTableRow
    from app.models.knowledge_base import KnowledgeBase

    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == source.get("kb_id"),
                KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if not kb or (kb.org_id or "default-org") != org_id:
        return []

    if source.get("kind") == "file":
        sheet = source.get("sheet") or ""
        rows = (
            db.query(FileTableRow)
            .filter(
                FileTableRow.kb_id == source["kb_id"],
                FileTableRow.sheet_name == sheet,
                FileTableRow.is_deleted == False,  # noqa: E712
            )
            .order_by(FileTableRow.row_idx)
            .limit(limit)
            .all()
        )
        return [dict(r.values_json or {}) for r in rows
                if r.values_json and any(r.values_json.get(c) for c in columns)]

    from app.services.db.query_service import QueryService

    table = source.get("table") or ""
    if not re.match(r"^[A-Za-z0-9_.]+$", table):
        return []
    cols = [c for c in columns if re.match(r"^[A-Za-z0-9_]+$", c)]
    if not cols:
        return []
    try:
        result = QueryService(db).execute(
            source["kb_id"],
            f"SELECT {', '.join(cols)} FROM {table}",
            max_rows=limit,
        )
        return [dict(r) for r in (result.get("rows") or [])]
    except Exception as exc:  # noqa: BLE001 — honest failure
        logger.warning("identity row read failed (%s): %s", table, exc)
        return []


# ── Lossless normalization (tier 1) ─────────────────────────────────────


def _lossless(v) -> str:
    """Trim, case-fold, int↔str unify. NOTHING else — a prefix like
    'C1002' is a DIFFERENT identifier and must never match here."""
    s = str(v).strip().lower()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Module-level indirection so tests can stub deterministically.
    Uses the local embedding model (no external API)."""
    from app.services.embeddings import get_embeddings_batch

    return [v for v in get_embeddings_batch(texts)]


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db_ = math.sqrt(sum(y * y for y in b))
    return num / (da * db_) if da and db_ else 0.0


def _record_match(db: Session, org_id: str, object_id: str, source_a: dict,
                  source_b: dict, pk_a: str, pk_b: str, tier: str, *,
                  status: str = "confirmed", confidence: float | None = None,
                  rule: dict | None = None, evidence: dict | None = None,
                  reviewed_by: str | None = None) -> IdentityMatch | None:
    """Idempotent crosswalk writer: never overwrites an existing decision
    (confirmed stays confirmed, rejected is REMEMBERED)."""
    existing = (
        db.query(IdentityMatch)
        .filter(
            IdentityMatch.org_id == org_id,
            IdentityMatch.object_id == object_id,
            IdentityMatch.source_a_key == _source_key(source_a),
            IdentityMatch.pk_a == pk_a,
            IdentityMatch.source_b_key == _source_key(source_b),
            IdentityMatch.pk_b == pk_b,
        )
        .first()
    )
    if existing is not None:
        return None  # decision already recorded — never re-propose
    m = IdentityMatch(
        org_id=org_id, object_id=object_id,
        source_a=source_a, source_b=source_b,
        source_a_key=_source_key(source_a), source_b_key=_source_key(source_b),
        pk_a=pk_a, pk_b=pk_b, match_tier=tier, status=status,
        normalization_rule_json=rule, confidence=confidence,
        evidence_json=evidence, reviewed_by=reviewed_by,
    )
    db.add(m)
    return m


def _rows_for_source(db: Session, org_id: str, source: dict, columns: list[str],
                     limit: int) -> list[dict]:
    pk = source.get("pk_column") or source.get("column")
    cols = list(dict.fromkeys([pk] + list(columns)))
    rows = fetch_source_rows(db, org_id, source, cols, limit)
    for r in rows:
        r.setdefault("_pk", str(r.get(pk)) if r.get(pk) is not None
                     else f"row:{rows.index(r)}")
    return rows


def resolve_object_identity(
    db: Session, org_id: str, object_id: str, source_a: dict, source_b: dict,
    *, attribute_pairs: list[tuple[str, str]] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_matches: int = DEFAULT_MAX_MATCHES,
) -> dict:
    """Run tiers 1→4 for ONE object across TWO sources. Bounded, read-only.
    `attribute_pairs` maps comparable columns across the two sources
    [(col_in_a, col_in_b), ...]; tiers 3/4 only run when it is provided —
    we never silently embed arbitrary columns.
    Returns an honest per-tier match-count report."""
    report = {"object_id": object_id, "tier1": 0, "tier2": 0, "tier3": 0,
              "tier4": 0, "skipped_existing": 0, "capped": False}

    id_a = source_a.get("column")
    id_b = source_b.get("column")
    if not id_a or not id_b:
        report["error"] = "both sources need an identifier column"
        return report

    # Tier 3/4 only run when the caller explicitly pairs comparable
    # attribute columns — we never silently embed arbitrary columns.
    pairs = [(a, b) for a, b in (attribute_pairs or []) if a and b]

    rows_a = _rows_for_source(db, org_id, source_a,
                              [id_a] + [p[0] for p in pairs], max_rows)
    rows_b = _rows_for_source(db, org_id, source_b,
                              [id_b] + [p[1] for p in pairs], max_rows)
    by_norm_a: dict[str, list[dict]] = {}
    for r in rows_a:
        if r.get(id_a) is not None:
            by_norm_a.setdefault(_lossless(r[id_a]), []).append(r)
    by_norm_b: dict[str, list[dict]] = {}
    for r in rows_b:
        if r.get(id_b) is not None:
            by_norm_b.setdefault(_lossless(r[id_b]), []).append(r)

    consumed_a: set[str] = set()

    def record(tier: str, ra: dict, rb: dict, *, status: str = "confirmed",
               confidence: float | None = None, rule: dict | None = None,
               evidence: dict | None = None, reviewed_by: str | None = None):
        if report["tier1"] + report["tier2"] + report["tier3"] + \
                report["tier4"] >= max_matches:
            report["capped"] = True
            return None
        m = _record_match(
            db, org_id, object_id, source_a, source_b,
            str(ra["_pk"]), str(rb["_pk"]), tier, status=status,
            confidence=confidence, rule=rule, evidence=evidence,
            reviewed_by=reviewed_by,
        )
        if m is None:
            report["skipped_existing"] += 1
        return m

    # ── Tier 1: exact (lossless) ────────────────────────────────────────
    for norm, a_rows in by_norm_a.items():
        b_rows = by_norm_b.get(norm)
        if not b_rows:
            continue
        for ra in a_rows:
            for rb in b_rows:
                if record("exact", ra, rb, confidence=1.0,
                          reviewed_by="system:exact",
                          evidence={"method": "exact_id",
                                    "normalized_value": norm,
                                    "columns": [id_a, id_b]}) is not None:
                    report["tier1"] += 1
        consumed_a.add(norm)

    # ── Tier 2: evidence-backed normalization (closed transform set) ───
    # The transform may be needed in EITHER direction (CRM ids bare,
    # master ids prefixed — or vice versa). Whichever direction produces
    # qualifying containment wins; the rule records the direction.
    residual_norms = [n for n in by_norm_a if n not in consumed_a]
    residual_b_norms = [n for n in by_norm_b if n not in consumed_a]
    chosen = None  # (transform_name, direction, rule)
    for tname, tfn in _NORMALIZERS.items():
        # direction a→b: transform A residuals, compare to B as-is
        transformed_a = {tfn(n): n for n in residual_norms}
        shared = {t for t in transformed_a if t in by_norm_b}
        containment = (len(shared) / len(residual_norms)
                       if residual_norms else 0.0)
        if containment >= _NORMALIZATION_THRESHOLD and \
                len(shared) >= _MIN_EVIDENCE_PAIRS:
            chosen = (tname, "a_to_b", {
                "transform": tname,
                "direction": "a_to_b",
                "threshold": _NORMALIZATION_THRESHOLD,
                "containment": round(containment, 4),
                "shared_pairs": len(shared),
                "revalidated_each_run": True,
            })
            break
        # direction b→a: transform B values, compare to A residuals as-is.
        # NOTE: containment must test the TRANSFORMED keys against A's norms.
        transformed_b = {tfn(n): n for n in residual_b_norms}
        shared = {t for t in transformed_b if t in by_norm_a}
        containment = (len(shared) / len(residual_b_norms)
                       if residual_b_norms else 0.0)
        if containment >= _NORMALIZATION_THRESHOLD and \
                len(shared) >= _MIN_EVIDENCE_PAIRS:
            chosen = (tname, "b_to_a", {
                "transform": tname,
                "direction": "b_to_a",
                "threshold": _NORMALIZATION_THRESHOLD,
                "containment": round(containment, 4),
                "shared_pairs": len(shared),
                "revalidated_each_run": True,
            })
            break
    if chosen:
        tname, direction, chosen_rule = chosen
        tfn = _NORMALIZERS[tname]
        if direction == "a_to_b":
            candidate_pairs = [
                (ra, rb, norm, tfn(norm))
                for norm in residual_norms
                for ra in by_norm_a[norm]
                for rb in by_norm_b.get(tfn(norm), [])
            ]
        else:
            candidate_pairs = [
                (ra, rb, norm_b, tfn(norm_b))
                for norm_b in residual_b_norms
                for rb in by_norm_b[norm_b]
                for ra in by_norm_a.get(tfn(norm_b), [])
            ]
        for ra, rb, original, transformed in candidate_pairs:
            m = record("normalization", ra, rb,
                       confidence=chosen_rule["containment"],
                       rule=chosen_rule,
                       reviewed_by="system:normalization",
                       evidence={"method": "evidence_backed_normalization",
                                 "original_value": original,
                                 "matched_value": transformed,
                                 "rule": chosen_rule})
            if m is not None:
                report["tier2"] += 1

    # ── Tier 3/4: embedding similarity → HUMAN review (never auto) ─────
    if pairs and residual_norms:
        rows_a = [r for n in residual_norms for r in by_norm_a[n]]
        rows_b = [r for rs in by_norm_b.values() for r in rs]
        pairs_text_a = [" | ".join(str(r.get(p[0]) or "") for p in pairs)
                        for r in rows_a]
        pairs_text_b = [" | ".join(str(r.get(p[1]) or "") for p in pairs)
                        for r in rows_b]
        try:
            vecs_a = _embed_texts(pairs_text_a)
            vecs_b = _embed_texts(pairs_text_b)
        except Exception as exc:  # noqa: BLE001 — honest degrade
            logger.warning("identity embedding failed: %s", exc)
            vecs_a, vecs_b = [], []
        used_a: set[int] = set()
        for ia, ra in enumerate(rows_a):
            if ia in used_a or ia >= len(vecs_a):
                continue
            best: tuple[float, int] = (0.0, -1)
            for ib, rb in enumerate(rows_b):
                if ib >= len(vecs_b):
                    continue
                sim = _cosine(vecs_a[ia], vecs_b[ib])
                if sim > best[0]:
                    best = (sim, ib)
            if best[1] < 0:
                continue
            sim, ib = best
            agreements = sum(
                1 for p in pairs
                if _lossless(rows_a[ia].get(p[0]) or "") ==
                _lossless(rows_b[ib].get(p[1]) or "")
            )
            if agreements >= _SEMANTIC_MIN_ATTRIBUTES and \
                    sim >= _SEMANTIC_COSINE:
                tier, conf = "semantic", round(sim, 4)
            elif sim >= _PROB_COSINE_MIN:
                tier, conf = "probabilistic", round(sim, 4)
            else:
                continue
            m = record(tier, rows_a[ia], rows_b[ib], status="inferred",
                       confidence=conf,
                       evidence={"method": f"{tier}_similarity",
                                 "attribute_pairs": pairs,
                                 "attribute_agreements": agreements,
                                 "cosine": round(sim, 4)})
            if m is not None:
                report[f"tier{3 if tier == 'semantic' else 4}"] += 1
                used_a.add(ia)

    return report


def mark_review(db: Session, org_id: str, match_id: str, decision: str,
                reviewed_by: str | None = None) -> IdentityMatch:
    """org-admin confirm/reject on a crosswalk. Audited by the endpoint."""
    if decision not in ("confirm", "reject"):
        raise ValueError("decision must be confirm or reject")
    m = (
        db.query(IdentityMatch)
        .filter(IdentityMatch.id == match_id,
                IdentityMatch.org_id == org_id)
        .first()
    )
    if m is None:
        raise ValueError("match not found")
    from datetime import datetime, timezone

    m.status = "confirmed" if decision == "confirm" else "rejected"
    m.reviewed_by = reviewed_by
    m.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return m
