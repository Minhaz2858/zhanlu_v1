"""ProjectKnowledgeCache -- the unified facade.

Wraps existing tables (project_entity, project_entity_link,
project_catalog_overlay, project_metric, kb_table_meta, resource_registry)
and exposes a layered query API to the agent loop.

All operations are strictly project-scoped. Every layer fails open.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_catalog import (
    KBTableMeta,
    ProjectCatalogOverlay,
    ProjectEntity,
    ProjectEntityLink,
    ProjectMetric,
)

from .entity_linker import tokenize_for_match
from .models import CacheQueryResult, CacheStats, CacheStatus

logger = logging.getLogger(__name__)

# Try to import the config-driven graph resolver (Layer 1).
try:
    from app.services.knowledge_graph.resolver import resolve_product_id as _kg_resolve  # type: ignore
    from app.services.knowledge_graph.graph import (  # type: ignore
        query_upstream, query_downstream, query_chain,
    )
    _LAYER1_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    _LAYER1_AVAILABLE = False
    logger.debug("project_knowledge.cache: Layer 1 resolver unavailable")


def _is_qwen_model(model_id: str | None) -> bool:
    """Detect whether ``model_id`` matches any of the Qwen prefix list."""
    if not model_id:
        return False
    prefixes = getattr(settings, "QWEN_FAST_PATH_MODEL_PREFIXES", ["qwen", "Qwen"]) or []
    mid = model_id.lower()
    return any(p.lower() in mid for p in prefixes)


def is_qwen_model(model_id: str | None) -> bool:
    return _is_qwen_model(model_id)


class ProjectKnowledgeCache:
    """Per-project cache facade. All ops scoped to ``project_id``."""

    def __init__(self, project_id: str):
        if not project_id or not isinstance(project_id, str):
            raise PermissionError("ProjectKnowledgeCache requires a non-empty project_id")
        self.project_id = project_id

    # Write path
    def ingest(self, db: Session, kb_id: str) -> CacheStatus:
        """Idempotent ingestion: catalog index + entity seed + link + registry.

        Returns a CacheStatus describing what was done. Never raises.
        """
        from .ingestion import ingest_for_project  # local to avoid cycle
        return ingest_for_project(self.project_id, kb_id, db)

    # Read path -- layered lookup
    def query(
        self,
        db: Session,
        question: str,
        *,
        model_id: str | None = None,
    ) -> CacheQueryResult | None:
        """Layered lookup; first non-None wins. Returns None on full miss."""
        if not getattr(settings, "PROJECT_KNOWLEDGE_CACHE_ENABLED", False):
            return None
        if not question or not question.strip():
            return None

        # Layer 1: config-driven product resolver
        try:
            r1 = self._layer1_resolver(question)
            if r1 is not None:
                return r1
        except Exception as e:
            logger.debug("cache.query Layer1 failed (fallthrough): %s", e)

        # Layer 2: project entities
        if getattr(settings, "PROJECT_KNOWLEDGE_LAYER_ENTITIES_ENABLED", True):
            try:
                r2 = self._layer2_entities(db, question)
                if r2 is not None:
                    return r2
            except Exception as e:
                logger.debug("cache.query Layer2 failed (fallthrough): %s", e)

        # Layer 3: project metrics
        if getattr(settings, "PROJECT_KNOWLEDGE_LAYER_METRICS_ENABLED", True):
            try:
                r3 = self._layer3_metrics(db, question)
                if r3 is not None:
                    return r3
            except Exception as e:
                logger.debug("cache.query Layer3 failed (fallthrough): %s", e)

        return None

    # Invalidation
    def invalidate(
        self,
        db: Session,
        scope: Literal["all", "links", "metrics"] = "all",
    ) -> int:
        """Delete cache rows for this project. Returns deleted count."""
        deleted = 0
        if scope in ("all", "links"):
            try:
                entity_ids = [
                    e.id for e in db.query(ProjectEntity).filter(
                        ProjectEntity.project_id == self.project_id,
                        ProjectEntity.is_deleted == False,  # noqa: E712
                    ).all()
                ]
                if entity_ids:
                    rows = db.query(ProjectEntityLink).filter(
                        ProjectEntityLink.entity_id.in_(entity_ids),
                        ProjectEntityLink.is_deleted == False,  # noqa: E712
                    ).all()
                    for r in rows:
                        r.is_deleted = True
                        deleted += 1
            except Exception as e:
                logger.warning("cache.invalidate links failed: %s", e)
        if scope == "all":
            # scope=all also soft-deletes the ProjectEntity rows themselves,
            # so the next ingest() will re-seed them from the domain config.
            try:
                rows = db.query(ProjectEntity).filter(
                    ProjectEntity.project_id == self.project_id,
                    ProjectEntity.is_deleted == False,  # noqa: E712
                ).all()
                for r in rows:
                    r.is_deleted = True
                    deleted += 1
            except Exception as e:
                logger.warning("cache.invalidate entities failed: %s", e)
        if scope in ("all", "metrics"):
            try:
                rows = db.query(ProjectMetric).filter(
                    ProjectMetric.project_id == self.project_id,
                    ProjectMetric.is_deleted == False,  # noqa: E712
                ).all()
                for r in rows:
                    r.is_deleted = True
                    deleted += 1
            except Exception as e:
                logger.warning("cache.invalidate metrics failed: %s", e)
        if scope == "all":
            try:
                rows = db.query(ProjectCatalogOverlay).filter(
                    ProjectCatalogOverlay.project_id == self.project_id,
                    ProjectCatalogOverlay.is_deleted == False,  # noqa: E712
                ).all()
                for r in rows:
                    r.is_deleted = True
                    deleted += 1
            except Exception as e:
                logger.warning("cache.invalidate overlays failed: %s", e)
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        return deleted

    # Stats
    def stats(self, db: Session) -> CacheStats:
        s = CacheStats(project_id=self.project_id)
        try:
            entities = db.query(ProjectEntity).filter(
                ProjectEntity.project_id == self.project_id,
                ProjectEntity.is_deleted == False,  # noqa: E712
            ).all()
            s.entities = len(entities)
            eids = [e.id for e in entities]
            if eids:
                s.links = db.query(ProjectEntityLink).filter(
                    ProjectEntityLink.entity_id.in_(eids),
                    ProjectEntityLink.is_deleted == False,  # noqa: E712
                ).count()
            else:
                s.links = 0
            s.metrics = db.query(ProjectMetric).filter(
                ProjectMetric.project_id == self.project_id,
                ProjectMetric.is_deleted == False,  # noqa: E712
            ).count()
            s.overlays = db.query(ProjectCatalogOverlay).filter(
                ProjectCatalogOverlay.project_id == self.project_id,
                ProjectCatalogOverlay.is_deleted == False,  # noqa: E712
            ).count()
            # catalog tables for any kb bound to this project
            try:
                from app.models.agent_app import AgentApp
                kb_ids: set[str] = set()
                for app in db.query(AgentApp).filter(AgentApp.is_deleted == False).all():  # noqa: E712
                    kbs = app.knowledge_bases or []
                    if isinstance(kbs, list):
                        for k in kbs:
                            if isinstance(k, str):
                                kb_ids.add(k)
                if kb_ids:
                    s.catalog_tables = db.query(KBTableMeta).filter(
                        KBTableMeta.kb_id.in_(kb_ids),
                        KBTableMeta.is_deleted == False,  # noqa: E712
                    ).count()
            except Exception:
                pass
        except Exception as e:
            logger.debug("cache.stats failed (partial): %s", e)
        return s

    # Layer implementations
    def _layer1_resolver(self, question: str) -> CacheQueryResult | None:
        if not _LAYER1_AVAILABLE:
            return None
        try:
            pid = _kg_resolve(question)
        except Exception:
            return None
        if not pid:
            return None
        try:
            upstream = query_upstream(pid)
            downstream = query_downstream(pid)
            chain = query_chain(pid, "downstream")
        except Exception as e:
            logger.debug("Layer1 query_upstream/downstream failed: %s", e)
            upstream = {"upstream": []}
            downstream = {"downstream": []}
            chain = {"downstream_chains": [], "substitutes": []}
        upstream_ids = [p.get("id") for p in upstream.get("upstream", [])]
        downstream_ids = [p.get("id") for p in downstream.get("downstream", [])]
        context_block = (
            f"[Project Knowledge Cache -- Layer 1 hit]\n"
            f"Product: {pid}\n"
            f"Upstream: {upstream_ids}\n"
            f"Downstream: {downstream_ids}\n"
            f"Chains: {len(chain.get('downstream_chains', []))} path(s)\n"
            f"Substitutes: {len(chain.get('substitutes', []))}\n"
        )
        return CacheQueryResult(
            kind="product",
            data={
                "product_id": pid,
                "upstream": upstream.get("upstream", []),
                "downstream": downstream.get("downstream", []),
                "chains": chain.get("downstream_chains", []),
                "substitutes": chain.get("substitutes", []),
            },
            context_block=context_block,
            confidence=1.0,
        )

    def _layer2_entities(self, db: Session, question: str) -> CacheQueryResult | None:
        tokens = tokenize_for_match(question)
        if not tokens:
            return None
        entities = (
            db.query(ProjectEntity)
            .filter(
                ProjectEntity.project_id == self.project_id,
                ProjectEntity.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        hits: list[tuple[ProjectEntity, int]] = []
        for ent in entities:
            needles = [ent.name.lower()] + [a.lower() for a in (ent.aliases or []) if a]
            score = 0
            for tok in tokens:
                if any(tok in n for n in needles):
                    score += 1
            if score > 0:
                hits.append((ent, score))
        if not hits:
            return None
        hits.sort(key=lambda x: -x[1])
        hits = hits[:3]
        top_ids = [h[0].id for h in hits]
        link_rows: list[ProjectEntityLink] = []
        if top_ids:
            link_rows = (
                db.query(ProjectEntityLink)
                .filter(
                    ProjectEntityLink.entity_id.in_(top_ids),
                    ProjectEntityLink.is_deleted == False,  # noqa: E712
                )
                .all()
            )
        links_by_entity: dict[str, list[dict]] = {}
        for lr in link_rows:
            links_by_entity.setdefault(lr.entity_id, []).append({
                "target_type": lr.target_type,
                "target_id": lr.target_id,
                "confidence": lr.confidence,
                "source": lr.source,
            })
        ents_payload = [
            {
                "id": e.id,
                "name": e.name,
                "aliases": e.aliases or [],
                "entity_type": e.entity_type,
                "description": e.description or "",
                "source": e.source,
                "links": links_by_entity.get(e.id, []),
            }
            for e, _ in hits
        ]
        top = hits[0][0]
        context_block = (
            f"[Project Knowledge Cache -- Layer 2 hit]\n"
            f"Top entity: {top.name} (type={top.entity_type}, "
            f"source={top.source}, links={len(links_by_entity.get(top.id, []))})\n"
        )
        return CacheQueryResult(
            kind="entity",
            data={"entities": ents_payload},
            context_block=context_block,
            confidence=0.8,
        )

    def _layer3_metrics(self, db: Session, question: str) -> CacheQueryResult | None:
        tokens = tokenize_for_match(question)
        if not tokens:
            return None
        metrics = (
            db.query(ProjectMetric)
            .filter(
                ProjectMetric.project_id == self.project_id,
                ProjectMetric.status == "approved",
                ProjectMetric.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        best: tuple[ProjectMetric, int] | None = None
        for m in metrics:
            needles = [m.name.lower()] + [a.lower() for a in (m.aliases or []) if a]
            score = sum(1 for tok in tokens if any(tok in n for n in needles))
            if score > 0 and (best is None or score > best[1]):
                best = (m, score)
        if best is None:
            return None
        m, _ = best
        context_block = (
            f"[Project Knowledge Cache -- Layer 3 hit]\n"
            f"Metric: {m.name} (unit={m.unit or 'unknown'}, "
            f"agg={m.default_aggregation or 'unknown'})\n"
        )
        return CacheQueryResult(
            kind="metric",
            data={
                "name": m.name,
                "aliases": m.aliases or [],
                "definition": m.definition or "",
                "sql_expression": m.sql_expression or "",
                "unit": m.unit,
                "default_aggregation": m.default_aggregation,
                "bindings": m.bindings or [],
            },
            context_block=context_block,
            confidence=0.85,
        )

    # Qwen fast-path helper
    @staticmethod
    def is_qwen_model(model_id: str | None) -> bool:
        return _is_qwen_model(model_id)


__all__ = ["ProjectKnowledgeCache", "is_qwen_model"]
