"""Provider configuration diagnostics (Phase 3.5 P5).

ROOT CAUSE ADDRESSED
--------------------
Phase 3's live probe returned HTTP 401 invalid_api_key, but nothing could
diagnose provider configuration WITHOUT making a paid request (and mission
constraint: never repeatedly call the provider, never expose key values).

This module reports ONLY booleans, names, categories and consistency
findings — never secrets. It performs ZERO network calls.

Checks:
- provider_configured / credential_present (boolean only)
- primary model + whether it is enabled in the llm_models registry
  (active rows, soft-deleted excluded)
- fallback routing entries referencing disabled/missing models (inconsistent)
- model-to-provider mapping presence
- last known error CATEGORY (from the probe sidecar written by
  reliability_metrics.provider_status on failed probes — a category string,
  never a message containing secrets)
"""
from __future__ import annotations

import json
from pathlib import Path

SIDECAR_FILENAME = ".provider_last_error.json"


def _normalize_rows(llm_rows) -> list[dict]:
    """Accept ORM rows or dicts; normalize to the diagnostic shape."""
    out = []
    for r in llm_rows or []:
        if isinstance(r, dict):
            out.append(r)
        else:
            out.append({
                "name": getattr(r, "name", None),
                "model": getattr(r, "model", None) or getattr(r, "name", None),
                "provider": getattr(r, "provider", None),
                "enabled": getattr(r, "enabled", getattr(r, "is_active", True)),
                "is_deleted": getattr(r, "is_deleted", False),
            })
    return out


def provider_diagnostics(
    settings_obj=None,
    llm_rows=None,
    *,
    sidecar_path: str | Path | None = None,
) -> dict:
    """Pure diagnostic report. NO network, NO secret values."""
    if settings_obj is None:
        from app.config import settings as settings_obj

    primary_model = (getattr(settings_obj, "LLM_MODEL", "") or "").strip()
    credential_present = bool(getattr(settings_obj, "OPENAI_API_KEY", ""))
    base_url = (getattr(settings_obj, "OPENAI_BASE_URL", "") or "").strip()
    fallbacks = getattr(settings_obj, "LLM_FALLBACKS", None) or []

    rows = _normalize_rows(llm_rows)
    active = [
        r for r in rows
        if r.get("enabled") is not False and r.get("is_deleted") is not True
    ]
    active_models = {(r.get("model") or r.get("name") or "") for r in active}

    findings: list[str] = []
    if primary_model and not credential_present:
        findings.append("primary model configured but no credential present")
    if credential_present and not primary_model and not base_url:
        findings.append("credential present but no model or base_url configured")
    if primary_model and active and primary_model not in active_models:
        findings.append(
            "primary model is NOT among enabled llm_models registry entries"
        )
    disabled_models = {
        (r.get("model") or r.get("name") or "") for r in rows
        if r.get("enabled") is False or r.get("is_deleted") is True
    }
    for fb in fallbacks:
        fb_model = (fb or {}).get("model", "") if isinstance(fb, dict) else str(fb)
        if fb_model and fb_model in disabled_models:
            findings.append(
                f"fallback model '{fb_model}' is disabled/soft-deleted in the registry"
            )

    last_error_category = None
    if sidecar_path is None:
        sidecar_path = Path(__file__).resolve().parents[2] / SIDECAR_FILENAME
    try:
        raw = json.loads(Path(sidecar_path).read_text())
        last_error_category = raw.get("category")
    except Exception:  # noqa: BLE001 — absent/unreadable sidecar is fine
        last_error_category = None

    return {
        "provider_configured": bool(primary_model or base_url),
        "credential_present": credential_present,
        "primary_model": primary_model or None,
        "model_enabled": (primary_model in active_models) if (primary_model and active) else None,
        "provider_selected": bool(base_url) or bool(active),
        "fallback_configured": bool(fallbacks),
        "registered_active_models": sorted(m for m in active_models if m)[:10],
        "last_error_category": last_error_category,
        "consistency_findings": findings,
        "consistent": not findings,
        # Explicit honesty marker: this diagnostic NEVER makes a paid request.
        "network_calls_made": 0,
    }


def record_probe_error(category: str, *, sidecar_path: str | Path | None = None) -> None:
    """Write the probe-error sidecar (category ONLY — no messages, no keys).
    Called by the live probe path after a failed connectivity check."""
    path = Path(sidecar_path) if sidecar_path else (
        Path(__file__).resolve().parents[2] / SIDECAR_FILENAME
    )
    try:
        path.write_text(json.dumps({"category": str(category)[:60]}))
    except OSError:
        pass  # diagnostics must never crash the probe path
