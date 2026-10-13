"""Domain packs service — packaged vertical value (IBM Gap 5).

The IBM insurance article's deployment pattern: use-case-specific,
industry-packaged AI beats generic tools — and Zhanlu's SaaS play is
exactly that: package a vertical's vocabulary, starter questions, and
configuration into a pack a customer installs in one call.

A pack is a JSON manifest in ``backend/domain_packs/``:
  pack_key / name / version / industry / description
  starter_prompts: questions that show what the vertical can answer
  terminology: business vocabulary merged into the org's glossary
    (terminologies) so NL2SQL immediately speaks the industry's language

Installing a pack is IDEMPOTENT (re-install updates version + re-merges
missing glossary words) and org-scoped (one row per pack_key+org in
domain_pack_installs — the slot that already existed).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = ("pack_key", "name", "version", "industry", "description")


def _packs_dir() -> Path:
    """backend/domain_packs — resolved from this file (services/ → app/ → backend/)."""
    return Path(__file__).resolve().parents[2] / "domain_packs"


def _load_pack(pack_key: str) -> dict | None:
    """Load and validate one pack manifest."""
    path = (_packs_dir() / f"{pack_key}.json").resolve()
    # Confinement: the key must not escape the packs directory.
    if not str(path).startswith(str(_packs_dir().resolve())):
        return None
    if not path.exists() or path.suffix != ".json":
        return None
    try:
        pack = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        logger.warning("domain pack %s is invalid JSON: %s", pack_key, exc)
        return None
    if not all(k in pack for k in _REQUIRED_KEYS):
        logger.warning("domain pack %s missing required keys", pack_key)
        return None
    pack.setdefault("terminology", [])
    pack.setdefault("starter_prompts", [])
    return pack


def list_packs(db: Session, org_id: str) -> dict:
    """All available packs with this org's install state."""
    from app.models.forecasting import DomainPackInstall

    installed = {
        r.pack_key: r
        for r in db.query(DomainPackInstall)
        .filter(
            DomainPackInstall.org_id == org_id,
            DomainPackInstall.is_deleted == False,  # noqa: E712
        )
        .all()
    }
    packs = []
    for path in sorted(_packs_dir().glob("*.json")):
        try:
            manifest = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        key = manifest.get("pack_key")
        if not key or not all(k in manifest for k in _REQUIRED_KEYS):
            continue
        inst = installed.get(key)
        packs.append({
            "pack_key": key,
            "name": manifest["name"],
            "version": manifest["version"],
            "industry": manifest["industry"],
            "description": manifest["description"],
            "starter_prompts": manifest.get("starter_prompts", []),
            "terminology_count": len(manifest.get("terminology", [])),
            "installed": inst is not None,
            "installed_version": inst.pack_version if inst else None,
        })
    return {"packs": packs}


def install_pack(db: Session, org_id: str, app_id: str, pack_key: str) -> dict:
    """Install a pack for one org: record + merge glossary vocabulary.

    Idempotent: re-installing updates the version snapshot and re-merges
    any glossary words the org doesn't have yet (existing words are never
    modified — customer edits win).
    """
    from app.models.forecasting import DomainPackInstall
    from app.models.terminology import Terminology

    pack = _load_pack(pack_key)
    if pack is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=f"Domain pack {pack_key!r} not found")

    # 1) Glossary merge (dedupe by word within the org).
    existing_words = {
        w for (w,) in db.query(Terminology.word)
        .filter(Terminology.org_id == org_id, Terminology.is_deleted == False)  # noqa: E712
        .all()
    }
    added_terms = 0
    for term in pack.get("terminology", []):
        word = str(term.get("word") or "").strip()
        if not word or word in existing_words:
            continue
        db.add(Terminology(
            word=word,
            description=str(term.get("description") or ""),
            org_id=org_id,
            app_id=app_id,
        ))
        existing_words.add(word)
        added_terms += 1

    # 2) Install record (version snapshot for audit/re-install).
    row = (
        db.query(DomainPackInstall)
        .filter(
            DomainPackInstall.org_id == org_id,
            DomainPackInstall.pack_key == pack_key,
            DomainPackInstall.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if row is None:
        row = DomainPackInstall(org_id=org_id, app_id=app_id, pack_key=pack_key)
        db.add(row)
    row.pack_version = pack["version"]
    row.config = pack
    row.installed_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        "domain pack installed: %s v%s → org=%s (+%d glossary terms)",
        pack_key, pack["version"], org_id, added_terms,
    )
    return {
        "pack_key": pack_key,
        "name": pack["name"],
        "version": pack["version"],
        "glossary_terms_added": added_terms,
        "starter_prompts": pack.get("starter_prompts", []),
    }
