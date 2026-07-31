"""Truth-gate: post-process forecast engine results into a data-anchored response.

Wraps raw engine output with a data_anchor (source table, sample size, first/last
rows) and converts thin-data cases into explicit insufficient_data verdicts.
This is what makes the LLM's forecast answer actually data-grounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TruthGateConfig:
    min_sample_size: int = 5            # below → "insufficient_data"
    max_anchor_rows: int = 10           # rows shown to LLM in the data_anchor
    confidence_threshold: float = 0.5   # below → "low_confidence" warning


def _build_failure(source_table: str, sample_size: int, min_required: int) -> dict:
    return {
        "success": False,
        "reason": "insufficient_data",
        "source_table": source_table,
        "sample_size": sample_size,
        "message": (
            f"Need ≥{min_required} data points in {source_table}; "
            f"found {sample_size}. Cannot produce a reliable forecast."
        ),
    }


def _build_anchor(anchor_rows: list[dict], source_table: str, sample_size: int) -> dict:
    return {
        "source_table": source_table,
        "sample_size": sample_size,
        "first_5": anchor_rows[:5],
        "last_5": anchor_rows[-5:] if len(anchor_rows) > 5 else anchor_rows,
    }


def wrap_forecast_result(
    raw_runs: list[dict],
    anchor_rows: list[dict],
    source_table: str,
    sample_size: int,
    config: TruthGateConfig | None = None,
) -> dict[str, Any]:
    """Post-process a forecast engine result into a truth-gated response.

    Success path (sample_size ≥ config.min_sample_size):
        {"success": True, "data_anchor": {...}, "runs": [...]}

    Failure path (sample_size < config.min_sample_size):
        {"success": False, "reason": "insufficient_data", "sample_size": N, ...}
    """
    cfg = config or TruthGateConfig()
    if sample_size < cfg.min_sample_size:
        return _build_failure(source_table, sample_size, cfg.min_sample_size)
    return {
        "success": True,
        "data_anchor": _build_anchor(anchor_rows, source_table, sample_size),
        "runs": raw_runs,
    }
