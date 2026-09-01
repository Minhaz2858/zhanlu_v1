"""Decision ROI calculator — pure functions with zero I/O (Phase F2).

Enables T1.4 threshold calibration: replay each decision under different
threshold values to find the optimal threshold set that maximizes ROI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

# ------------------------------------------------------------------ #
# ROI score for a single decision
# ------------------------------------------------------------------ #

def score_decision(
    action: str,
    actual_price_t: float,
    actual_price_th: float,
    margin_pct: float = 0.0,
) -> float:
    """Compute ROI (%) for a single buy/sell/hold decision.

    ROI convention:
      - buy:  (actual_price_th / actual_price_t - 1) * 100  (positive = price went up, good buy)
      - sell: (actual_price_t / actual_price_th - 1) * 100  (positive = price went down, good sell)
      - hold / watch: 0.0  (no position taken, no P&L)
      - margin_pct: subtract from ROI (transaction cost / bid-ask slippage)

    Returns signed ROI in percent.
    """
    if action in ("hold", "watch"):
        return 0.0
    if actual_price_t <= 0 or actual_price_th <= 0:
        return 0.0
    if action == "buy":
        roi = (actual_price_th / actual_price_t - 1) * 100.0
    elif action == "sell":
        roi = (actual_price_t - actual_price_th) / actual_price_t * 100.0
    else:
        return 0.0
    return roi - margin_pct


def score_pending_decisions(
    pending_logs: Sequence[Any],
) -> list[dict[str, Any]]:
    """Score all pending decisions whose realized window has closed.

    Each log must have: action, actual_price_t, actual_price_th.
    Returns list of {log_id, action, roi_pct} for batch DB update.
    """
    results: list[dict[str, Any]] = []
    for log in pending_logs:
        if (getattr(log, "actual_price_t", None) and
                getattr(log, "actual_price_th", None)):
            roi = score_decision(
                action=log.action,
                actual_price_t=float(log.actual_price_t),
                actual_price_th=float(log.actual_price_th),
            )
            results.append({
                "log_id": log.id,
                "action": log.action,
                "roi_pct": roi,
            })
    return results


# ------------------------------------------------------------------ #
# Aggregate ROI over a set of decisions
# ------------------------------------------------------------------ #

@dataclass
class RoiSummary:
    """Aggregate ROI statistics across a batch of decisions."""
    total_decisions: int
    buy_count: int
    sell_count: int
    hold_count: int
    buy_roi_avg: float
    buy_roi_sum: float
    sell_roi_avg: float
    sell_roi_sum: float
    weighted_roi: float
    accuracy_pct: float
    # Per-action accuracy: was the decision directionally correct?
    buy_correct: int
    sell_correct: int
    total_realized: int


def aggregate_roi(logs: Sequence[Any]) -> RoiSummary:
    """Compute aggregate ROI stats from a sequence of decision logs.

    Each log must have: action, roi_pct (already scored).
    """
    buy_rois: list[float] = []
    sell_rois: list[float] = []
    buy_correct = sell_correct = hold_count = 0
    total_realized = 0

    for log in logs:
        action = getattr(log, "action", "hold")
        roi = float(getattr(log, "roi_pct", 0) or 0)
        if action == "hold":
            hold_count += 1
        elif action == "buy":
            buy_rois.append(roi)
            if roi > 0:
                buy_correct += 1
            total_realized += 1
        elif action == "sell":
            sell_rois.append(roi)
            if roi > 0:
                sell_correct += 1
            total_realized += 1
        elif action == "watch":
            hold_count += 1

    buy_avg = sum(buy_rois) / len(buy_rois) if buy_rois else 0.0
    sell_avg = sum(sell_rois) / len(sell_rois) if sell_rois else 0.0

    all_rois = buy_rois + sell_rois
    weighted = sum(all_rois) / len(all_rois) if all_rois else 0.0
    accuracy = (
        (buy_correct + sell_correct) / total_realized * 100
        if total_realized > 0
        else 0.0
    )

    return RoiSummary(
        total_decisions=len(logs),
        buy_count=len(buy_rois),
        sell_count=len(sell_rois),
        hold_count=hold_count,
        buy_roi_avg=round(buy_avg, 4),
        buy_roi_sum=round(sum(buy_rois), 4),
        sell_roi_avg=round(sell_avg, 4),
        sell_roi_sum=round(sum(sell_rois), 4),
        weighted_roi=round(weighted, 4),
        accuracy_pct=round(accuracy, 2),
        buy_correct=buy_correct,
        sell_correct=sell_correct,
        total_realized=total_realized,
    )


# ------------------------------------------------------------------ #
# Threshold grid search (T1.4 calibration)
# ------------------------------------------------------------------ #

def replay_under_thresholds(
    logs: Sequence[Any],
    buy_threshold: float,
    sell_threshold: float,
    min_change: float = 0.03,
) -> list[dict[str, Any]]:
    """Replay what each decision WOULD have been under different thresholds.

    Each log must have: predicted_p_rise, predicted_change_pct (the original
    input that fed the decision engine).

    The original decision engine logic (simplified):
      - if p_rise >= BUY_THRESHOLD and change >= min_change → buy
      - elif p_rise <= SELL_THRESHOLD and change <= -min_change → sell
      - else → hold

    Returns list of {log_id, original_action, replayed_action, roi_pct_if_replayed}
    so you can compute what ROI would have been under each threshold combo.
    """
    results: list[dict[str, Any]] = []
    for log in logs:
        p_rise = float(getattr(log, "predicted_p_rise", 0) or 0)
        change = float(getattr(log, "predicted_change_pct", 0) or 0)
        original_action = getattr(log, "action", "hold")
        actual_price_t = float(getattr(log, "actual_price_t", 0) or 0)
        actual_price_th = float(getattr(log, "actual_price_th", 0) or 0)

        # Determine what action WOULD be taken under these thresholds
        if p_rise >= buy_threshold and change >= min_change:
            replayed_action = "buy"
        elif p_rise <= sell_threshold and change <= -min_change:
            replayed_action = "sell"
        else:
            replayed_action = "hold"

        # Calculate what ROI WOULD have been
        if actual_price_t > 0 and actual_price_th > 0:
            replayed_roi = score_decision(
                replayed_action, actual_price_t, actual_price_th
            )
        else:
            replayed_roi = 0.0

        results.append({
            "log_id": getattr(log, "id", None),
            "original_action": original_action,
            "replayed_action": replayed_action,
            "roi_pct": replayed_roi,
        })

    return results


def grid_search_thresholds(
    logs: Sequence[Any],
    buy_range: Sequence[float] | None = None,
    sell_range: Sequence[float] | None = None,
    min_change: float = 0.03,
) -> list[dict[str, Any]]:
    """Grid search over (buy_threshold, sell_threshold) to find optimal ROI.

    Default ranges:
      - buy:  [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
      - sell: [0.20, 0.25, 0.30, 0.35, 0.40]
    """
    if buy_range is None:
        buy_range = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    if sell_range is None:
        sell_range = [0.20, 0.25, 0.30, 0.35, 0.40]

    best: dict[str, Any] = {
        "buy_threshold": 0.70,
        "sell_threshold": 0.30,
        "weighted_roi": -999,
    }

    for bt in buy_range:
        for st in sell_range:
            if st >= bt:
                continue  # sell threshold must be lower than buy threshold
            replayed = replay_under_thresholds(logs, bt, st, min_change)
            rois = [r["roi_pct"] for r in replayed]
            avg_roi = sum(rois) / len(rois) if rois else 0.0
            if avg_roi > best["weighted_roi"]:
                best = {
                    "buy_threshold": bt,
                    "sell_threshold": st,
                    "weighted_roi": round(avg_roi, 4),
                    "num_decisions": len(rois),
                }

    return [best]
