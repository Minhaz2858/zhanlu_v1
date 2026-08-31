"""Canonical forecast read-model shared by the market dashboard and chat agents.

Both the dashboard and the BI chat agent
(``forecast_get`` / ``forecast_run`` tools) read the SAME ``ForecastRun``
table, so for a given target + horizon there is exactly ONE correct number:
the first point of that horizon's ``base`` curve in the latest persisted run.
This module is the single implementation of that contract — horizon-key
selection, curve slicing, and the answer block both surfaces quote from.

Contract:
- "next week" == the 7-day horizon (``DEFAULT_FORECAST_DAY``), matching the
  dashboard card default.
- ``point_estimate`` == ``results[horizon]["base"][0]`` — identical to what
  ``get_forecast_base_batch_v2`` returns for the same product + day.
- ``explanation`` is passed through verbatim from the persisted run so the
  chat narrative matches the dashboard's analysis block.
"""

from __future__ import annotations

from typing import Any

DEFAULT_FORECAST_DAY = 7  # "next week" — the dashboard card default


def pick_horizon_payload(results: dict, days: int) -> tuple[dict, int | None]:
    """Return ``(horizon_payload, effective_horizon_days)`` for ``days``.

    Preference: exact key ``f"{days}d"`` → ``str(days)``; otherwise the
    nearest available numeric horizon key (ties → larger key).  Returns
    ``({}, None)`` if no usable horizon is found.
    """
    if not results:
        return {}, None

    # Exact-key preference
    for key in (f"{days}d", str(days)):
        payload = results.get(key)
        if isinstance(payload, dict) and payload:
            return payload, days

    # Nearest-horizon fallback: parse numeric horizon keys
    numeric_keys: list[int] = []
    for k in results:
        try:
            numeric_keys.append(int(str(k).rstrip("d")))
        except (ValueError, TypeError, AttributeError):
            continue

    if not numeric_keys:
        return {}, None

    # Closest absolute distance; ties → larger horizon (more data to slice from)
    best_h = max(numeric_keys, key=lambda h: (-abs(h - days), h))
    payload = results.get(f"{best_h}d") or results.get(str(best_h)) or {}
    if isinstance(payload, dict) and payload:
        return payload, best_h

    return {}, None


def select_headline_day(horizons: list[int]) -> int:
    """Pick the horizon a chat answer should lead with.

    Prefers the 7-day horizon ("next week" — the number the dashboard card
    shows); otherwise the smallest requested horizon.
    """
    normalized = [int(h) for h in horizons] or [DEFAULT_FORECAST_DAY]
    if DEFAULT_FORECAST_DAY in normalized:
        return DEFAULT_FORECAST_DAY
    return min(normalized)


def curve_to_float_list(curve: Any) -> list[float] | None:
    """Normalise a curve value (list / scalar / None) into a list of floats."""
    if isinstance(curve, list) and len(curve) > 0:
        return [float(v) for v in curve]
    if isinstance(curve, (int, float)):
        return [float(curve)]
    return None


def format_run_answer(run, day: int = DEFAULT_FORECAST_DAY) -> dict:
    """Build the canonical answer block for one persisted ``ForecastRun``.

    Args:
        run: A ``ForecastRun`` ORM row.
        day: Requested horizon in days (default 7 == "next week").

    Returns a dict with:
        point_estimate:  base[0] for the requested horizon — the exact value
                         the dashboard card shows for this product + day.
        scenarios:       base/bull/bear curves (sliced to ``day`` points when
                         a larger fallback horizon had to be used, mirroring
                         the dashboard chart).
        effective_horizon: the horizon actually used (== day on exact match).
        explanation:     the run's stored explanation dict, verbatim.
    """
    results = run.results or {}
    horizon_payload, effective_horizon = pick_horizon_payload(results, day)

    scenarios: dict[str, list[float]] = {}
    for key in ("base", "bull", "bear"):
        curve = curve_to_float_list(horizon_payload.get(key))
        if curve is not None and effective_horizon is not None and effective_horizon > day:
            curve = curve[:day]
        if curve is not None:
            scenarios[key] = curve

    base_curve = scenarios.get("base") or []
    as_of = getattr(run, "as_of_date", None) or getattr(run, "created_date", None)

    return {
        "target_id": run.target_id,
        "run_id": run.id,
        "day": day,
        "effective_horizon": effective_horizon,
        "point_estimate": base_curve[0] if base_curve else None,
        "scenarios": scenarios,
        "as_of_date": as_of.isoformat() if as_of else None,
        "confidence": run.confidence,
        "below_naive_baseline": run.below_naive_baseline,
        "explanation": getattr(run, "explanation", None) or {},
        "model_detail": run.model_detail,
    }
