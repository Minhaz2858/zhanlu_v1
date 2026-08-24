"""Nightly fine-tuning orchestrator for Chronos-Bolt prompt tuning.

Called from the nightly scheduled task loop. Orchestrates:
  1. Load price series from DB (active ForecastTarget products)
  2. Build pooled dataset via dataset_builder
  3. Train soft-prompt tokens via prompt_tuning
  4. Save trained tokens to disk for next forecast run
  5. Log metrics for monitoring
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Default output directory for fine-tuned assets
_DEFAULT_OUTPUT_DIR = os.path.join(
    os.environ.get("FORECAST_FINETUNING_DIR", "/tmp/forecast_finetuning"),
)


def run_nightly_finetuning(db, output_dir: str | None = None) -> dict[str, Any]:
    """Run the nightly fine-tuning pipeline.

    Steps:
      1. Load all active target price series from DB
      2. Build pooled training dataset
      3. Split into train / temporal holdout
      4. Train soft-prompt tokens on Chronos-Bolt
      5. Save trained tokens + metrics to output_dir
      6. Return summary dict

    Parameters
    ----------
    db : Session
        SQLAlchemy DB session.
    output_dir : str | None
        Directory to save fine-tuned assets. Defaults to FORECAST_FINETUNING_DIR env var.

    Returns
    -------
    dict
        Summary with keys: products_loaded, samples_built, train_samples,
        holdout_samples, epochs, final_loss, output_path, errors.
    """
    from app.config import settings
    from app.models.forecasting import ForecastTarget, ForecastRun
    from app.services.forecasting.finetuning.dataset_builder import (
        build_pooled_dataset,
        split_temporal_holdout,
    )
    from app.services.forecasting.finetuning.prompt_tuning import (
        PromptTuningConfig,
        train_soft_prompts,
    )

    if not settings.FORECAST_FINETUNING_ENABLED:
        logger.info("Fine-tuning disabled (FORECAST_FINETUNING_ENABLED=false)")
        return {"status": "disabled"}

    out_dir = Path(output_dir or _DEFAULT_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    summary: dict[str, Any] = {
        "products_loaded": 0,
        "samples_built": 0,
        "train_samples": 0,
        "holdout_samples": 0,
        "epochs": 0,
        "final_loss": None,
        "output_path": str(out_dir),
        "errors": [],
    }

    # Step 1: Load price series from DB
    try:
        series_map = _load_price_series_from_db(db)
        summary["products_loaded"] = len(series_map)
        if not series_map:
            logger.warning("No price series loaded — skipping fine-tuning")
            summary["errors"].append("no_price_series")
            return summary
    except Exception as exc:
        errors.append(f"load_price_series: {exc}")
        summary["errors"] = errors
        return summary

    # Step 2: Build pooled dataset
    try:
        config = PromptTuningConfig(
            n_prompt_tokens=settings.FORECAST_FINETUNING_PROMPT_TOKENS
            if hasattr(settings, "FORECAST_FINETUNING_PROMPT_TOKENS")
            else 16,
            lr=settings.FORECAST_FINETUNING_LR
            if hasattr(settings, "FORECAST_FINETUNING_LR")
            else 1e-3,
            epochs=settings.FORECAST_FINETUNING_EPOCHS
            if hasattr(settings, "FORECAST_FINETUNING_EPOCHS")
            else 5,
            batch_size=4,
            context_len=60,
            horizon=7,
        )

        samples = build_pooled_dataset(
            series_map,
            context_len=config.context_len,
            horizon=config.horizon,
        )
        summary["samples_built"] = len(samples)

        if len(samples) < 10:
            logger.warning("Too few samples (%d) — skipping fine-tuning", len(samples))
            summary["errors"].append("too_few_samples")
            return summary

    except Exception as exc:
        errors.append(f"build_dataset: {exc}")
        summary["errors"] = errors
        return summary

    # Step 3: Split into train / holdout
    try:
        train, holdout = split_temporal_holdout(samples, holdout_days=14)
        summary["train_samples"] = len(train)
        summary["holdout_samples"] = len(holdout)
    except Exception as exc:
        errors.append(f"split_holdout: {exc}")
        summary["errors"] = errors
        return summary

    # Step 4: Train soft-prompt tokens
    try:
        result = train_soft_prompts(train, config)
        summary["epochs"] = config.epochs
        summary["final_loss"] = result["loss_history"][-1] if result["loss_history"] else None

        # Step 5: Save trained tokens + metadata
        timestamp = _dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        tokens_path = out_dir / f"prompt_tokens_{timestamp}.npy"
        meta_path = out_dir / f"prompt_meta_{timestamp}.json"

        np.save(str(tokens_path), result["prompt_tokens"])

        meta = {
            "timestamp": timestamp,
            "config": {
                "n_prompt_tokens": config.n_prompt_tokens,
                "lr": config.lr,
                "epochs": config.epochs,
                "context_len": config.context_len,
                "horizon": config.horizon,
            },
            "train_samples": len(train),
            "holdout_samples": len(holdout),
            "loss_history": result["loss_history"],
            "final_loss": summary["final_loss"],
            "products_used": list(series_map.keys()),
        }
        with open(str(meta_path), "w") as f:
            json.dump(meta, f, indent=2, default=str)

        # Update symlink to latest
        latest_tokens = out_dir / "prompt_tokens_latest.npy"
        latest_meta = out_dir / "prompt_meta_latest.json"
        # Remove old symlinks if they exist
        for p in [latest_tokens, latest_meta]:
            if p.exists() or p.is_symlink():
                p.unlink()
        latest_tokens.symlink_to(tokens_path.name)
        latest_meta.symlink_to(meta_path.name)

        logger.info(
            "Fine-tuning complete: %d samples, %d epochs, final_loss=%.4f → %s",
            len(train), config.epochs,
            summary["final_loss"] or 0,
            str(tokens_path),
        )

    except ImportError as exc:
        errors.append(f"torch/chronos not installed: {exc}")
        logger.warning("Fine-tuning skipped: %s", exc)
    except Exception as exc:
        errors.append(f"train: {exc}")
        logger.warning("Fine-tuning training failed: %s", exc)

    summary["errors"] = errors
    return summary


def _load_price_series_from_db(db) -> dict[str, "pd.Series"]:
    """Load price series for all active ForecastTargets from DB.

    Uses the most recent ForecastRun's actual_price values when available,
    falls back to the target's historical series.

    Returns dict[product_key, pd.Series].
    """
    import pandas as pd
    from app.models.forecasting import ForecastTarget, ForecastRun

    targets = (
        db.query(ForecastTarget)
        .filter(
            ForecastTarget.org_id == "default-org",
            ForecastTarget.status == "active",
            ForecastTarget.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    series_map: dict[str, pd.Series] = {}

    for target in targets:
        product_key = target.product_key or target.name
        try:
            # Try to get price history from ForecastRun actuals
            runs = (
                db.query(ForecastRun)
                .filter(
                    ForecastRun.target_id == target.id,
                    ForecastRun.actual_price.isnot(None),
                )
                .order_by(ForecastRun.forecast_date.desc())
                .limit(365)  # last year
                .all()
            )

            if runs:
                dates = []
                prices = []
                for run in sorted(runs, key=lambda r: r.forecast_date):
                    if run.forecast_date and run.actual_price is not None:
                        dates.append(run.forecast_date)
                        prices.append(float(run.actual_price))
                if len(prices) >= 60:
                    series_map[product_key] = pd.Series(
                        prices, index=dates, name=product_key
                    )
                    continue

            # Fallback: try to get from the raw price table
            # (This would be the lz_v_* views in production)
            logger.debug(
                "No sufficient ForecastRun actuals for %s — skipping",
                product_key,
            )

        except Exception as exc:
            logger.warning("Failed to load series for %s: %s", product_key, exc)

    return series_map


def load_latest_prompt_tokens(output_dir: str | None = None) -> np.ndarray | None:
    """Load the most recently trained prompt tokens from disk.

    Returns None if no tokens found.
    """
    out_dir = Path(output_dir or _DEFAULT_OUTPUT_DIR)
    latest = out_dir / "prompt_tokens_latest.npy"
    if latest.exists():
        return np.load(str(latest))
    return None
