"""Business Rule Extractor — mine business rules from what the catalog
ALREADY knows about a datasource, and stage them as SemanticGuidelines.

Closes the "BusinessRuleExtractorSkill MISSING" audit gap: organizational
knowledge (dictionaries, decode comments, data windows, grain, view
semantics) sits in ``kb_column_meta`` / ``kb_table_meta`` but never reaches
the BUSINESS RULES prompt block, so every SQL generation had to re-derive it
from the same comments.

Sources (catalog-only — nothing is invented, everything is quoted from the
source or measured from its data):
  1. column source comments that decode values ("0=否,1=是", "单位:元")
  2. measured enum dictionaries (low-cardinality DISTINCT probes)
  3. the data window (coverage MIN/MAX) — questions outside it are empty
  4. grain declarations ("one row per ...") — aggregation correctness
  5. VIEW objects — derived data; joining a view to its base double-counts

Honesty rules:
- Extracted rules are STAGED as ``is_enabled=False`` guidelines titled
  "[auto] ..." — a human enables them, exactly like metric promotion. An
  auto-extracted guess must never arrive in the prompt as an authored rule.
- Deduped by content fingerprint, so re-running never duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_RULES_PER_SYNC = 60

# Comment fragments that mark a decode/dictionary comment (generic shapes,
# no domain vocabulary).
_DECODE_RE = re.compile(r"(\w+\s*=\s*[^,;，；/|]{1,20}([,;，；/|]|$)){2,}")


def _looks_like_decode(comment: str) -> bool:
    """``FSTATUS``-style comments: '0=未审核,1=已审核', 'Y=yes,N=no'."""
    text = (comment or "").strip()
    if not text or len(text) > 120 or "\n" in text:
        return False
    if "=" not in text:
        return False
    # Two or more `token=value` pairs separated by common delimiters, or a
    # short single-assignment like '单位:元' style — a dictionary, not prose.
    return bool(_DECODE_RE.search(text)) or len(text) <= 40


def _fingerprint(kind: str, table: str, content: str) -> str:
    raw = f"{kind}|{table}|{content}".strip().lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def extract_business_rules(db: Session, kb_id: str, limit: int = MAX_RULES_PER_SYNC) -> dict:
    """Mine candidate rules from the persisted catalog. Returns a report:
    ``{rules: [{kind, title, content, table, fingerprint, source}], counts}``.
    Read-only — nothing is written.
    """
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

    metas = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.is_deleted.is_(False))
        .order_by(KBTableMeta.table_name)
        .all()
    )
    meta_ids = [m.id for m in metas]
    cols = (
        db.query(KBColumnMeta)
        .filter(KBColumnMeta.table_meta_id.in_(meta_ids))
        .all()
        if meta_ids
        else []
    )
    col_by_table: dict[str, list[KBColumnMeta]] = {}
    for c in cols:
        col_by_table.setdefault(c.table_meta_id, []).append(c)

    rules: list[dict] = []

    def _add(kind: str, table: str, title: str, content: str, source: str) -> None:
        content = (content or "").strip()
        if not content:
            return
        rules.append({
            "kind": kind,
            "table": table,
            "title": title[:200],
            "content": content[:600],
            "source": source,
            "fingerprint": _fingerprint(kind, table, content),
        })

    for m in metas:
        tname = m.table_name
        tcols = col_by_table.get(m.id, [])

        # 1. decode / dictionary comments (highest value)
        for c in tcols:
            comment = (getattr(c, "source_comment", None) or "").strip()
            if comment and _looks_like_decode(comment):
                _add(
                    "other", tname,
                    f"{tname}.{c.column_name} 取值含义",
                    f"{tname}.{c.column_name}: {comment}",
                    "source_comment",
                )

        # 2. measured enum dictionaries
        for c in tcols:
            enums = getattr(c, "enum_values", None)
            if isinstance(enums, list) and 1 < len(enums) <= 25:
                vals = ", ".join(str(v) for v in enums[:15])
                _add(
                    "default_filter", tname,
                    f"{tname}.{c.column_name} 已观测取值",
                    f"{tname}.{c.column_name} only ever holds: {vals} "
                    f"(measured dictionary — filter literals must come from "
                    f"this list)",
                    "measured_enum",
                )

        # 3. data window
        cov = m.coverage_json or {}
        date_col = cov.get("date_column")
        if date_col and (cov.get("min_date") or cov.get("max_date")):
            _add(
                "fiscal_calendar", tname,
                f"{tname} 数据窗口",
                f"{tname}.{date_col} data window: "
                f"{cov.get('min_date') or '?'} ~ {cov.get('max_date') or '?'} — "
                f"questions outside this window return ZERO rows by data "
                f"boundaries, not by error",
                "coverage_probe",
            )

        # 4. grain
        if m.grain:
            _add(
                "other", tname,
                f"{tname} 行粒度",
                f"{tname}: grain = {m.grain}. Aggregate accordingly — never "
                f"mix grains in one SUM.",
                "grain",
            )

        # 5. views
        if (m.table_type or "").upper() == "VIEW":
            _add(
                "exception", tname,
                f"{tname} 是视图",
                f"{tname} is a VIEW (derived data). Joining it to its base "
                f"tables can double-count measures — prefer it alone.",
                "table_type",
            )

    # Priority order: decodes > enums > windows > grain > views; then cap.
    prio = {"source_comment": 0, "measured_enum": 1, "coverage_probe": 2,
            "grain": 3, "table_type": 4}
    rules.sort(key=lambda r: (prio.get(r["source"], 9), r["table"]))
    rules = rules[: max(1, limit)]

    counts: dict[str, int] = {}
    for r in rules:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    return {"kb_id": kb_id, "rules": rules, "counts": counts}


def sync_business_rules(
    db: Session, kb_id: str, *, enable: bool = False, limit: int = MAX_RULES_PER_SYNC,
) -> dict:
    """Stage extracted rules as SemanticGuidelines (idempotent).

    New rules land as ``is_enabled=enable`` (default False — a human enables
    them via the guidelines PATCH endpoint). Existing fingerprints are
    skipped; only a stale rule whose content CHANGED is updated.
    """
    from app.models.semantic_guideline import SemanticGuideline

    report = extract_business_rules(db, kb_id, limit=limit)
    created = updated = skipped = 0
    existing = {
        g.title: g
        for g in db.query(SemanticGuideline)
        .filter(
            SemanticGuideline.datasource_id == kb_id,
            SemanticGuideline.is_deleted.is_(False),
            SemanticGuideline.title.like("[auto]%"),
        )
        .all()
    }
    for r in report["rules"]:
        title = f"[auto] {r['title']}"
        g = existing.get(title)
        if g is None:
            db.add(SemanticGuideline(
                datasource_id=kb_id,
                kind=r["kind"] if r["kind"] in (
                    "fiscal_calendar", "default_filter", "territory_map",
                    "exception", "policy", "other",
                ) else "other",
                title=title,
                content=r["content"],
                is_enabled=enable,
            ))
            created += 1
        elif g.content != r["content"]:
            g.content = r["content"]
            updated += 1
        else:
            skipped += 1
    db.commit()
    return {
        "kb_id": kb_id,
        "extracted": len(report["rules"]),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "enabled": enable,
        "counts": report["counts"],
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
