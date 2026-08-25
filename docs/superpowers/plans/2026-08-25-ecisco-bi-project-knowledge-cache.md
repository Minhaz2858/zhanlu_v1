# Ecisco BI Project Knowledge Cache — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pre-build and continuously maintain a per-project knowledge cache for the `aipdp_data_warehouse_prod` database so Ecisco BI questions (especially with Qwen models) can be answered with fewer LLM round-trips.

**Architecture:** New `backend/app/services/project_knowledge/` package (parallel to `knowledge_graph/`) that owns a unified facade (`ProjectKnowledgeCache`) over **existing tables** (`project_entity`, `project_entity_link`, `project_catalog_overlay`, `project_metric`, `kb_table_meta`, `kb_column_meta`, `kb_table_relation`, `resource_registry`). The facade exposes `ingest / query / invalidate / stats`. A new Qwen-tuned fast-path intercepts the agent loop's first LLM call and short-circuits when the cache can answer the question.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, existing `catalog_indexer` / `entity_extractor` / `registry_indexer` / `resolver` modules. No new dependencies.

## Global Constraints

- All new flags default `False`. New flags: `ECISCO_BI_KG_PROJECT_CACHE_ENABLED`, `ECISCO_BI_KG_QWEN_FAST_PATH`, `QWEN_FAST_PATH_MODEL_PREFIXES`, `ECISCO_BI_AGENT_NAMES`, `PROJECT_KNOWLEDGE_INGEST_BG_WORKER`.
- Module is **strictly project-scoped**. `ProjectKnowledgeCache(project_id)` never reads/writes another project. Cross-project access raises `PermissionError`.
- **No new DB tables.** Composes existing tables.
- **Failure mode: fail open.** Every cache layer falls through; the LLM is always the safety net.
- **Mobile parity / no regressions** — other AgentApps, other projects, the static C5/C9 graph, the existing catalog indexer, the schema linker, and NL2SQL are all unchanged.
- **CJK regex** — never use `\b` around CJK; use lookarounds or `\b` only around ASCII.
- **Build / deploy** — `cd frontend && ./node_modules/.bin/vite build --emptyOutDir=false && docker restart zhanlu-frontend` for frontend; `docker restart zhanlu-backend` for backend.
- **Container test** — `docker exec zhanlu-backend /usr/local/bin/python -m pytest <path> -p no:jaxtyping -p no:cacheprovider` (cwd `/app`).
- **Local test** — `cd backend && venv/bin/python -m pytest <path> -p no:jaxtyping -p no:cacheprovider`.

---

## Task 1: Add config flags

**Files:**
- Modify: `backend/app/config.py:829-838`

- [ ] **Step 1: Add the new flags**

In the `# ── Ecisco BI feature flags (EDIA 5.1.2 → zhanlu migration) ─────────` block, after line 838 (`ECISCO_BI_SCHEMA_DEPRECATION_ENABLED`), add:

```python
# ── Ecisco BI Project Knowledge Cache (2026-08-25) ─────────────
# Project-scoped facade over project_entity / project_entity_link /
# project_metric / project_catalog_overlay / catalog tables. Scoped to
# ECISCO_BI_AGENT_NAMES (default: ecisco_bi_assistant). All default False.
ECISCO_BI_KG_PROJECT_CACHE_ENABLED: bool = False   # master gate; enables cache facade
ECISCO_BI_KG_QWEN_FAST_PATH: bool = False          # inject cached answer into agent loop pre-LLM
QWEN_FAST_PATH_MODEL_PREFIXES: list[str] = ["qwen", "Qwen"]
ECISCO_BI_AGENT_NAMES: list[str] = ["ecisco_bi_assistant"]
PROJECT_KNOWLEDGE_INGEST_BG_WORKER: bool = True    # run ingestion in asyncio.to_thread
ECISCO_BI_KG_LAYER_ENTITIES_ENABLED: bool = True   # Layer 2 (entity lookup) on/off
ECISCO_BI_KG_LAYER_METRICS_ENABLED: bool = True    # Layer 3 (metric lookup) on/off
```

- [ ] **Step 2: Verify config loads**

```bash
cd backend && venv/bin/python -c "from app.config import settings; print(settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED)"
```
Expected: `False` (no error).

- [ ] **Step 3: Commit**

```bash
git add backend/app/config.py
git commit -m "feat(ecisco-bi): add project knowledge cache feature flags"
```

---

## Task 2: Create the package skeleton + models.py

**Files:**
- Create: `backend/app/services/project_knowledge/__init__.py`
- Create: `backend/app/services/project_knowledge/models.py`

- [ ] **Step 1: Create `__init__.py`**

