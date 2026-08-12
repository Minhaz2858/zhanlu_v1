"""Entity Extractor — generic named-entity extraction from project memory.

Extracts business entities (product / customer / metric / concept /
organization / location) from ProjectMemory entries using a cheap LLM
call, then upserts them into ``project_entity`` with idempotency via
``source_ref`` (the memory ID). Optionally links entities to catalog
tables/columns via embedding similarity (confidence ≥ 0.7).

CRITICAL CONSTRAINT: the extraction prompt is fully generic — it contains
NO domain-specific examples. Entity types are universal categories only;
entity names and descriptions always come from the project's own data.

Flag-gated by ``ENTITY_GRAPH_ENABLED``. Never raises into callers.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_catalog import ProjectEntity
from app.models.project_memory import ProjectMemory
from app.services.llm_service import call_llm

logger = logging.getLogger(__name__)

VALID_ENTITY_TYPES = {"product", "customer", "metric", "concept", "organization", "location"}

_EXTRACTION_SYSTEM = (
    "You are an entity extraction system. From the provided text, extract all "
    "named entities that represent business concepts relevant to a data project. "
    "For each entity, provide:\n"
    '  - "name": the canonical name (as it appears in the text)\n'
    '  - "aliases": a list of alternative names or abbreviations\n'
    '  - "entity_type": exactly one of [product, customer, metric, concept, '
    "organization, location]\n"
    '  - "description": a brief description of what this entity means in context\n\n'
    "Rules:\n"
    "- Only extract entities that are explicitly mentioned or clearly implied.\n"
    "- Do not invent entities that are not in the text.\n"
    "- Use the most specific type. If unsure, use 'concept'.\n"
    '- Output strict JSON: {"entities": [...]}'
)


def _build_extraction_prompt(text: str) -> str:
    """Build the user-message prompt for entity extraction (generic)."""
    return f"Extract entities from the following text:\n\n{text}"


async def _extract_via_llm(memory: ProjectMemory) -> list[dict]:
    """Call the LLM to extract entities from a memory entry's content."""
    try:
        result = await call_llm(
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM},
                {"role": "user", "content": _build_extraction_prompt(memory.content)},
            ],
            temperature=0.1,
            response_json_schema={
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "aliases": {"type": "array", "items": {"type": "string"}},
                                "entity_type": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["entities"],
            },
            task_type="entity_extraction",
        )
    except Exception as e:
        logger.debug("entity_extractor: LLM call failed (non-fatal): %s", e)
        return []

    data = result.get("data") if isinstance(result, dict) else {}
    if data is None:
        data = {}
    entities = data.get("entities", []) if isinstance(data, dict) else []
    return entities if isinstance(entities, list) else []


def _upsert_entity(
    db: Session,
    *,
    project_id: str,
    name: str,
    entity_type: str,
    aliases: list[str] | None = None,
    description: str = "",
    source: str = "memory",
    source_ref: str | None = None,
) -> ProjectEntity:
    """Insert or update a ProjectEntity (idempotent on project_id + name)."""
    etype = entity_type if entity_type in VALID_ENTITY_TYPES else "concept"
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
            entity_type=etype,
            aliases=aliases or [],
            description=description,
            source=source,
            source_ref=source_ref,
        )
        db.add(row)
    else:
        # Update in place — enrich aliases/description but keep the existing type
        # if it was already set to a more specific value.
        row.entity_type = etype
        if aliases:
            existing = set(row.aliases or [])
            row.aliases = list(existing | set(aliases))
        if description:
            row.description = description
        row.source_ref = source_ref
    db.flush()
    return row


