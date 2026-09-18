#!/usr/bin/env python3
"""Live E2E probe: SemanticStore retrieval + business vocabulary on real rows.

Unit tests prove the predicates and the seams; this proves the LIVE path — real
catalog, real project, real question, real SQL — and prints what the model would
actually have been shown.

Run inside the backend container of the stack under test:

    docker cp backend/scripts_probe_semantic_live.py <backend>:/app/
    docker exec -w /app <backend> python scripts_probe_semantic_live.py

Costs one LLM SQL call (skip_narrate=True). Read the `NLAnswer:` lines in the
container log afterwards — that is where "semantic retrieval → N candidate
tables" is printed.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import ProjectCatalogOverlay, ProjectMetric
from app.models.terminology import Terminology
from app.services.db.nl_answer_service import NLAnswerService
from app.services.db.semantic_store import (
    SemanticStore,
    format_retrieval_hint,
    format_source_ranking,
)
from app.services.knowledge_graph.business_context import build_business_context

KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "aipdp_data_warehouse_prod"
QUESTION = (
    sys.argv[2]
    if len(sys.argv) > 2
    else "How many contracts are there by document status? Use charts."
)


async def main() -> None:
    db = SessionLocal()
    try:
        kb = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.name == KB_NAME,
                KnowledgeBase.is_deleted.is_(False),
            )
            .first()
        )
        if kb is None:
            print(f"no KB named {KB_NAME!r}")
            return
        project_id = kb.project_id
        print(f"kb: {kb.name} ({kb.id}) project={project_id}")

        # what the semantic foundation knows, and what it would show the model
        store = SemanticStore(db, [kb.id], project_id=project_id)
        print("stats:", store.stats())
        hits = store.rank_relations(QUESTION, k=12)
        print(f"\nRETRIEVED_CANDIDATES={len(hits)}")
        print(format_retrieval_hint(hits) or "(no candidate evidence)")

        # curation state — why the vocabulary section is (or is not) populated
        print(
            "\ncuration: overlay_rows=%d approved_metrics=%d proposed_metrics=%d "
            "terminology=%d"
            % (
                db.query(ProjectCatalogOverlay)
                .filter(ProjectCatalogOverlay.project_id == project_id)
                .count()
                if project_id
                else 0,
                db.query(ProjectMetric)
                .filter(ProjectMetric.status == "approved")
                .count(),
                db.query(ProjectMetric)
                .filter(ProjectMetric.status == "proposed")
                .count(),
                db.query(Terminology).count(),
            )
        )

        block = build_business_context(db, project_id, kb.id, QUESTION)
        print(
            "\n--- business context injected into the SQL prompt "
            f"({len(block)} chars) ---"
        )
        print(block or "(empty)")

        # --- VERIFIED-tier proof, ROLLED BACK ---------------------------------
        # Curation is empty on this stack (0 approved metrics, 0 glossary rows),
        # so the vocabulary section is legitimately blank. Prove the section
        # RENDERS by staging one glossary term + one approved metric inside the
        # session and rolling back — live rows, real function, no state change.
        # Approving a real metric is a curator's decision, not a test's.
        from app.models.terminology import Terminology as _T

        before = (
            db.query(_T).count(),
            db.query(ProjectMetric).filter(ProjectMetric.status == "approved").count(),
        )
        db.add(
            _T(
                word="ARR", description="annual recurring revenue (staged probe row)",
                datasource_ids=None, enabled=True,
            )
        )
        db.flush()
        # Flip the REAL proposed metric to approved inside this transaction
        # (a new row would collide on uq_project_metric) — then roll back, so
        # the stack's curation state is left exactly as found.
        real_metric = (
            db.query(ProjectMetric)
            .filter(
                ProjectMetric.project_id == project_id,
                ProjectMetric.name == "Contract Amount",
            )
            .first()
        )
        if real_metric is not None:
            real_metric.status = "approved"
            db.flush()
        staged = build_business_context(db, project_id, kb.id, QUESTION)
        print("\n--- business context WITH a staged VERIFIED row "
              f"({len(staged)} chars) ---")
        print(staged or "(empty)")
        print(
            "\nVOCABULARY_SECTION_RENDERS="
            f"{'## Business vocabulary (user-defined)' in staged}"
        )
        print(f"STAGED_TERM_PRESENT={'Contract Amount' in staged}")
        db.rollback()
        after = (
            db.query(_T).count(),
            db.query(ProjectMetric).filter(ProjectMetric.status == "approved").count(),
        )
        print(f"ROLLED_BACK={before == after} before={before} after={after}")

        # the live answering path (this is what runs in production).
        # Capture this module's own INFO records: the container's stdout may not
        # carry them when the process is not attached to a tty, and the log line
        # is the only proof the retrieval step actually ran.
        records: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, record):  # noqa: ANN001
                try:
                    records.append(f"{record.levelname} {record.getMessage()}")
                except Exception:
                    pass

        cap = _Cap(level=logging.INFO)
        target = logging.getLogger("app.services.db.nl_answer_service")
        target.addHandler(cap)
        # The platform root logger runs at WARNING, so this module's INFO
        # diagnostics are dropped before any handler sees them — including the
        # "domain scoping" / "static route" / "semantic retrieval" lines. Raise
        # the level for the duration of the probe so the wiring is observable.
        prev_level = target.level
        target.setLevel(logging.INFO)

        # Second, log-independent instrument: count the calls into the
        # retrieval step. This proves the wiring even if logging is silent.
        from app.services.db import semantic_store as _ss

        _orig_rank = _ss.SemanticStore.rank_relations
        rank_calls: list[tuple[str, int]] = []

        def _spy(self, question, k=25, use_workload=None):  # noqa: ANN001
            hits = _orig_rank(self, question, k=k, use_workload=use_workload)
            rank_calls.append((question[:60], len(hits)))
            return hits

        _ss.SemanticStore.rank_relations = _spy
        try:
            res = await NLAnswerService(db).answer(
                question=QUESTION, kb_id=kb.id, project_id=project_id,
                skip_narrate=True,
            )
        finally:
            _ss.SemanticStore.rank_relations = _orig_rank
            target.removeHandler(cap)
            target.setLevel(prev_level)

        print("\n--- NLAnswer log records this turn (INFO forced on) ---")
        for line in records[-25:]:
            print(" *", line)
        if not records:
            print(" * (none captured)")
        retrieval_line = [r for r in records if "semantic retrieval" in r]
        print(f"\nRETRIEVAL_STEP_FIRED={bool(retrieval_line)}")
        for line in retrieval_line:
            print("   ", line)
        print(f"RANK_RELATIONS_CALLS={len(rank_calls)} {rank_calls}")
        rows = res.get("rows") or []
        print(f"\n[live] rows={len(rows)} truncated={res.get('truncated')} "
              f"violations={res.get('contract_violations')}")
        print(f"[live] sql: {(res.get('sql') or '')[:300]}")
        if rows:
            print(f"[live] row sample: {rows[0]}")
        if len(sys.argv) > 3 and sys.argv[3] == "--rank-sources":
            print("\n" + (format_source_ranking(store.rank_sources(QUESTION)) or
                          "(no source ranking)"))
    finally:
        db.close()


asyncio.run(main())