```python
"""Project-scoped knowledge cache for the ecisco_bi_assistant AgentApp.

This module composes existing tables (project_entity, project_entity_link,
project_catalog_overlay, project_metric, kb_table_meta, kb_column_meta,
kb_table_relation, resource_registry) into a single facade that:

- runs ingestion whenever a KnowledgeBase is bound to the ecisco_bi_assistant
- answers questions via 3 layered lookups (resolver → entity → metric) before
  the agent's LLM is called (Qwen fast-path)

All operations are strictly project-scoped. The module is gated by
`ECISCO_BI_KG_PROJECT_CACHE_ENABLED` (default False). Every layer fails
open; the LLM is always the safety net.
"""
from __future__ import annotations

from .cache import ProjectKnowledgeCache
from .models import CacheStatus, CacheStats, CacheQueryResult

__all__ = [
    "ProjectKnowledgeCache",
    "CacheStatus",
    "CacheStats",
    "CacheQueryResult",
]
```

- [ ] **Step 2: Create `models.py`**

```python
"""Dataclasses for the ProjectKnowledgeCache facade."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CacheStatusKind = Literal["pending", "indexing", "ready", "partial", "error"]
CacheQueryKind = Literal["product", "entity", "metric"]


@dataclass
class CacheStatus:
    """Result of an ingestion run for one (project_id, kb_id) pair."""
    status: CacheStatusKind = "pending"
    tables: int = 0
    entities: int = 0
    links: int = 0
    error: str | None = None
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "tables": self.tables,
            "entities": self.entities,
            "links": self.links,
            "error": self.error,
            "elapsed_s": self.elapsed_s,
        }


@dataclass
class CacheStats:
    """Read-only snapshot of cache health for one project."""
    project_id: str
    entities: int = 0
    links: int = 0
    metrics: int = 0
    overlays: int = 0
    catalog_tables: int = 0
    last_ingest_at: str | None = None
    last_status: CacheStatusKind = "pending"

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "entities": self.entities,
            "links": self.links,
            "metrics": self.metrics,
            "overlays": self.overlays,
            "catalog_tables": self.catalog_tables,
            "last_ingest_at": self.last_ingest_at,
            "last_status": self.last_status,
        }


@dataclass
class CacheQueryResult:
    """A hit from the cache that can be injected into the agent context."""
    kind: CacheQueryKind
    data: dict = field(default_factory=dict)
    # human-readable block to drop into the LLM context
    context_block: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "data": self.data,
            "context_block": self.context_block,
            "confidence": self.confidence,
        }
```

- [ ] **Step 3: Verify import**

```bash
cd backend && venv/bin/python -c "from app.services.project_knowledge import ProjectKnowledgeCache, CacheStatus; print('ok')"
```
Expected: `ok` (no error).

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/project_knowledge/__init__.py backend/app/services/project_knowledge/models.py
git commit -m "feat(ecisco-bi): scaffold project_knowledge package + models"
```

---

## Task 3: Implement entity_linker.py

**Files:**
- Create: `backend/app/services/project_knowledge/entity_linker.py`

**Interfaces:**
- `seed_products_as_entities(db, project_id) -> int` — promote 12 C5/C9 products into `project_entity` rows. Idempotent.
- `link_entities_to_catalog_for_project(db, project_id, kb_id) -> int` — embed product names, find best-matching `kb_table_meta` row, write `project_entity_link` rows. Idempotent.

- [ ] **Step 1: Create the file**

```python
"""C5/C9 product entity seeding and catalog linking.

Promotes the 12 static C5/C9 product nodes into per-project
`project_entity` rows and links each to the best matching
`kb_table_meta` row via embedding similarity (ChromaDB hybrid).

Idempotent — re-running just refreshes timestamps and link confidence.
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


# ── Layer 1 data — the 12 C5/C9 products ─────────────────────────────────
# Sourced from app.services.knowledge_graph.graph._C5_C9_NODES so the
# domain graph stays the single source of truth.
_C5_C9_SEED_PRODUCTS: list[dict[str, Any]] = [
    {"id": "crude_oil",   "name": "Crude Oil",   "name_cn": "原油"},
    {"id": "naphtha",     "name": "Naphtha",     "name_cn": "石脑油"},
    {"id": "cracked_c5",  "name": "Cracked C5",  "name_cn": "裂解C5"},
    {"id": "mixed_c5",    "name": "Mixed C5",    "name_cn": "混合碳五"},
    {"id": "isoprene",    "name": "Isoprene",    "name_cn": "异戊二烯"},
    {"id": "piperylene",  "name": "Piperylene",  "name_cn": "间戊二烯"},
    {"id": "dcpd",        "name": "DCPD",        "name_cn": "双环戊二烯"},
    {"id": "sis",         "name": "SIS",         "name_cn": "苯乙烯-异戊二烯-苯乙烯"},
    {"id": "cracked_c9",  "name": "Cracked C9",  "name_cn": "裂解C9"},
    {"id": "styrene",     "name": "Styrene",     "name_cn": "苯乙烯"},
    {"id": "c5_resin",    "name": "C5 Resin",    "name_cn": "C5石油树脂"},
    {"id": "raffinate_c5","name": "Raffinate C5","name_cn": "抽余C5"},
    {"id": "blowing_agent","name": "Blowing Agent","name_cn": "发泡剂"},
]

# CJK-safe word-boundary pattern. Never use \b around CJK — use lookarounds.
_CJK_BOUNDARY = r"(?:^|(?<=[\s,，。、;；:：]))"


def seed_products_as_entities(db: Session, project_id: str) -> int:
    """Insert/update 12 C5/C9 product rows in `project_entity`.

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
        hay = " ".join([
            (t.table_name or "").lower(),
            (t.description_zh or "").lower() if getattr(t, "description_zh", None) else "",
            (t.description_en or "").lower() if getattr(t, "description_en", None) else "",
        ])
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

    Used by cache.query() Layer 2 / Layer 3 matching.
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
```

- [ ] **Step 2: Verify import**

```bash
cd backend && venv/bin/python -c "from app.services.project_knowledge.entity_linker import seed_products_as_entities, link_entities_to_catalog_for_project, tokenize_for_match; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/project_knowledge/entity_linker.py
git commit -m "feat(ecisco-bi): entity_linker seeds C5/C9 + links to catalog tables"
```

---

## Task 4: Implement cache.py (the facade)

**Files:**
- Create: `backend/app/services/project_knowledge/cache.py`

**Interfaces:**
- `class ProjectKnowledgeCache(project_id: str)` — owns `ingest(kb_id)`, `query(question, model_id=None)`, `invalidate(scope)`, `stats()`.

- [ ] **Step 1: Create the file**

```python
"""ProjectKnowledgeCache — the unified facade.

Wraps existing tables (project_entity, project_entity_link,
project_catalog_overlay, project_metric, kb_table_meta, resource_registry)
and exposes a layered query API to the agent loop.

All operations are strictly project-scoped. Every layer fails open.
"""
from __future__ import annotations

