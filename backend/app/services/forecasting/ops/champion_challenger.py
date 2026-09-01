"""P2.16: Champion/challenger shadow-run tracker.

One candidate model per product is shadow-run nightly. Its forecast is
persisted as a shadow entry in ChallengerShadowRun (DB). Realized MAPE
is tracked; auto-promotion rule (>1pp MAPE improvement over 3 consecutive
nights) writes ensemble_overrides into ForecastTarget.model_config.

The in-memory ChampionChallengerTracker is kept for single-run use; the
nightly loop uses run_nightly_champion_challenger for DB persistence.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Promotion criteria for the in-memory ChampionChallengerTracker (weekly granularity)
_MIN_CONSECUTIVE_WEEKS = 4
_MIN_IMPROVEMENT_PCT = 5.0

# Promotion criteria for nightly DB-backed auto-promotion (daily granularity)
_MIN_CONSECUTIVE_NIGHTS = 3
_MIN_IMPROVEMENT_PP = 1.0


@dataclass
class ShadowForecastResult:
    """A single shadow forecast from a challenger model."""
    product_id: str
    challenger_model: str
    forecast_value: float
    mase_estimate: float | None = None


@dataclass
class ChampionChallengerTracker:
    """Tracks shadow-run results and promotion eligibility.

    In production this would be backed by a DB table; for now it's
    in-memory per nightly run, with a promotion recommendation log.
    """

    shadows: list[ShadowForecastResult] = field(default_factory=list)
    # product_id → list of (challenger_mase, champion_mase) per week
    weekly_results: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    def record_shadow(self, shadow: ShadowForecastResult) -> None:
        """Record a shadow forecast (never published)."""
        self.shadows.append(shadow)
        logger.info(
            "Shadow forecast: %s / %s = %.2f (MASE=%.3f)",
            shadow.product_id, shadow.challenger_model,
            shadow.forecast_value, shadow.mase_estimate,
        )

    def record_weekly_result(
        self,
        product_id: str,
        challenger_mase: float,
        champion_mase: float,
    ) -> None:
        """Record one week's realized MASE for both challenger and champion."""
        if product_id not in self.weekly_results:
            self.weekly_results[product_id] = []
        self.weekly_results[product_id].append((challenger_mase, champion_mase))

    def check_promotion(self, product_id: str) -> dict | None:
        """Check if the challenger is eligible for promotion.

        Rule: >5% MASE improvement over 4 consecutive weeks.
        Returns a recommendation dict or None.
        """
        results = self.weekly_results.get(product_id, [])
        if len(results) < _MIN_CONSECUTIVE_WEEKS:
            return None

        # Check the last N weeks for consecutive improvement
        streak = 0
        for ch_mase, champ_mase in reversed(results[-_MIN_CONSECUTIVE_WEEKS * 2:]):
            improvement = (champ_mase - ch_mase) / champ_mase * 100 if champ_mase > 0 else 0
            if improvement > _MIN_IMPROVEMENT_PCT:
                streak += 1
            else:
                streak = 0

            if streak >= _MIN_CONSECUTIVE_WEEKS:
                # Compute average improvement over the streak
                recent = results[-streak:]
                avg_ch = sum(r[0] for r in recent) / len(recent)
                avg_champ = sum(r[1] for r in recent) / len(recent)
                improvement_pct = (avg_champ - avg_ch) / avg_champ * 100 if avg_champ > 0 else 0
                return {
                    "product_id": product_id,
                    "improvement_pct": round(improvement_pct, 2),
                    "consecutive_weeks": streak,
                    "avg_challenger_mase": round(avg_ch, 4),
                    "avg_champion_mase": round(avg_champ, 4),
                }

        return None


# ---------------------------------------------------------------------------
# Nightly-loop integration: DB-backed shadow runs + auto-promotion
# ---------------------------------------------------------------------------

