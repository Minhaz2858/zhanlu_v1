"""Evidence Pack builder — structured evidence for depth-analysis answers.

Modeled on the forecasting/analyst evidence_pack pattern: collects all
queries run, rows examined, charts, confidence, and citations so the
final answer is grounded in verifiable evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvidencePack:
    """Structured evidence collected during a depth-analysis loop."""

    queries: list[dict[str, Any]] = field(default_factory=list)
    # Each: {sql, table, row_count, sample_rows}
    rows_examined: int = 0
    charts: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    citations: list[str] = field(default_factory=list)
    iterations: int = 0

    def add_query(
        self,
        *,
        sql: str,
        table: str = "",
        row_count: int = 0,
        sample_rows: list[dict] | None = None,
    ) -> None:
        self.queries.append({
            "sql": sql,
            "table": table,
            "row_count": row_count,
            "sample_rows": (sample_rows or [])[:5],  # cap samples
        })
        self.rows_examined += row_count

    def add_citation(self, citation: str) -> None:
        if citation and citation not in self.citations:
            self.citations.append(citation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queries": self.queries,
            "rows_examined": self.rows_examined,
            "charts": self.charts,
            "confidence": self.confidence,
            "citations": self.citations,
            "iterations": self.iterations,
        }
