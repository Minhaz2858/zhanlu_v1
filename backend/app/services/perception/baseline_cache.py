"""Per-product weekly volatility baseline cache (read-through, LLM-backed).

Each product gets an LLM-judged ``baseline_pct`` = its "typical" absolute
7-day price change. The SmartEvaluator multiplies this baseline by the user's
``threshold_multiplier`` to derive the per-product trigger, so a volatile
product does not fire alerts on its own normal noise.

Design goals:
  - Trigger-time evaluation is pure math over the cached baseline — no LLM.
  - LLM runs only on cache miss / stale row, fire-and-forget via
    ``asyncio.create_task`` — the scan cycle never blocks on the LLM.
  - On LLM failure or invalid JSON, a safe default (2.5%) is persisted and
    the scan continues. Graceful degradation is the contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.alert_baseline import AlertProductBaseline
from app.services.llm_service import call_llm

logger = logging.getLogger(__name__)

# Module-level constant kept for tests that import it directly.
DEFAULT_BASELINE_PCT = settings.ALERT_BASELINE_DEFAULT_PCT

_JSON_MARKER_START = "{"


class BaselineCache:
    """Read-through cache of per-product volatility baselines."""

    def __init__(self, db: Session) -> None:
        self.db = db
        # Guard against duplicate fire-and-forget refreshes for the same product.
        self._pending: dict[str, asyncio.Task] = {}

    # ── Public API ───────────────────────────────────────────────────────

    def get_or_compute(self, product: dict) -> AlertProductBaseline:
        """Return the baseline for one product.

        Fresh row → return it. Missing/stale row → seed the safe default,
        schedule an async LLM recompute (fire-and-forget), return the default.
        """
        pid = product["product_id"]
        row = self._load_fresh(pid)
        if row is not None:
            return row

        # Cache miss (or stale): seed default immediately, never block on LLM.
        if row is None:
            row = self._seed_default(pid)
        else:
            self._reset_stale_row(row)

        if settings.ALERT_BASELINE_LLM_ENABLED:
            self._schedule_refresh(product)
        return row

    async def refresh(self, product: dict) -> AlertProductBaseline:
        """Awaitable LLM recompute (used by tests and the background task).

        On failure/invalid JSON, falls back to the default baseline and
        returns a persisted row so callers always get a usable baseline.
        """
        pid = product["product_id"]
        try:
            parsed, model_used = await self._judge(product)
            baseline_pct = self._parse_baseline(parsed)
            reasoning = str(parsed.get("reasoning") or "")[:500] or None

            row = self.db.get(AlertProductBaseline, pid)
            if row is None:
                row = AlertProductBaseline(
                    product_id=pid,
                    baseline_pct=baseline_pct,
                    reasoning=reasoning,
                    computed_at=datetime.now(timezone.utc),
                    model_used=model_used,
                )
                self.db.add(row)
            else:
                row.baseline_pct = baseline_pct
                row.reasoning = reasoning
                row.computed_at = datetime.now(timezone.utc)
                row.model_used = model_used
            self.db.commit()
            return row
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the scan
            logger.warning(
                "baseline LLM refresh failed for %s (using default %.2f%%): %s",
                pid, DEFAULT_BASELINE_PCT, exc,
            )
            return self._seed_default(pid)

    def get_many(self, products: list[dict]) -> dict[str, AlertProductBaseline]:
        """Batch read-through: {product_id: baseline} for a snapshot."""
        return {p["product_id"]: self.get_or_compute(p) for p in products}

    # ── Internals ────────────────────────────────────────────────────────

    def _load_fresh(self, pid: str) -> AlertProductBaseline | None:
        row = self.db.get(AlertProductBaseline, pid)
        if row is None or row.computed_at is None:
            return None
        age = (datetime.now(timezone.utc) - row.computed_at).total_seconds()
        if age > settings.ALERT_BASELINE_TTL_SECONDS:
            return None
        return row

    def _seed_default(self, pid: str) -> AlertProductBaseline:
        row = self.db.get(AlertProductBaseline, pid)
        if row is None:
            row = AlertProductBaseline(
                product_id=pid,
                baseline_pct=DEFAULT_BASELINE_PCT,
                reasoning=None,
                computed_at=datetime.now(timezone.utc),
                model_used="seed_default",
            )
            self.db.add(row)
        else:
            row.baseline_pct = DEFAULT_BASELINE_PCT
            row.computed_at = datetime.now(timezone.utc)
            row.model_used = "seed_default"
        self.db.commit()
        return row

    def _reset_stale_row(self, row: AlertProductBaseline) -> None:
        row.baseline_pct = DEFAULT_BASELINE_PCT
        row.computed_at = datetime.now(timezone.utc)
        row.model_used = "seed_default"
        self.db.commit()

    def _schedule_refresh(self, product: dict) -> None:
        pid = product["product_id"]
        task = self._pending.get(pid)
        if task is not None and not task.done():
            return  # already in flight
        try:
            self._pending[pid] = asyncio.create_task(self.refresh(product))
        except RuntimeError:  # no running event loop (sync context) — skip
            logger.info("no running loop for baseline refresh of %s — skipped", pid)

    async def _judge(self, product: dict) -> dict:
        """Ask the LLM for the product's typical weekly volatility baseline."""
        prompt = (
            "You are a petrochemical market analyst. Judge the TYPICAL weekly "
            "(7-day) absolute price volatility for one product, so a monitoring "
            "system can filter out normal noise and alert only on anomalies.\n\n"
            f"Product: {product.get('product_name')} ({product.get('product_id')})\n"
            f"Current price: {product.get('current_price')} CNY/ton\n"
            f"Latest 7-day change: {product.get('change_7d_pct')}%\n\n"
            "Respond with STRICT JSON only, no markdown, exactly:\n"
            '{"baseline_pct": <float 0.5–15.0, typical |7d change| in percent>,\n'
            ' "reasoning": "<one short Chinese sentence why>"}\n'
            "If uncertain, prefer a slightly LOWER baseline (more alerts) over a "
            "high one (missed alerts)."
        )
        result = await call_llm(
            prompt=prompt,
            temperature=0.2,
            task_type="alert_baseline",
        )
        model_used = str(result.get("model") or "unknown")
        response = result.get("response") or result.get("data") or {}
        if isinstance(response, dict):
            return response, model_used
        text = str(response).strip()
        if not text:
            raise ValueError("empty LLM response")
        start = text.find(_JSON_MARKER_START)
        if start < 0:
            raise ValueError(f"no JSON object in LLM response: {text[:200]}")
        return json.loads(text[start:]), model_used

    @staticmethod
    def _parse_baseline(parsed: dict) -> float:
        value = float(parsed.get("baseline_pct"))
        return min(max(value, 0.5), 15.0)  # clamp to sane range