def run_nightly_champion_challenger(db) -> dict:
    """Nightly champion/challenger step — auto-promotion from shadow runs.

    The engine.py nightly loop already persists stacking shadow MAPE to
    ChallengerShadowRun rows.  This function checks those rows for
    auto-promotion eligibility.

    Auto-promotion rule: challenger beats champion for
    _MIN_CONSECUTIVE_NIGHTS consecutive nights with delta > _MIN_IMPROVEMENT_PP.
    When promoted, ensemble_overrides are written into ForecastTarget.model_config
    so the next nightly forecast uses the stacking blend.

    Additionally, registers the cross-product panel model as a challenger
    candidate when it beats the default blend for a product.

    Returns {shadow_runs, promotions, errors}.
    """
    from app.models.forecasting import (
        ForecastTarget, ChallengerShadowRun,
    )

    now = _dt.datetime.now(_dt.timezone.utc)

    targets = (
        db.query(ForecastTarget)
        .filter(
            ForecastTarget.org_id == "default-org",
            ForecastTarget.status == "active",
            ForecastTarget.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    shadow_runs = 0
    promotions = 0
    errors = []

    for target in targets:
        try:
            product_key = target.product_key or target.name

            # Count recent shadow runs (already persisted by engine.py)
            recent_shadow = (
                db.query(ChallengerShadowRun)
                .filter(
                    ChallengerShadowRun.target_id == target.id,
                    ChallengerShadowRun.challenger_type == "stacking_meta",
                    ChallengerShadowRun.shadow_delta_mape.isnot(None),
                    ChallengerShadowRun.promoted == False,  # noqa: E712
                )
                .order_by(ChallengerShadowRun.run_date.desc())
                .limit(_MIN_CONSECUTIVE_NIGHTS + 2)
                .all()
            )
            shadow_runs += len(recent_shadow)

            if not recent_shadow:
                continue

            # Check auto-promotion: need _MIN_CONSECUTIVE_NIGHTS consecutive wins
            # where delta > _MIN_IMPROVEMENT_PP
            winning_streak = 0
            for run_row in recent_shadow:
                if (run_row.shadow_delta_mape is not None
                        and run_row.shadow_delta_mape >= _MIN_IMPROVEMENT_PP):
                    winning_streak += 1
                else:
                    break  # streak broken

            if winning_streak >= _MIN_CONSECUTIVE_NIGHTS:
                # Already promoted? Check if ensemble_overrides already set
                existing = (target.model_config or {}).get("ensemble_overrides", {})
                if existing.get("source") == "stacking_meta":
                    continue  # already promoted, skip

                # Auto-promote: override ensemble weights
                if target.model_config is None:
                    target.model_config = {}
                winning_runs = recent_shadow[:winning_streak]
                avg_delta = sum(r.shadow_delta_mape for r in winning_runs) / len(winning_runs)
                target.model_config["ensemble_overrides"] = {
                    "source": "stacking_meta",
                    "promoted_at": now.isoformat(),
                    "avg_delta_mape_pp": round(avg_delta, 2),
                    "consecutive_wins": winning_streak,
                    "weights": {"stacking": 0.6, "default_blend": 0.4},
                }
                # Mark shadow runs as promoted
                for r in winning_runs:
                    r.promoted = True
                db.add(target)
                promotions += 1
                logger.info(
                    "[champ/chall] PROMOTED stacking for %s: avg delta=%.2fpp over %d nights",
                    product_key, avg_delta, winning_streak,
                )

            # Also check for panel model shadow runs
            panel_shadow = (
                db.query(ChallengerShadowRun)
                .filter(
                    ChallengerShadowRun.target_id == target.id,
                    ChallengerShadowRun.challenger_type == "panel_xgboost",
                    ChallengerShadowRun.shadow_delta_mape.isnot(None),
                    ChallengerShadowRun.promoted == False,  # noqa: E712
                )
                .order_by(ChallengerShadowRun.run_date.desc())
                .limit(_MIN_CONSECUTIVE_NIGHTS + 2)
                .all()
            )
            shadow_runs += len(panel_shadow)

            if panel_shadow:
                panel_streak = 0
                for run_row in panel_shadow:
                    if (run_row.shadow_delta_mape is not None
                            and run_row.shadow_delta_mape >= _MIN_IMPROVEMENT_PP):
                        panel_streak += 1
                    else:
                        break

                if panel_streak >= _MIN_CONSECUTIVE_NIGHTS:
                    existing = (target.model_config or {}).get("ensemble_overrides", {})
                    if existing.get("source") in ("panel_xgboost", "stacking_meta"):
                        continue  # already has a promoted challenger

                    if target.model_config is None:
                        target.model_config = {}
                    winning_runs = panel_shadow[:panel_streak]
                    avg_delta = sum(r.shadow_delta_mape for r in winning_runs) / len(winning_runs)
                    target.model_config["ensemble_overrides"] = {
                        "source": "panel_xgboost",
                        "promoted_at": now.isoformat(),
                        "avg_delta_mape_pp": round(avg_delta, 2),
                        "consecutive_wins": panel_streak,
                        "weights": {"panel_xgboost": 0.5, "default_blend": 0.5},
                    }
                    for r in winning_runs:
                        r.promoted = True
                    db.add(target)
                    promotions += 1
                    logger.info(
                        "[champ/chall] PROMOTED panel_xgboost for %s: avg delta=%.2fpp over %d nights",
                        product_key, avg_delta, panel_streak,
                    )

        except Exception as exc:
            errors.append(f"{target.name}: {exc}")
            logger.warning("[champ/chall] failed for %s: %s", target.name, exc)

    if promotions > 0:
        db.commit()

    logger.info(
        "[champ/chall] Done: %d shadow runs checked, %d promotions, %d errors",
        shadow_runs, promotions, len(errors),
    )
    return {
        "shadow_runs": shadow_runs,
        "promotions": promotions,
        "errors": errors[:10],
    }
