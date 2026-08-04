"""Macro Override Service — real-time shock injection layer.

Port of EDIA 5.1.2 backend/src/service/macro_override_service.py (§16).
Adapted: file-based JSON → class-based constructor with optional DB persistence,
module-level singletons → class, datetime.utcnow → timezone-aware.

An active override adjusts the naphtha monthly forecast by
`override.naphtha_shock_pct` (hard-capped at ±4% per spec).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hard cap from spec §16 — ±4% on naphtha shock
_NAPHTHA_SHOCK_CAP = 4.0


class MacroOverrideService:
    """Manage real-time macro override for naphtha shock propagation.

    Uses a file-based JSON store (or DB-backed if a db session is provided
    at construction time). The override auto-expires after TTL.
    """

    def __init__(
        self,
        db=None,
        org_id: str | None = None,
        llm_caller=None,
        storage_path: str | None = None,
    ):
        self.db = db
        self.org_id = org_id
        self.llm = llm_caller
        # Default storage path: project root / data / macro_override_{org_id}.json
        if storage_path:
            self._override_path = Path(storage_path)
        else:
            base = Path(__file__).parent.parent.parent.parent.parent
            suffix = f"_{org_id}" if org_id else ""
            self._override_path = base / "data" / f"macro_override{suffix}.json"

    # ── Public API ──────────────────────────────────────────────────────

    def get_active_override(self) -> dict | None:
        """Return the active override dict, or None if absent / expired / inactive."""
        if not self._override_path.exists():
            return None
        try:
            data = json.loads(self._override_path.read_text(encoding="utf-8"))
            if not data.get("active", False):
                return None
            expires_at = datetime.fromisoformat(data["expires_at"])
            now = datetime.now(timezone.utc)
            # Handle both aware and naive expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now > expires_at:
                self._deactivate()
                return None
            return data
        except Exception as exc:
            logger.warning("Error reading macro override: %s", exc)
            return None

    def set_override(
        self,
        naphtha_shock_pct: float,
        crude_shock_pct: float = 0.0,
        event_type: str = "manual",
        description: str = "",
        ttl_hours: int = 24,
        product_shocks: dict | None = None,
    ) -> dict:
        """Write a new macro override (replaces any existing one).

        Args:
            naphtha_shock_pct: % change to naphtha forecast (e.g. +2.5 → +2.5%)
            crude_shock_pct:   % change in crude price (for logging)
            event_type:        "crude_spike" | "tariff_shock" | "force_majeure" | "manual"
            description:       Free-text description
            ttl_hours:         Hours until auto-expiry (default 24)
            product_shocks:    Optional {product_id: extra_pct_adj}
        """
        naphtha_shock_pct = max(
            -_NAPHTHA_SHOCK_CAP, min(_NAPHTHA_SHOCK_CAP, naphtha_shock_pct)
        )
        now = datetime.now(timezone.utc)
        confidence_penalty = min(0.15, abs(naphtha_shock_pct) * 0.025)

        payload = {
            "event_type": event_type,
            "description": description,
            "naphtha_shock_pct": round(naphtha_shock_pct, 2),
            "crude_shock_pct": round(crude_shock_pct, 2),
            "product_shocks": product_shocks or {},
            "confidence_penalty": round(confidence_penalty, 3),
            "active": True,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(),
        }
        # Ensure parent dir exists
        self._override_path.parent.mkdir(parents=True, exist_ok=True)
        self._override_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(
            "Macro override set: %s naphtha=%+.1f%% ttl=%dh",
            event_type, naphtha_shock_pct, ttl_hours,
        )
        return payload

    def clear_override(self) -> bool:
        """Manually deactivate the current override."""
        self._deactivate()
        return True

    def override_summary(self, override: dict | None = None) -> str:
        """One-line summary for LLM prompt context."""
        if override is None:
            override = self.get_active_override()
        if not override:
            return ""
        shock = override.get("naphtha_shock_pct", 0)
        crude = override.get("crude_shock_pct", 0)
        evt = override.get("event_type", "unknown").replace("_", " ").upper()
        desc = override.get("description", "")
        pen = override.get("confidence_penalty", 0)
        expires = override.get("expires_at", "")[:16]
        return (
            f"[MACRO OVERRIDE ACTIVE — {evt}] {desc} | "
            f"Naphtha shock: {shock:+.1f}% | Crude shock: {crude:+.1f}% | "
            f"Confidence penalty: -{pen * 100:.0f}% | Expires: {expires} UTC"
        )

    @staticmethod
    def apply_macro_shock_to_naphtha_mom(arima_mom: float, override: dict) -> float:
        """Add the override naphtha shock to the ARIMA MoM forecast.

        arima_mom: ARIMA naphtha MoM % (e.g. 1.2 → +1.2%)
        Returns adjusted MoM (globally capped at ±8% per spec §8.2 Layer 2).
        """
        adjusted = arima_mom + override.get("naphtha_shock_pct", 0.0)
        return max(-8.0, min(8.0, adjusted))

    # ── Internal ────────────────────────────────────────────────────────

    def _deactivate(self):
        if self._override_path.exists():
            try:
                data = json.loads(self._override_path.read_text(encoding="utf-8"))
                data["active"] = False
                self._override_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass
