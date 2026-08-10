"""P3-1: Model state cache — save/load XGBoost booster to disk.

Provides simple file-based persistence for model state so that the nightly
forecast can warm-start from the previous night's trained model, rather than
training from scratch every time.

Storage layout: data/model_cache/{product_id}_{model_name}.json (XGBoost native)
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default cache directory relative to backend/
_DEFAULT_CACHE_DIR = "data/model_cache"


def _cache_dir() -> Path:
    """Resolve the cache directory (create if missing)."""
    env_override = os.environ.get("MODEL_CACHE_DIR")
    if env_override:
        p = Path(env_override)
    else:
        backend_root = Path(__file__).resolve().parent.parent.parent.parent
        p = backend_root / _DEFAULT_CACHE_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_state(
    model_obj: Any,
    product_id: str,
    model_name: str = "xgboost_reg",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Save a trained XGBoost booster to disk.

    Args:
        model_obj: An XGBoost XGBRegressor (or anything with .save_model()).
        product_id: Dashboard product key.
        model_name: Model identifier (default xgboost_reg).
        metadata: Optional dict serialized alongside the model.

    Returns:
        Absolute path to the saved file.
    """
    cache_dir = _cache_dir()
    fname = f"{product_id}_{model_name}.json"
    path = str(cache_dir / fname)

    # XGBoost native save — ensure _estimator_type is set (xgboost 2.1.x quirk)
    if not getattr(model_obj, "_estimator_type", None):
        model_obj._estimator_type = "regressor"
    model_obj.save_model(path)

    # Save metadata as sibling JSON
    meta_path = str(cache_dir / f"{product_id}_{model_name}_meta.json")
    import json
    meta = metadata or {}
    meta["saved_at"] = time.time()
    meta["product_id"] = product_id
    meta["model_name"] = model_name
    meta["path"] = path
    with open(meta_path, "w") as f:
        json.dump(meta, f, default=str)

    logger.info("[model-cache] saved %s to %s", model_name, path)
    return path


def load_state(
    product_id: str,
    model_name: str = "xgboost_reg",
    xgb_module: Any = None,
) -> tuple[Any | None, dict[str, Any]]:
    """Load a previously saved XGBoost booster.

    Args:
        product_id: Dashboard product key.
        model_name: Model identifier.
        xgb_module: The `xgboost` module (lazy import).

    Returns:
        (model | None, metadata_dict). model is None if no cache found.
    """
    cache_dir = _cache_dir()
    path = str(cache_dir / f"{product_id}_{model_name}.json")

    if not os.path.isfile(path):
        logger.debug("[model-cache] no cache for %s/%s", product_id, model_name)
        return None, {}

    try:
        if xgb_module is None:
            import xgboost as xgb_module
        model = xgb_module.XGBRegressor()
        # xgboost 2.1.x: _estimator_type must be set before load_model()
        model._estimator_type = "regressor"
        model.load_model(path)
    except Exception as exc:
        logger.warning("[model-cache] failed to load %s: %s", path, exc)
        return None, {}

    # Try loading metadata
    import json
    meta_path = str(cache_dir / f"{product_id}_{model_name}_meta.json")
    meta: dict[str, Any] = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception:
            pass

    logger.info("[model-cache] loaded %s from %s", model_name, path)
    return model, meta


def invalidate_cache(
    product_id: str,
    model_name: str = "xgboost_reg",
) -> None:
    """Remove cached model state for a product (force full retrain)."""
    cache_dir = _cache_dir()
    for fname in [
        f"{product_id}_{model_name}.json",
        f"{product_id}_{model_name}_meta.json",
    ]:
        p = cache_dir / fname
        if p.exists():
            p.unlink()
            logger.info("[model-cache] invalidated %s", p)