def extract_entities_sync(
    db: Session,
    memory: ProjectMemory,
    *,
    link_to_catalog: bool = False,
    kb_id: str | None = None,
) -> list[ProjectEntity]:
    """Extract entities from a single ProjectMemory entry (sync, never raises).

    Idempotent: if entities with ``source_ref == memory.id`` already exist,
    the memory is considered processed and an empty list is returned.

    Args:
        db: SQLAlchemy session.
        memory: the ProjectMemory entry to process.
        link_to_catalog: if True, link entities to catalog tables via embeddings.
        kb_id: the KB whose catalog collection to link against.

    Returns:
        list of created/updated ProjectEntity rows (empty if skipped/failed).
    """
    if not getattr(settings, "ENTITY_GRAPH_ENABLED", False):
        return []

    # Idempotency: skip if this memory was already processed
    existing = (
        db.query(ProjectEntity)
        .filter(
            ProjectEntity.project_id == memory.project_id,
            ProjectEntity.source_ref == memory.id,
            ProjectEntity.is_deleted == False,  # noqa: E712
        )
        .limit(1)
        .first()
    )
    if existing is not None:
        return []

    import asyncio

    try:
        loop = asyncio.new_event_loop()
        try:
            raw_entities = loop.run_until_complete(_extract_via_llm(memory))
        finally:
            loop.close()
    except Exception as e:
        logger.debug("entity_extractor: extraction failed (non-fatal): %s", e)
        return []

    created: list[ProjectEntity] = []
    for ent in raw_entities:
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        try:
            row = _upsert_entity(
                db,
                project_id=memory.project_id,
                name=name,
                entity_type=ent.get("entity_type", "concept"),
                aliases=ent.get("aliases") or [],
                description=ent.get("description", ""),
                source="memory",
                source_ref=memory.id,
            )
            created.append(row)
        except Exception as e:
            logger.debug("entity_extractor: upsert failed for '%s': %s", name, e)

    if created and link_to_catalog and kb_id:
        try:
            _link_entities_to_catalog(db, memory.project_id, kb_id, created)
        except Exception as e:
            logger.debug("entity_extractor: catalog linking failed (non-fatal): %s", e)

    return created


def _link_entities_to_catalog(
    db: Session,
    project_id: str,
    kb_id: str,
    entities: list[ProjectEntity],
    *,
    confidence_threshold: float = 0.7,
) -> None:
    """Link entities to catalog tables via embedding similarity.

    Uses the catalog Chroma collection to find tables whose descriptions
    match each entity's name + description. Creates ProjectEntityLink rows
    for matches above the confidence threshold.
    """
    from app.models.knowledge_catalog import ProjectEntityLink
    from app.services.document_ingestion.store import _get_client
    from app.services.rag.hybrid_retrieval import hybrid_query_collection

    try:
        client = _get_client()
        coll = client.get_collection(f"catalog_{kb_id}")
    except Exception as e:
        logger.debug("entity_extractor: catalog collection unavailable: %s", e)
        return

    for ent in entities:
        query_text = f"{ent.name} {ent.description or ''}"
        if ent.aliases:
            query_text += " " + " ".join(ent.aliases)
        try:
            hits = hybrid_query_collection(coll, query_text, top_k=5)
        except Exception:
            continue

        for doc_id, score in hits:
            # Normalize score to [0, 1] confidence (RRF scores are small)
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

            existing_link = (
                db.query(ProjectEntityLink)
                .filter(
                    ProjectEntityLink.entity_id == ent.id,
                    ProjectEntityLink.target_type == "table",
                    ProjectEntityLink.target_id == target_id,
                    ProjectEntityLink.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if existing_link is None:
                db.add(ProjectEntityLink(
                    entity_id=ent.id,
                    target_type="table",
                    target_id=target_id,
                    confidence=confidence,
                    source="embedding",
                ))
    db.flush()


def extract_for_project(
    db: Session,
    project_id: str,
    *,
    limit: int = 50,
    kb_id: str | None = None,
) -> int:
    """Process unprocessed ProjectMemory entries for a project.

    Returns the count of newly extracted entities. Intended as a background
    job (flag-gated, never raises).
    """
    if not getattr(settings, "ENTITY_GRAPH_ENABLED", False):
        return 0

    try:
        from app.models.project_memory import ProjectMemory

        memories = (
            db.query(ProjectMemory)
            .filter(
                ProjectMemory.project_id == project_id,
                ProjectMemory.is_deleted == False,  # noqa: E712
            )
            .order_by(ProjectMemory.created_date.desc())
            .limit(limit)
            .all()
        )

        total = 0
        for mem in memories:
            entities = extract_entities_sync(db, mem, link_to_catalog=bool(kb_id), kb_id=kb_id)
            total += len(entities)
        db.commit()
        return total
    except Exception as e:
        logger.debug("entity_extractor: project batch failed (non-fatal): %s", e)
        return 0


__all__ = [
    "extract_entities_sync",
    "extract_for_project",
    "VALID_ENTITY_TYPES",
]
