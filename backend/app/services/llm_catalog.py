"""Well-known LLM provider catalog — seeds ``llm_models`` rows on startup.

Keeps the admin model UI useful out of the box: known cloud models (Kimi K2,
MiniMax M2, ...) appear with the correct ``base_url`` / ``context_window``
without the admin having to type them. Only rows whose ``model_id`` is absent
for the tenant are inserted (``missing-only``) so admin edits and manual rows
are never overwritten. API keys are never seeded — rows with ``api_key=NULL``
fall back to the global ``OPENAI_API_KEY`` in the resolver, or the admin
attaches a per-row key in the UI.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.llm_model import LlmModel

logger = logging.getLogger(__name__)

DEFAULT_ORG = "default-org"
DEFAULT_APP = "default-app"

# One entry per known model. ``model_id`` is the dedupe key per tenant.
LLM_CATALOG: list[dict[str, Any]] = [
    {
        "name": "Kimi K2 (Moonshot)",
        "model_id": "kimi-k2",
        "provider": "moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "is_private": False,
        "context_window": 131072,
        "max_output_tokens": None,
    },
    {
        "name": "MiniMax M2",
        "model_id": "MiniMax-M2",
        "provider": "minimax",
        "base_url": "https://api.minimax.io/v1",
        "is_private": False,
        "context_window": 204800,
        "max_output_tokens": None,
    },
]


def should_seed_catalog(seed_mode: str | None = None) -> bool:
    """Whether catalog rows should be seeded on startup.

    ``seed_mode`` overrides ``settings.LLM_CATALOG_SEED_MODE``; an empty
    value falls back to ``"missing-only"`` (seed rows that are missing).
    Only ``"skip"`` (case-insensitive) disables seeding.
    """
    mode = (
        seed_mode
        or getattr(settings, "LLM_CATALOG_SEED_MODE", "")
        or "missing-only"
    )
    return mode.strip().lower() != "skip"


def upsert_catalog_row(
    db: Session,
    entry: dict[str, Any],
    *,
    org_id: str = DEFAULT_ORG,
    app_id: str = DEFAULT_APP,
) -> LlmModel | None:
    """Insert ``entry`` when no row with the same ``model_id`` exists for the
    tenant. Never updates existing rows (admin edits survive reseeding).

    Returns the created row, or ``None`` when a row already exists.
    """
    model_id = entry["model_id"]
    existing = (
        db.query(LlmModel)
        .filter(
            LlmModel.model_id == model_id,
            LlmModel.org_id == org_id,
            LlmModel.app_id == app_id,
            LlmModel.is_deleted.is_(False),
        )
        .first()
    )
    if existing is not None:
        return None

    row = LlmModel(
        name=entry.get("name") or model_id,
        model_id=model_id,
        provider=entry.get("provider") or "custom",
        base_url=entry["base_url"],
        api_key=None,  # never seed secrets — resolver falls back to global key
        is_private=bool(entry.get("is_private", False)),
        is_default=False,  # admins decide the default explicitly
        enabled=True,
        bypass_hallucination_guardrail=False,
        context_window=entry.get("context_window"),
        max_output_tokens=entry.get("max_output_tokens"),
        supports_structured_tool_calls=True,
        org_id=org_id,
        app_id=app_id,
    )
    db.add(row)
    db.flush()
    logger.info("Seeded LLM catalog row %s (%s)", model_id, entry.get("base_url", ""))
    return row


def seed_llm_catalog(
    db: Session,
    *,
    org_id: str = DEFAULT_ORG,
    app_id: str = DEFAULT_APP,
) -> list[LlmModel]:
    """Insert all missing catalog rows for the tenant. Idempotent."""
    created: list[LlmModel] = []
    for entry in LLM_CATALOG:
        row = upsert_catalog_row(db, entry, org_id=org_id, app_id=app_id)
        if row is not None:
            created.append(row)
    if created:
        db.commit()
    return created
