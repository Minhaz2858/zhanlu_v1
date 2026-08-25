"""C5/C9 product entity seeding and catalog linking.

Promotes the 12 static C5/C9 product nodes into per-project
``project_entity`` rows and links each to the best matching
``kb_table_meta`` row via embedding similarity (ChromaDB hybrid).

Idempotent -- re-running just refreshes timestamps and link confidence.
Failures are logged and never raised; layer 2 stays functional even
when the embedding service is unavailable.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_catalog import (
    KBTableMeta,
    ProjectEntity,
    ProjectEntityLink,
)

logger = logging.getLogger(__name__)


# Layer 1 data -- the 12 C5/C9 products
# Sourced from app.services.knowledge_graph.graph._C5_C9_NODES so the
# domain graph stays the single source of truth.
_C5_C9_SEED_PRODUCTS: list[dict[str, Any]] = [
    {"id": "crude_oil",    "name": "Crude Oil",    "name_cn": "原油"},
    {"id": "naphtha",      "name": "Naphtha",      "name_cn": "石脑油"},
    {"id": "cracked_c5",   "name": "Cracked C5",   "name_cn": "裂解C5"},
    {"id": "mixed_c5",     "name": "Mixed C5",     "name_cn": "混合碳五"},
    {"id": "isoprene",     "name": "Isoprene",     "name_cn": "异戊二烯"},
    {"id": "piperylene",   "name": "Piperylene",   "name_cn": "间戊二烯"},
    {"id": "dcpd",         "name": "DCPD",         "name_cn": "双环戊二烯"},
    {"id": "sis",          "name": "SIS",          "name_cn": "苯乙烯-异戊二烯-苯乙烯"},
    {"id": "cracked_c9",   "name": "Cracked C9",   "name_cn": "裂解C9"},
    {"id": "styrene",      "name": "Styrene",      "name_cn": "苯乙烯"},
    {"id": "c5_resin",     "name": "C5 Resin",     "name_cn": "C5石油树脂"},
    {"id": "raffinate_c5", "name": "Raffinate C5", "name_cn": "抽余C5"},
    {"id": "blowing_agent","name": "Blowing Agent","name_cn": "发泡剂"},
]


def seed_products_as_entities(db: Session, project_id: str) -> int:
    """Insert/update 12 C5/C9 product rows in ``project_entity``.

    Idempotent on (project_id, name). Sets source='c5c9_seed'.
    Returns count of rows upserted.
    """
    if not getattr(settings, "ECISCO_BI_KG_PROJECT_CACHE_ENABLED", False):
        return 0
    if not project_id:
        return 0
    count = 0
    for p in _C5_C9_SEED_PRODUCTS:
        name = p["name"]
        aliases = [p["id"], p["name_cn"]]
        try:
            row = (
                db.query(ProjectEntity)
                .filter(
                    ProjectEntity.project_id == project_id,
                    ProjectEntity.name == name,
                    ProjectEntity.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if row is None:
                row = ProjectEntity(
                    project_id=project_id,
                    name=name,
                    aliases=aliases,
                    entity_type="product",
                    description=f"C5/C9 product: {p['name_cn']} ({p['id']})",
                    source="c5c9_seed",
                )
                db.add(row)
            else:
                # enrich aliases but never widen the entity_type
                existing = set(row.aliases or [])
                row.aliases = list(existing | set(aliases))
                if row.source != "c5c9_seed":
                    row.source = "c5c9_seed"
            count += 1
        except Exception as e:
            logger.warning("entity_linker.seed failed for %s/%s: %s", project_id, name, e)
    try:
        db.flush()
    except Exception as e:
        logger.warning("entity_linker.seed flush failed: %s", e)
    return count


def _try_embedding_link(
    db: Session,
    project_id: str,
    kb_id: str,
    entity: ProjectEntity,
    confidence_threshold: float = 0.7,
) -> int:
    """Link one entity to its best-matching catalog table via Chroma.

    Returns 1 if a link was created, 0 otherwise. Never raises.
    """
    try:
        from app.services.document_ingestion.store import _get_client
        from app.services.rag.hybrid_retrieval import hybrid_query_collection

        client = _get_client()
        coll = client.get_collection(f"catalog_{kb_id}")
    except Exception as e:
        logger.debug("entity_linker: catalog collection unavailable: %s", e)
        return 0

    query_text = f"{entity.name} {entity.description or ''}"
    if entity.aliases:
        query_text += " " + " ".join(entity.aliases)
    try:
        hits = hybrid_query_collection(coll, query_text, top_k=3)
    except Exception:
        return 0

    for doc_id, score in hits:
        confidence = min(1.0, score * 10.0)
        if confidence < confidence_threshold:
            continue
        try:
            meta = coll.get(ids=[doc_id], include=["metadatas"])
            m = (meta.get("metadatas") or [{}])[0] if meta else {}
        except Exception:
            continue
        if m.get("kind") != "table":
            continue
        target_id = m.get("table_name", "")
        if not target_id:
            continue
        existing = (
            db.query(ProjectEntityLink)
            .filter(
                ProjectEntityLink.entity_id == entity.id,
                ProjectEntityLink.target_type == "table",
                ProjectEntityLink.target_id == target_id,
                ProjectEntityLink.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if existing is not None:
            existing.confidence = confidence
            continue
        db.add(ProjectEntityLink(
            entity_id=entity.id,
            target_type="table",
            target_id=target_id,
            confidence=confidence,
            source="embedding",
        ))
        return 1
    return 0


def _fallback_name_link(
    db: Session,
    project_id: str,
    kb_id: str,
    entity: ProjectEntity,
) -> int:
    """Rule-based link: find catalog tables whose name/description mentions
    any of the entity's aliases (subproduct_id, cn name). Confidence=0.55.
    """
    needles = [entity.name.lower()]
    for a in (entity.aliases or []):
        if a:
            needles.append(a.lower())
    needles = [n for n in needles if len(n) >= 3]
    if not needles:
        return 0

    tables = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.is_deleted == False)  # noqa: E712
        .all()
    )
    best: tuple[KBTableMeta, int] | None = None
    for t in tables:
        hay_parts = [(t.table_name or "").lower()]
        for attr in ("description_zh", "description_en"):
            v = getattr(t, attr, None)
            if v:
                hay_parts.append(v.lower())
        hay = " ".join(hay_parts)
        score = sum(1 for n in needles if n in hay)
        if score > 0 and (best is None or score > best[1]):
            best = (t, score)
    if best is None:
        return 0
    t, _ = best
    existing = (
        db.query(ProjectEntityLink)
        .filter(
            ProjectEntityLink.entity_id == entity.id,
            ProjectEntityLink.target_type == "table",
            ProjectEntityLink.target_id == t.table_name,
            ProjectEntityLink.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if existing is not None:
        existing.confidence = max(existing.confidence, 0.55)
        return 0
    db.add(ProjectEntityLink(
        entity_id=entity.id,
        target_type="table",
        target_id=t.table_name,
        confidence=0.55,
        source="name_match",
    ))
    return 1


def link_entities_to_catalog_for_project(
    db: Session, project_id: str, kb_id: str
) -> int:
    """Embed each C5/C9-seeded entity and link to best catalog table.

    Falls back to name-match (lower confidence 0.55) if embedding service
    is unavailable. Returns count of links created/updated.
    """
    if not getattr(settings, "ECISCO_BI_KG_PROJECT_CACHE_ENABLED", False):
        return 0
    if not getattr(settings, "ECISCO_BI_KG_LAYER_ENTITIES_ENABLED", True):
        return 0

    entities = (
        db.query(ProjectEntity)
        .filter(
            ProjectEntity.project_id == project_id,
            ProjectEntity.source == "c5c9_seed",
            ProjectEntity.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    count = 0
    for ent in entities:
        # 1. Try embedding-based link
        created = _try_embedding_link(db, project_id, kb_id, ent, confidence_threshold=0.7)
        if created == 0:
            # 2. Fallback: name match
            created = _fallback_name_link(db, project_id, kb_id, ent)
        count += created
    try:
        db.flush()
    except Exception as e:
        logger.warning("entity_linker.link flush failed: %s", e)
    return count


def tokenize_for_match(text: str) -> list[str]:
    """CJK-safe tokenization: returns lowercased ASCII words + CJK 2..6-grams.

    Used by cache.query() Layer 2 / Layer 3 matching. Never uses \\b around
    CJK; uses 2..6-grams which are inherently word-boundary-free.
    """
    if not text:
        return []
    text = text.lower()
    # ASCII words
    ascii_tokens = re.findall(r"[a-z0-9_]+", text)
    # CJK 2..6-grams
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    cjk_grams: list[str] = []
    for word in cjk:
        for n in (2, 3, 4, 5, 6):
            for i in range(0, max(1, len(word) - n + 1)):
                cjk_grams.append(word[i:i + n])
    return ascii_tokens + cjk_grams


__all__ = [
    "seed_products_as_entities",
    "link_entities_to_catalog_for_project",
    "tokenize_for_match",
]