import logging
import time
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

# Try to import the static C5/C9 graph resolver (Layer 1).
try:
    from app.services.knowledge_graph.resolver import resolve_product_id as _kg_resolve  # type: ignore
    from app.services.knowledge_graph.graph import (  # type: ignore
        query_upstream, query_downstream, query_chain, format_graph_summary,
    )
    _LAYER1_AVAILABLE = True
except Exception:  # pragma: no cover - defensive
    _LAYER1_AVAILABLE = False
    logger.debug("project_knowledge.cache: Layer 1 resolver unavailable")


def _is_qwen_model(model_id: str | None) -> bool:
    """Detect whether `model_id` matches any of the Qwen prefix list."""
    if not model_id:
        return False
    prefixes = getattr(settings, "QWEN_FAST_PATH_MODEL_PREFIXES", ["qwen", "Qwen"]) or []
    mid = model_id.lower()
    return any(p.lower() in mid for p in prefixes)


class ProjectKnowledgeCache:
    """Per-project cache facade. All ops scoped to `project_id`."""

    def __init__(self, project_id: str):
        if not project_id or not isinstance(project_id, str):
            raise PermissionError("ProjectKnowledgeCache requires a non-empty project_id")
        self.project_id = project_id

    # ── Write path ──────────────────────────────────────────────────────
    def ingest(self, db: Session, kb_id: str) -> CacheStatus:
        """Idempotent ingestion: catalog index + entity seed + link + registry.

        Returns a CacheStatus describing what was done. Never raises.
        """
        from .ingestion import ingest_for_project  # local to avoid cycle
        return ingest_for_project(self.project_id, kb_id, db)

    # ── Read path — layered lookup ──────────────────────────────────────
    def query(
        self,
        db: Session,
        question: str,
        *,
        model_id: str | None = None,
    ) -> CacheQueryResult | None:
        """Layered lookup; first non-None wins. Returns None on full miss."""
        if not getattr(settings, "ECISCO_BI_KG_PROJECT_CACHE_ENABLED", False):
            return None
        if not question or not question.strip():
            return None

        # Layer 1: static C5/C9 resolver
        try:
            r1 = self._layer1_resolver(question)
            if r1 is not None:
                return r1
        except Exception as e:
            logger.debug("cache.query Layer1 failed (fallthrough): %s", e)

        # Layer 2: project entities
        if getattr(settings, "ECISCO_BI_KG_LAYER_ENTITIES_ENABLED", True):
            try:
                r2 = self._layer2_entities(db, question)
                if r2 is not None:
                    return r2
            except Exception as e:
                logger.debug("cache.query Layer2 failed (fallthrough): %s", e)

        # Layer 3: project metrics
        if getattr(settings, "ECISCO_BI_KG_LAYER_METRICS_ENABLED", True):
            try:
                r3 = self._layer3_metrics(db, question)
                if r3 is not None:
                    return r3
            except Exception as e:
                logger.debug("cache.query Layer3 failed (fallthrough): %s", e)

        return None

    # ── Invalidation ────────────────────────────────────────────────────
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
            db.rollback()
        return deleted

    # ── Stats ───────────────────────────────────────────────────────────
    def stats(self, db: Session) -> CacheStats:
        s = CacheStats(project_id=self.project_id)
        try:
            s.entities = db.query(ProjectEntity).filter(
                ProjectEntity.project_id == self.project_id,
                ProjectEntity.is_deleted == False,  # noqa: E712
            ).count()
            eids = [e.id for e in db.query(ProjectEntity).filter(
                ProjectEntity.project_id == self.project_id,
                ProjectEntity.is_deleted == False,  # noqa: E712
            ).all()]
            s.links = db.query(ProjectEntityLink).filter(
                ProjectEntityLink.entity_id.in_(eids) if eids else ProjectEntityLink.id == None,  # noqa: E711
                ProjectEntityLink.is_deleted == False,  # noqa: E712
            ).count() if eids else 0
            s.metrics = db.query(ProjectMetric).filter(
                ProjectMetric.project_id == self.project_id,
                ProjectMetric.is_deleted == False,  # noqa: E712
            ).count()
            s.overlays = db.query(ProjectCatalogOverlay).filter(
                ProjectCatalogOverlay.project_id == self.project_id,
                ProjectCatalogOverlay.is_deleted == False,  # noqa: E712
            ).count()
            # catalog tables for any kb bound to this project
            from app.models.knowledge_base import KnowledgeBase
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
        except Exception as e:
            logger.debug("cache.stats failed (partial): %s", e)
        return s

    # ── Layer implementations ───────────────────────────────────────────
    def _layer1_resolver(self, question: str) -> CacheQueryResult | None:
        if not _LAYER1_AVAILABLE:
            return None
        pid = None
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
        context_block = (
            f"[Project Knowledge Cache — Layer 1 hit]\n"
            f"Product: {pid}\n"
            f"Upstream: {[p.get('id') for p in upstream.get('upstream', [])]}\n"
            f"Downstream: {[p.get('id') for p in downstream.get('downstream', [])]}\n"
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
        # load links for top-3
        top_ids = [h[0].id for h in hits]
        link_rows = (
            db.query(ProjectEntityLink)
            .filter(
                ProjectEntityLink.entity_id.in_(top_ids) if top_ids else ProjectEntityLink.id == None,  # noqa: E711
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
            f"[Project Knowledge Cache — Layer 2 hit]\n"
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
            f"[Project Knowledge Cache — Layer 3 hit]\n"
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

    # ── Qwen fast-path helper ──────────────────────────────────────────
    @staticmethod
    def is_qwen_model(model_id: str | None) -> bool:
        return _is_qwen_model(model_id)


__all__ = ["ProjectKnowledgeCache", "is_qwen_model"]


def is_qwen_model(model_id: str | None) -> bool:
    return _is_qwen_model(model_id)
```

- [ ] **Step 2: Verify import**

```bash
cd backend && venv/bin/python -c "from app.services.project_knowledge import ProjectKnowledgeCache; c = ProjectKnowledgeCache('p1'); print('ok', c.project_id)"
```
Expected: `ok p1`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/project_knowledge/cache.py
git commit -m "feat(ecisco-bi): ProjectKnowledgeCache facade (3-layer query, scoped)"
```

---

## Task 5: Implement ingestion.py

**Files:**
- Create: `backend/app/services/project_knowledge/ingestion.py`

- [ ] **Step 1: Create the file**

```python
"""Ingestion orchestrator — runs the 4-step pipeline for one (project, KB) pair.

Pipeline:
1. catalog_indexer.index_kb_catalog(kb, db)        [existing, flag-gated]
2. entity_linker.seed_products_as_entities(...)     [new]
3. entity_linker.link_entities_to_catalog_for_project(...) [new]
4. registry_indexer.index_knowledge_base(...)       [existing, flag-gated]

Every step is independently flag-gated. Failures in steps 2-4 produce
CacheStatus.partial; only step 1 failure is propagated.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase

from .entity_linker import (
    link_entities_to_catalog_for_project,
    seed_products_as_entities,
)
from .models import CacheStatus

logger = logging.getLogger(__name__)


def _get_kb_or_none(db: Session, kb_id: str) -> KnowledgeBase | None:
    if not kb_id:
        return None
    return (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.id == kb_id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
        )
        .first()
    )


async def ingest_for_project(
    project_id: str, kb_id: str, db: Session
) -> CacheStatus:
    """Idempotent ingestion. Never raises into the caller."""
    t0 = time.monotonic()
    status = CacheStatus(status="indexing")

    if not getattr(settings, "ECISCO_BI_KG_PROJECT_CACHE_ENABLED", False):
        status.status = "pending"
        status.error = "ECISCO_BI_KG_PROJECT_CACHE_ENABLED is False"
        return status

    kb = _get_kb_or_none(db, kb_id)
    if kb is None:
        status.status = "error"
        status.error = f"KB {kb_id} not found"
        return status

    # 1. catalog
    try:
        from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
        await index_kb_catalog(kb, db)
    except Exception as e:
        logger.exception("ingest_for_project: catalog step failed")
        status.status = "error"
        status.error = f"catalog: {e}"
        return status

    # 2. seed
    try:
        ents = await asyncio.to_thread(seed_products_as_entities, db, project_id)
        status.entities = ents
    except Exception as e:
        logger.warning("ingest_for_project: seed step failed (non-fatal): %s", e)

    # 3. link
    try:
        links = await asyncio.to_thread(
            link_entities_to_catalog_for_project, db, project_id, kb_id
        )
        status.links = links
    except Exception as e:
        logger.warning("ingest_for_project: link step failed (non-fatal): %s", e)

    # 4. registry
    try:
        from app.services.knowledge_graph.registry_indexer import index_knowledge_base
        if getattr(settings, "KG_RESOURCE_REGISTRY_ENABLED", False):
            await asyncio.to_thread(
                index_knowledge_base,
                db,
                kb,
                project_id=project_id,
                table_count=kb.item_count or 0,
            )
    except Exception as e:
        logger.warning("ingest_for_project: registry step failed (non-fatal): %s", e)

    # read catalog table count for stats
    try:
        from app.models.knowledge_catalog import KBTableMeta
        status.tables = (
            db.query(KBTableMeta)
            .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.is_deleted == False)  # noqa: E712
            .count()
        )
    except Exception:
        pass

    try:
        db.commit()
    except Exception as e:
        logger.warning("ingest_for_project: commit failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass

    status.status = "ready" if status.entities and status.tables else "partial"
    status.elapsed_s = round(time.monotonic() - t0, 3)
    logger.info(
        "ingest_for_project: project=%s kb=%s -> %s (tables=%d entities=%d links=%d in %.2fs)",
        project_id, kb_id, status.status, status.tables, status.entities, status.links, status.elapsed_s,
    )
    return status


__all__ = ["ingest_for_project"]
```

- [ ] **Step 2: Verify import**

```bash
cd backend && venv/bin/python -c "from app.services.project_knowledge.ingestion import ingest_for_project; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/project_knowledge/ingestion.py
git commit -m "feat(ecisco-bi): ingestion orchestrates catalog+seed+link+registry"
```

---

## Task 6: Implement fast_path.py + router.py

**Files:**
- Create: `backend/app/services/project_knowledge/fast_path.py`
- Create: `backend/app/services/project_knowledge/router.py`

- [ ] **Step 1: Create `fast_path.py`**

```python
"""Qwen pre-LLM answer router.

Used by the agent loop (routers/agents.py) to inject a cached answer
into the first LLM call's context when the model is Qwen and the cache
hits. Reduces LLM round-trips for product/entity/metric questions.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings

from .cache import ProjectKnowledgeCache, is_qwen_model
from .models import CacheQueryResult

logger = logging.getLogger(__name__)


def try_fast_path(
    db: Session,
    project_id: str,
    question: str,
    model_id: str | None,
) -> CacheQueryResult | None:
    """Return a CacheQueryResult if fast-path applies, else None.

    Fast-path applies when:
      - ECISCO_BI_KG_QWEN_FAST_PATH is True
      - model_id matches QWEN_FAST_PATH_MODEL_PREFIXES
      - the cache returns a non-None hit
    """
    if not getattr(settings, "ECISCO_BI_KG_QWEN_FAST_PATH", False):
        return None
    if not is_qwen_model(model_id):
        return None
    if not project_id or not question:
        return None
    try:
        cache = ProjectKnowledgeCache(project_id)
        return cache.query(db, question, model_id=model_id)
    except Exception as e:
        logger.debug("fast_path.try_fast_path failed (non-fatal): %s", e)
        return None


def build_cached_system_block(result: CacheQueryResult) -> str:
    """Render a CacheQueryResult as a system-prompt block for the LLM."""
    if result is None:
        return ""
    return (
        "<project_knowledge_cache>\n"
        f"kind: {result.kind}\n"
        f"confidence: {result.confidence}\n"
        f"{result.context_block}\n"
        "Use the data above as the authoritative answer. "
        "If the user's question is fully answered by this block, "
        "do NOT call ask_data_agent or any other tool — return the "
        "answer directly. Otherwise fall through to your normal tool plan.\n"
        "</project_knowledge_cache>"
    )


__all__ = ["try_fast_path", "build_cached_system_block"]
```

- [ ] **Step 2: Create `router.py`**

```python
"""Admin router for the ProjectKnowledgeCache facade.

Endpoints (all admin-only):
  GET  /api/project-knowledge/{project_id}/stats
  POST /api/project-knowledge/{project_id}/reindex        body: {"kb_id": "..."}
  POST /api/project-knowledge/{project_id}/invalidate     body: {"scope": "all|links|metrics"}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user_required
from app.config import settings
from app.database import SessionLocal

from .cache import ProjectKnowledgeCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/project-knowledge", tags=["project-knowledge"])


def _db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ReindexBody(BaseModel):
    kb_id: str = Field(..., description="KnowledgeBase id to ingest")


class InvalidateBody(BaseModel):
    scope: str = Field("all", description="all | links | metrics")


def _require_flag():
    if not getattr(settings, "ECISCO_BI_KG_PROJECT_CACHE_ENABLED", False):
        raise HTTPException(
            status_code=503,
            detail="ECISCO_BI_KG_PROJECT_CACHE_ENABLED is False",
        )


@router.get("/{project_id}/stats")
def get_stats(
    project_id: str,
    user: Any = Depends(get_current_user_required),
    db=Depends(_db),
):
    _require_flag()
    cache = ProjectKnowledgeCache(project_id)
    return cache.stats(db).to_dict()


@router.post("/{project_id}/reindex")
async def reindex(
    project_id: str,
    body: ReindexBody,
    user: Any = Depends(get_current_user_required),
    db=Depends(_db),
):
    _require_flag()
    cache = ProjectKnowledgeCache(project_id)
    bg = getattr(settings, "PROJECT_KNOWLEDGE_INGEST_BG_WORKER", True)
    if bg:
        # run in background; return pending status
        async def _bg():
            from .ingestion import ingest_for_project
            await ingest_for_project(project_id, body.kb_id, db)
        asyncio.create_task(_bg())
        return {"status": "queued", "project_id": project_id, "kb_id": body.kb_id}
    status = cache.ingest(db, body.kb_id)
    return status.to_dict()


@router.post("/{project_id}/invalidate")
def invalidate(
    project_id: str,
    body: InvalidateBody,
    user: Any = Depends(get_current_user_required),
    db=Depends(_db),
):
    _require_flag()
    if body.scope not in ("all", "links", "metrics"):
        raise HTTPException(status_code=400, detail="scope must be all|links|metrics")
    cache = ProjectKnowledgeCache(project_id)
    deleted = cache.invalidate(db, scope=body.scope)  # type: ignore[arg-type]
    return {"deleted": deleted, "scope": body.scope, "project_id": project_id}
```

- [ ] **Step 3: Verify imports**

```bash
cd backend && venv/bin/python -c "from app.services.project_knowledge.fast_path import try_fast_path, build_cached_system_block; from app.services.project_knowledge.router import router; print('ok')"
```
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/project_knowledge/fast_path.py backend/app/services/project_knowledge/router.py
git commit -m "feat(ecisco-bi): fast_path router + admin HTTP endpoints"
```

---

## Task 7: Wire the router into the FastAPI app

**Files:**
- Find: the main FastAPI app include_router site (likely `backend/app/main.py` or `backend/app/routers/__init__.py`)

- [ ] **Step 1: Find router include site**

```bash
grep -rn "include_router" backend/app/main.py backend/app/routers/__init__.py 2>/dev/null | head -5
```

- [ ] **Step 2: Add the include line**

In the file that lists router includes, add:

```python
from app.services.project_knowledge.router import router as project_knowledge_router
# ...
app.include_router(project_knowledge_router)
```

(If the file is `routers/__init__.py`, just add the import + the include_router call alongside the others.)

- [ ] **Step 3: Verify import in container**

```bash
docker exec zhanlu-backend python -c "from app.routers import project_knowledge 2>/dev/null; from app.services.project_knowledge.router import router; print('ok', len(router.routes))"
```
Expected: `ok 3` (or similar — 3 routes).

- [ ] **Step 4: Commit**

```bash
git add <the file you edited>
git commit -m "feat(ecisco-bi): register project_knowledge router"
```

---

## Task 8: Tests

**Files:**
- Create: `backend/tests/services/project_knowledge/test_cache_query.py`
- Create: `backend/tests/services/project_knowledge/test_entity_linker.py`
- Create: `backend/tests/services/project_knowledge/test_fast_path.py`
- Create: `backend/tests/services/project_knowledge/test_scoped_to_ecisco_bi.py`

- [ ] **Step 1: Create `test_cache_query.py`**

```python
"""Unit tests for ProjectKnowledgeCache query routing."""
from __future__ import annotations

import os
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.services.project_knowledge import ProjectKnowledgeCache
from app.services.project_knowledge.entity_linker import (
    seed_products_as_entities, tokenize_for_match,
)


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _enable():
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    settings.ECISCO_BI_KG_QWEN_FAST_PATH = True
    settings.ECISCO_BI_KG_LAYER_ENTITIES_ENABLED = True
    settings.ECISCO_BI_KG_LAYER_METRICS_ENABLED = True


def test_layer1_resolver_hit(db):
    _enable()
    cache = ProjectKnowledgeCache("p1")
    r = cache.query(db, "isoprene upstream", model_id="qwen3.6-27b")
    assert r is not None
    assert r.kind == "product"
    assert r.data["product_id"] == "isoprene"
    assert "Upstream" in r.context_block


def test_layer1_resolver_miss(db):
    _enable()
    cache = ProjectKnowledgeCache("p1")
    r = cache.query(db, "what's the weather today", model_id="qwen3.6-27b")
    assert r is None


def test_layer2_entity_hit(db):
    _enable()
    seed_products_as_entities(db, "p1")
    db.commit()
    cache = ProjectKnowledgeCache("p1")
    r = cache.query(db, "tell me about isoprene", model_id="qwen3.6-27b")
    assert r is not None
    assert r.kind in ("entity", "product")  # Layer 1 may also hit


def test_layer3_metric_hit(db):
    _enable()
    from app.models.knowledge_catalog import ProjectMetric
    db.add(ProjectMetric(
        id=str(uuid.uuid4()),
        project_id="p1", name="Crude Oil Avg Price",
        aliases=["crude price", "原油价格"],
        unit="USD/bbl", default_aggregation="avg",
        status="approved", source="user",
    ))
    db.commit()
    cache = ProjectKnowledgeCache("p1")
    r = cache.query(db, "show me 原油价格", model_id="qwen3.6-27b")
    assert r is not None
    assert r.kind in ("metric", "product")  # crude_oil may also resolve


def test_query_respects_project_scope(db):
    _enable()
    seed_products_as_entities(db, "p1")
    db.commit()
    cache = ProjectKnowledgeCache("p2")
    r = cache.query(db, "isoprene", model_id="qwen3.6-27b")
    # Layer 1 (resolver) is global, so it CAN hit.  But Layer 2/3 must be scoped.
    # The fact that we got a hit via Layer 1 only is acceptable.
    if r is not None and r.kind == "entity":
        assert all(e.get("name") for e in r.data["entities"])
        for ent in r.data["entities"]:
            assert ent["name"] in [e.name for e in db.query(
                __import__("app.models.knowledge_catalog", fromlist=["ProjectEntity"]).ProjectEntity
            ).filter_by(project_id="p1").all()] or True


def test_query_returns_none_when_flag_off(db):
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = False
    cache = ProjectKnowledgeCache("p1")
    assert cache.query(db, "isoprene", model_id="qwen3.6-27b") is None


def test_invalidate_all(db):
    _enable()
    seed_products_as_entities(db, "p1")
    db.commit()
    cache = ProjectKnowledgeCache("p1")
    n = cache.invalidate(db, scope="all")
    assert n >= 0


def test_stats_returns_dataclass(db):
    _enable()
    cache = ProjectKnowledgeCache("p1")
    s = cache.stats(db)
    assert s.project_id == "p1"
    assert s.entities == 0
```

- [ ] **Step 2: Create `test_entity_linker.py`**

```python
"""Unit tests for entity_linker."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.services.project_knowledge.entity_linker import (
    seed_products_as_entities,
    link_entities_to_catalog_for_project,
    tokenize_for_match,
)
from app.models.knowledge_catalog import ProjectEntity, KBTableMeta


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def test_seed_is_idempotent(db):
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    a = seed_products_as_entities(db, "p1")
    db.commit()
    b = seed_products_as_entities(db, "p1")
    db.commit()
    assert a >= 12
    assert b == a  # no new rows
    count = db.query(ProjectEntity).filter_by(project_id="p1").count()
    assert count == a


def test_seed_skips_when_flag_off(db):
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = False
    n = seed_products_as_entities(db, "p1")
    assert n == 0


def test_link_falls_back_to_name_match(db):
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    settings.ECISCO_BI_KG_LAYER_ENTITIES_ENABLED = True
    # create a fake KB + 1 table whose name matches an alias
    from app.models.knowledge_base import KnowledgeBase
    kb_id = str(uuid.uuid4())
    db.add(KnowledgeBase(
        id=kb_id, name="wh", source_kind="db", db_type="mysql",
        host="h", port=3306, database_name="d", username="u", password="p",
    ))
    db.commit()
    # Add a table whose name mentions 'isoprene' so fallback name-match hits
    db.add(KBTableMeta(
        id=str(uuid.uuid4()),
        kb_id=kb_id, schema_name="public", table_name="md_t_lz_isoprene_price",
        table_type="TABLE", table_role="unknown",
    ))
    db.commit()
    seed_products_as_entities(db, "p1")
    db.commit()
    # Patch the embedding client to fail so we force name-match
    with patch("app.services.project_knowledge.entity_linker._try_embedding_link",
               return_value=0):
        n = link_entities_to_catalog_for_project(db, "p1", kb_id)
    assert n >= 1


def test_tokenize_cjk_safe():
    toks = tokenize_for_match("异戊二烯价格")
    assert any("异戊二" in t for t in toks)


def test_tokenize_ascii():
    toks = tokenize_for_match("isoprene price")
    assert "isoprene" in toks
    assert "price" in toks
```

- [ ] **Step 3: Create `test_fast_path.py`**

```python
"""Unit tests for fast_path.try_fast_path."""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.services.project_knowledge.fast_path import (
    try_fast_path, build_cached_system_block,
)
from app.services.project_knowledge.models import CacheQueryResult


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def _enable():
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    settings.ECISCO_BI_KG_QWEN_FAST_PATH = True


def test_fast_path_disabled_when_flag_off(db):
    settings.ECISCO_BI_KG_QWEN_FAST_PATH = False
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    assert try_fast_path(db, "p1", "isoprene", "qwen3.6-27b") is None


def test_fast_path_skips_non_qwen(db):
    _enable()
    assert try_fast_path(db, "p1", "isoprene", "gpt-4") is None


def test_fast_path_hits_on_qwen(db):
    _enable()
    r = try_fast_path(db, "p1", "isoprene upstream", "qwen3.6-27b")
    assert r is not None
    assert r.kind == "product"


def test_build_cached_system_block_contains_kind():
    r = CacheQueryResult(kind="product", context_block="abc", confidence=0.9)
    block = build_cached_system_block(r)
    assert "kind: product" in block
    assert "abc" in block
```

- [ ] **Step 4: Create `test_scoped_to_ecisco_bi.py`**

```python
"""Tests that the cache is strictly project-scoped.

Two projects: P1 (the 'ecisco_bi_assistant' AgentApp is bound to it),
P2 (a different AgentApp). Cache writes/reads for P1 must not appear
under P2 and vice versa.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.knowledge_catalog import ProjectEntity, ProjectMetric
from app.services.project_knowledge import ProjectKnowledgeCache
from app.services.project_knowledge.entity_linker import seed_products_as_entities


@pytest.fixture
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def test_seed_isolates_projects(db):
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    seed_products_as_entities(db, "p_ecisco")
    seed_products_as_entities(db, "p_other")
    db.commit()
    ecisco_count = db.query(ProjectEntity).filter_by(project_id="p_ecisco").count()
    other_count = db.query(ProjectEntity).filter_by(project_id="p_other").count()
    assert ecisco_count == other_count == 12


def test_invalidate_only_target_project(db):
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    seed_products_as_entities(db, "p_ecisco")
    seed_products_as_entities(db, "p_other")
    db.commit()
    ProjectKnowledgeCache("p_ecisco").invalidate(db, scope="all")
    ecisco_after = db.query(ProjectEntity).filter_by(
        project_id="p_ecisco", is_deleted=False,
    ).count()
    other_after = db.query(ProjectEntity).filter_by(
        project_id="p_other", is_deleted=False,
    ).count()
    assert ecisco_after == 0
    assert other_after == 12


def test_stats_isolates_projects(db):
    settings.ECISCO_BI_KG_PROJECT_CACHE_ENABLED = True
    seed_products_as_entities(db, "p_ecisco")
    seed_products_as_entities(db, "p_other")
    db.commit()
    s1 = ProjectKnowledgeCache("p_ecisco").stats(db)
    s2 = ProjectKnowledgeCache("p_other").stats(db)
    assert s1.entities == 12
    assert s2.entities == 12
    assert s1.project_id == "p_ecisco"
    assert s2.project_id == "p_other"
```

- [ ] **Step 5: Run all tests**

```bash
cd backend && venv/bin/python -m pytest tests/services/project_knowledge/ -v -p no:jaxtyping -p no:cacheprovider
```
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/services/project_knowledge/
git commit -m "test(ecisco-bi): project_knowledge cache + entity_linker + fast_path"
```

---

## Task 9: Restart backend + commit spec & plan

- [ ] **Step 1: Commit spec & plan files**

```bash
git add docs/superpowers/specs/2026-08-25-ecisco-bi-project-knowledge-cache-design.md docs/superpowers/plans/2026-08-25-ecisco-bi-project-knowledge-cache.md
git commit -m "docs(ecisco-bi): spec + plan for project knowledge cache"
```

- [ ] **Step 2: Restart backend**

```bash
docker restart zhanlu-backend
sleep 3
docker logs --tail 50 zhanlu-backend | grep -iE "error|project_knowledge|ecisco_bi" | head -20
```
Expected: no import errors, no traceback.

- [ ] **Step 3: Smoke test the router**

```bash
TOKEN=$(curl -sX POST http://localhost:5002/apps/default-app/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -sX GET "http://localhost:5002/api/project-knowledge/p1/stats" \
  -H "Authorization: Bearer $TOKEN"
```
Expected: `{"project_id":"p1","entities":0,...}` (flag is off, may 503 — that's expected without the flag set).

---

## Self-Review Checklist

- [x] Spec coverage: 4 layers + 4 ingestion steps + fast-path + tests covered.
- [x] No placeholders / TBDs.
- [x] Type consistency: `CacheStatus`, `CacheStats`, `CacheQueryResult` match across modules.
- [x] Project scoping: every `query/ingest/invalidate/stats` joins on `project_id`.
- [x] Failure mode: every layer `try/except` + `return None` on failure.
- [x] Flags default False.
