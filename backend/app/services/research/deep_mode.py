"""
DeepResearchService — parallel fan-out research mode.

Splits a complex question into sub-questions, fans them out as queued agent
runs in parallel, collects results, and synthesizes a final report.

Gated by DEEP_RESEARCH_MODE_ENABLED (default False).
Requires both AGENT_HARNESS_ENABLED and DELEGATION_ASYNC_ENABLED.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DeepResearchResult:
    """Output of one DeepResearchService.research() cycle."""

    success: bool = False
    answer: str = ""
    sub_results: list[dict[str, Any]] = field(default_factory=list)
    sub_count: int = 0
    duration_ms: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

SplitterFn = Callable[[str], list[str]]
"""sync fn(question) -> list of sub-questions"""

SynthesizerFn = Callable[[str, list[dict[str, Any]]], Coroutine[Any, Any, str]]
"""async fn(question, sub_results) -> synthesized report"""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DeepResearchService:
    """Splits complex questions, fans out parallel agent runs, synthesizes.

    All external dependencies are injected (DI pattern): the AgentRunService
    for dispatching runs, the splitter for question decomposition, and the
    synthesizer for report generation.
    """

    def __init__(
        self,
        *,
        run_service: Any,  # AgentRunService (duck-typed for testability)
        splitter_fn: SplitterFn,
        synthesizer_fn: SynthesizerFn,
        default_timeout: float = 120.0,
        poll_interval: float = 1.0,
    ):
        self._run_service = run_service
        self._splitter = splitter_fn
        self._synthesizer = synthesizer_fn
        self.default_timeout = default_timeout
        self.poll_interval = poll_interval

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def research(self, question: str) -> DeepResearchResult:
        """Execute deep research: split → fan-out → collect → synthesize.

        Returns a DeepResearchResult even on partial failure (best-effort).
        """
        t_start = time.time()

        # Step 1 — split into sub-questions
        sub_questions: list[str]
        try:
            sub_questions = self._splitter(question)
            if not sub_questions:
                sub_questions = [question]
        except Exception:
            logger.warning("DeepResearchService: splitter failed, using single question")
            sub_questions = [question]

        # Step 2 — fan-out: start all sub-runs in queued mode
        run_ids: list[str] = []
        for i, sub_q in enumerate(sub_questions):
            try:
                rid = await self._run_service.start_run(
                    agent_name="deep_research",
                    task=sub_q,
                    mode="queued",
                    run_id=f"deep-r{i}",
                )
                run_ids.append(rid)
            except Exception:
                logger.exception("DeepResearchService: failed to start sub-run %d", i)

        # Step 3 — collect all sub-run results in parallel
        collect_tasks = [
            self._run_service.collect_run(
                rid,
                timeout=self.default_timeout,
                poll_interval=self.poll_interval,
            )
            for rid in run_ids
        ]
        raw_results = await asyncio.gather(*collect_tasks, return_exceptions=True)

        # Normalize: replace exceptions with error dicts
        sub_results: list[dict[str, Any]] = []
        for i, res in enumerate(raw_results):
            if isinstance(res, Exception):
                sub_results.append({
                    "status": "error",
                    "answer": "",
                    "error": str(res),
                })
            elif isinstance(res, dict):
                sub_results.append(res)
            else:
                sub_results.append({
                    "status": "error",
                    "answer": "",
                    "error": f"unexpected result type: {type(res)}",
                })

        # Step 4 — synthesize final report
        final_answer: str = ""
        synthesis_error: str | None = None
        try:
            final_answer = await self._synthesizer(question, sub_results)
        except Exception as exc:
            logger.exception("DeepResearchService: synthesis failed")
            synthesis_error = str(exc)
            # Fallback: concatenate all sub-answers
            parts = [r.get("answer", "") for r in sub_results if r.get("answer")]
            final_answer = "\n\n---\n\n".join(parts) if parts else ""

        duration_ms = int((time.time() - t_start) * 1000)

        any_completed = any(
            r.get("status") == "completed" for r in sub_results
        )

        return DeepResearchResult(
            success=any_completed and synthesis_error is None,
            answer=final_answer,
            sub_results=sub_results,
            sub_count=len(sub_questions),
            duration_ms=duration_ms,
            error=synthesis_error,
        )
