"""Depth Analysis Loop — bounded hypothesis→query→validate→refine.

For "why" questions, runs a multi-step investigation:
1. LLM generates a hypothesis (what to investigate).
2. Runner uses schema_linker to find relevant tables.
3. Runner executes a deterministic exploratory query (the LLM NEVER
   decides what SQL to run).
4. Runner validates results (non-empty = sufficient evidence).
5. If insufficient, LLM refines the hypothesis; repeat (max 3).
6. LLM writes the final explanatory answer grounded in the evidence pack.

The LLM explains, NEVER decides. Flag-gated by DEPTH_ANALYSIS_LOOP_ENABLED.
Soft-fail: any error → degraded=True, returns a fallback answer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.services.depth_analysis.evidence_pack import EvidencePack
from app.services.db.query_service import QueryService
from app.services.knowledge_graph.schema_linker import link_schema
from app.services.llm_service import call_llm

logger = logging.getLogger(__name__)

_HYPOTHESIS_SYSTEM = (
    "You are a data analyst. Given a question and a data schema, generate "
    "a concise hypothesis about what to investigate. Output JSON: "
    '{"hypothesis": "what to check", "table_hint": "table name if obvious"}'
)

_ANSWER_SYSTEM = (
    "You are a data analyst writing an evidence-based answer. Use ONLY the "
    "provided evidence (query results, row counts, samples). Do not invent "
    "data. If the evidence is insufficient, say so. Be concise and specific. "
    "Output plain text."
)

_REFINE_SYSTEM = (
    "You are a data analyst. The previous investigation found insufficient "
    "evidence. Suggest a refined hypothesis. Output JSON: "
    '{"refinement": "what else to check"}'
)


@dataclass
class DepthAnalysisResult:
    """Result of a depth-analysis loop run."""

    answer: str = ""
    evidence_pack: dict[str, Any] = field(default_factory=dict)
    iterations: int = 0
    confidence: float = 0.0
    degraded: bool = False


def run_depth_loop(
    question: str,
    project_id: str,
    db: Session,
    *,
    max_iterations: int = 3,
    kb_id: str | None = None,
) -> DepthAnalysisResult:
    """Run a bounded depth-analysis loop. Never raises.

    Returns a DepthAnalysisResult with the answer, evidence pack, and
    confidence. On any failure, returns degraded=True with a fallback.
    """
    if not getattr(settings, "DEPTH_ANALYSIS_LOOP_ENABLED", False):
        return DepthAnalysisResult(
            answer="Depth analysis is not enabled.",
            degraded=True,
        )

    pack = EvidencePack()
    bound_kb_id = kb_id

    try:
        loop = asyncio.new_event_loop()
        try:
            result = _run_loop_sync(
                loop, question, project_id, db, pack,
                max_iterations=max_iterations, kb_id=bound_kb_id,
            )
            return result
        finally:
            loop.close()
    except Exception as e:
        logger.exception("depth_analysis: loop failed (non-fatal): %s", e)
        return DepthAnalysisResult(
            answer=(
                "I was unable to complete a depth analysis of this question. "
                "Please try rephrasing or ask a simpler question."
            ),
            evidence_pack=pack.to_dict(),
            iterations=pack.iterations,
            confidence=0.0,
            degraded=True,
        )


def _run_loop_sync(
    loop: asyncio.AbstractEventLoop,
    question: str,
    project_id: str,
    db: Session,
    pack: EvidencePack,
    *,
    max_iterations: int,
    kb_id: str | None,
) -> DepthAnalysisResult:
    """Core loop logic (sync wrapper around async LLM calls)."""

    # ── 1. Generate initial hypothesis ──
    hypothesis = _llm_hypothesis(loop, question, schema_text="")
    if not hypothesis:
        return DepthAnalysisResult(
            answer="Unable to generate a hypothesis for this question.",
            evidence_pack=pack.to_dict(),
            degraded=True,
        )

    # ── 2. Iterative investigation ──
    for iteration in range(1, max_iterations + 1):
        pack.iterations = iteration

        # Find relevant tables via schema linker
        kb_ids = [kb_id] if kb_id else []
        if not kb_ids:
            # Try to find project-bound KBs
            kb_ids = _find_project_kb_ids(db, project_id)

        linked = None
        if kb_ids:
            try:
                linked = link_schema(question, kb_ids, db, top_k=5)
            except Exception:
                pass

        if not linked or not linked.get("tables"):
            # No catalog → can't query → refine or give up
            if iteration < max_iterations:
                hypothesis = _llm_refine(loop, question, "No tables found in catalog.")
                continue
            break

        # Execute deterministic exploratory query on the top table
        top_table = linked["tables"][0]
        table_name = top_table.get("table_name", "")
        exec_kb_id = kb_ids[0] if kb_ids else kb_id

        if table_name and exec_kb_id:
            sql = f"SELECT * FROM {table_name} ORDER BY 1 DESC LIMIT 20"
            try:
                qs = QueryService(db)
                res = qs.execute(exec_kb_id, sql, max_rows=20)
                rows = res.get("rows", [])
                row_count = res.get("row_count", len(rows))
                pack.add_query(
                    sql=sql, table=table_name,
                    row_count=row_count, sample_rows=rows,
                )
                pack.add_citation(f"Table {table_name}: {row_count} rows examined")
            except Exception as e:
                logger.debug("depth_analysis: query failed (iter %d): %s", iteration, e)

        # Validate: sufficient evidence?
        if pack.rows_examined > 0:
            break  # sufficient — exit early

        # Refine hypothesis
        if iteration < max_iterations:
            hypothesis = _llm_refine(
                loop, question,
                f"Query returned 0 rows from {table_name}.",
            )

    # ── 3. Build confidence score ──
    confidence = _compute_confidence(pack)
    degraded = pack.rows_examined == 0

    # ── 4. LLM writes final answer ──
    answer = _llm_answer(loop, question, pack)

    pack.confidence = confidence
    return DepthAnalysisResult(
        answer=answer,
        evidence_pack=pack.to_dict(),
        iterations=pack.iterations,
        confidence=confidence,
        degraded=degraded,
    )


def _llm_hypothesis(
    loop: asyncio.AbstractEventLoop, question: str, schema_text: str
) -> str:
    """Generate an initial hypothesis via LLM."""
    try:
        result = loop.run_until_complete(call_llm(
            messages=[
                {"role": "system", "content": _HYPOTHESIS_SYSTEM},
                {"role": "user", "content": f"Question: {question}\nSchema: {schema_text or 'unknown'}"},
            ],
            temperature=0.2,
            response_json_schema={
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "table_hint": {"type": "string"},
                },
                "required": ["hypothesis"],
            },
            task_type="depth_analysis_hypothesis",
        ))
        data = result.get("data") if isinstance(result, dict) else {}
        return (data.get("hypothesis") or "") if isinstance(data, dict) else ""
    except Exception as e:
        logger.debug("depth_analysis: hypothesis LLM failed: %s", e)
        return ""


def _llm_refine(
    loop: asyncio.AbstractEventLoop, question: str, feedback: str
) -> str:
    """Generate a refined hypothesis based on feedback."""
    try:
        result = loop.run_until_complete(call_llm(
            messages=[
                {"role": "system", "content": _REFINE_SYSTEM},
                {"role": "user", "content": f"Question: {question}\nFeedback: {feedback}"},
            ],
            temperature=0.3,
            response_json_schema={
                "type": "object",
                "properties": {"refinement": {"type": "string"}},
                "required": ["refinement"],
            },
            task_type="depth_analysis_refine",
        ))
        data = result.get("data") if isinstance(result, dict) else {}
        return (data.get("refinement") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _llm_answer(
    loop: asyncio.AbstractEventLoop, question: str, pack: EvidencePack
) -> str:
    """Generate the final explanatory answer from evidence (LLM explains, never decides)."""
    evidence_text = _format_evidence_for_llm(pack)
    try:
        result = loop.run_until_complete(call_llm(
            messages=[
                {"role": "system", "content": _ANSWER_SYSTEM},
                {"role": "user", "content": f"Question: {question}\n\nEvidence:\n{evidence_text}"},
            ],
            temperature=0.1,
            task_type="depth_analysis_answer",
        ))
        return (result.get("response") or "").strip() if isinstance(result, dict) else ""
    except Exception as e:
        logger.debug("depth_analysis: answer LLM failed: %s", e)
        return "Based on the available evidence, I was unable to draw a definitive conclusion."


def _format_evidence_for_llm(pack: EvidencePack) -> str:
    """Format the evidence pack as readable text for the LLM."""
    if not pack.queries:
        return "No queries were executed (no data sources available)."
    lines = []
    for i, q in enumerate(pack.queries, 1):
        lines.append(f"Query {i}: {q['sql']}")
        lines.append(f"  Table: {q['table']}, Rows: {q['row_count']}")
        if q.get("sample_rows"):
            for row in q["sample_rows"][:3]:
                lines.append(f"  Sample: {row}")
    lines.append(f"\nTotal rows examined: {pack.rows_examined}")
    return "\n".join(lines)


def _compute_confidence(pack: EvidencePack) -> float:
    """Compute a confidence score based on evidence quality."""
    if pack.rows_examined == 0:
        return 0.0
    # Base confidence for having any evidence, scaled by rows (capped at 5)
    row_factor = min(1.0, pack.rows_examined / 5.0)
    # Fewer iterations → higher confidence (found answer quickly)
    iter_factor = max(0.3, 1.0 - (pack.iterations - 1) * 0.2)
    return round(0.3 + 0.7 * row_factor * iter_factor, 2)


def _find_project_kb_ids(db: Session, project_id: str) -> list[str]:
    """Find KB IDs bound to a project."""
    try:
        from app.models.knowledge_base import KnowledgeBase
        from sqlalchemy import or_

        from app.models.project import Project

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return []
        kbs = (
            db.query(KnowledgeBase)
            .filter(
                or_(
                    KnowledgeBase.project_id == project_id,
                    KnowledgeBase.project == project.name,
                ),
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.db_type.isnot(None),
            )
            .limit(5)
            .all()
        )
        return [kb.id for kb in kbs]
    except Exception:
        return []
