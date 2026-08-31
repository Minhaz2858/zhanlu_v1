"""Per-user agent profile store (experience layer Phase A / Layer 3).

Tracks, per (agent_app_id, user_id):
- preferred language (zh / en, detected from user content)
- frequently asked products (matched against known product names)
- preferred depth (brief / standard / detailed)
- explicit thumbs feedback counts + preference overrides

Learned implicitly from user content and explicitly from feedback, then
injected into the system prompt so reports match this user's preferences.

Storage: one JSON file per (agent, user) — same lightweight pattern as the
learning graph. Best-effort: any failure is logged and skipped.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.services.domain_config import get_domain_config

logger = logging.getLogger(__name__)

_DEFAULT_STORAGE_DIR = os.path.join(
    os.environ.get("ZHANLU_DATA_DIR", "/root/zhanlu"),
    "user_profiles",
)

# Product aliases come from the app's domain configuration (key
# ``product_aliases``: product_id -> [user-language aliases]).  With no
# config present no products are tracked (fully generic behavior).
def _get_product_aliases() -> dict[str, list[str]]:
    """Load product aliases from the app's domain config (fail-soft)."""
    cfg = get_domain_config("") or {}
    raw = cfg.get("product_aliases") or {}
    out: dict[str, list[str]] = {}
    for pid, aliases in raw.items():
        if isinstance(aliases, (list, tuple)):
            cleaned = [str(a) for a in aliases if a is not None and str(a).strip()]
            if cleaned:
                out[str(pid)] = cleaned
    return out

# Chinese char range — used for language detection
_CN_RE = re.compile(r"[\u4e00-\u9fff]")
_EN_RE = re.compile(r"[a-zA-Z]")

_DEPTH_DETAILED = {"详细", "深入", "详尽", "完整", "全面"}
_DEPTH_BRIEF = {"简单", "简要", "简洁", "略", "概括", "几句"}


@dataclass
class ProductCount:
    name: str
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "count": self.count}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProductCount":
        return cls(name=str(data.get("name", "")), count=int(data.get("count", 0)))


@dataclass
class UserProfile:
    """Per (agent, user) profile."""
    agent_app_id: str
    user_id: str
    language: str = ""                    # "zh" | "en" | ""
    top_products: list[ProductCount] = field(default_factory=list)
    depth_pref: str = ""                  # "brief" | "standard" | "detailed"
    thumbs_up: int = 0
    thumbs_down: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_app_id": self.agent_app_id,
            "user_id": self.user_id,
            "language": self.language,
            "top_products": [p.to_dict() for p in self.top_products],
            "depth_pref": self.depth_pref,
            "thumbs_up": self.thumbs_up,
            "thumbs_down": self.thumbs_down,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserProfile":
        products = [
            ProductCount.from_dict(item)
            for item in (data.get("top_products") or [])
            if isinstance(item, dict)
        ]
        return cls(
            agent_app_id=str(data.get("agent_app_id", "")),
            user_id=str(data.get("user_id", "")),
            language=str(data.get("language", "")),
            top_products=products,
            depth_pref=str(data.get("depth_pref", "")),
            thumbs_up=int(data.get("thumbs_up", 0)),
            thumbs_down=int(data.get("thumbs_down", 0)),
            updated_at=str(data.get("updated_at", "")),
        )


# -- Storage ---------------------------------------------------------------- #

def _get_storage_path(agent_app_id: str, user_id: str, storage_dir: str | None = None) -> Path:
    base = Path(storage_dir or _DEFAULT_STORAGE_DIR)
    base.mkdir(parents=True, exist_ok=True)
    safe_agent = agent_app_id.replace("/", "_").replace("\\", "_")
    safe_user = user_id.replace("/", "_").replace("\\", "_")
    return base / f"{safe_agent}.{safe_user}.json"


def get_user_profile(
    agent_app_id: str,
    user_id: str,
    storage_dir: str | None = None,
) -> UserProfile:
    """Load a user profile from disk. Returns a fresh empty profile if missing."""
    path = _get_storage_path(agent_app_id, user_id, storage_dir)
    if not path.exists():
        return UserProfile(agent_app_id=agent_app_id, user_id=user_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        p = UserProfile.from_dict(data)
        p.agent_app_id = agent_app_id
        p.user_id = user_id
        return p
    except Exception as e:
        logger.warning("Failed to load profile %s/%s: %s", agent_app_id, user_id, e)
        return UserProfile(agent_app_id=agent_app_id, user_id=user_id)


def _save_profile(profile: UserProfile, storage_dir: str | None = None) -> None:
    profile.updated_at = datetime.now(timezone.utc).isoformat()
    path = _get_storage_path(profile.agent_app_id, profile.user_id, storage_dir)
    try:
        path.write_text(
            json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save profile %s/%s: %s", profile.agent_app_id, profile.user_id, e)


# -- Learning ---------------------------------------------------------------- #

def _detect_language(text: str) -> str:
    """Detect zh / en from user content. zh wins on mixed content."""
    if _CN_RE.search(text):
        return "zh"
    if _EN_RE.search(text):
        return "en"
    return ""


def _match_products(text: str) -> list[str]:
    """Return product IDs found in the text (Chinese or English aliases).

    The alias table comes from the app's domain config (``product_aliases``);
    with no config, no products are matched.
    """
    lowered = text.lower()
    found: list[str] = []
    for pid, aliases in _get_product_aliases().items():
        for alias in aliases:
            a = alias.lower()
            if a in lowered or alias in text:
                found.append(pid)
                break
    return found


def _detect_depth(text: str) -> str:
    if any(kw in text for kw in _DEPTH_DETAILED):
        return "detailed"
    if any(kw in text for kw in _DEPTH_BRIEF):
        return "brief"
    return ""


def update_user_profile(
    agent_app_id: str,
    user_id: str,
    user_content: str = "",
    storage_dir: str | None = None,
) -> None:
    """Update a user profile from user content (rule-based, post-turn).

    Best-effort: extracts language, product mentions, and depth preference.
    Called after each turn so the profile reflects the user's behavior.
    """
    try:
        profile = get_user_profile(agent_app_id, user_id, storage_dir)
        text = (user_content or "").strip()

        if text:
            # Language (zh wins on mixed)
            detected_lang = _detect_language(text)
            if detected_lang and profile.language != detected_lang:
                # Only upgrade: zh takes precedence, don't downgrade zh->en
                if not profile.language or detected_lang == "zh":
                    profile.language = detected_lang

            # Products
            for product_zh in _match_products(text):
                match = next(
                    (p for p in profile.top_products if p.name == product_zh),
                    None,
                )
                if match is None:
                    profile.top_products.append(ProductCount(name=product_zh, count=1))
                else:
                    match.count += 1

            # Depth preference (implicit, from phrasing)
            detected_depth = _detect_depth(text)
            if detected_depth and not profile.depth_pref:
                profile.depth_pref = detected_depth

        _save_profile(profile, storage_dir)
    except Exception as e:
        logger.debug("Failed to update profile: %s", e)


def add_feedback(
    agent_app_id: str,
    user_id: str,
    rating: int,
    detail_pref: Optional[str] = None,
    product: Optional[str] = None,
    storage_dir: str | None = None,
) -> None:
    """Update a user profile from explicit thumbs feedback (Phase C)."""
    try:
        profile = get_user_profile(agent_app_id, user_id, storage_dir)
        if rating > 0:
            profile.thumbs_up += 1
        elif rating < 0:
            profile.thumbs_down += 1
        if detail_pref:
            profile.depth_pref = detail_pref
        if product:
            match = next((p for p in profile.top_products if p.name == product), None)
            if match is None:
                profile.top_products.append(ProductCount(name=product, count=1))
            else:
                match.count += 1
        _save_profile(profile, storage_dir)
    except Exception as e:
        logger.debug("Failed to add feedback: %s", e)


def get_profile_prompt(
    agent_app_id: str,
    user_id: str,
    storage_dir: str | None = None,
) -> str:
    """Build the user-profile text for system prompt injection."""
    profile = get_user_profile(agent_app_id, user_id, storage_dir)

    parts: list[str] = []
    if profile.language:
        lang_label = {"zh": "中文", "en": "English"}.get(profile.language, profile.language)
        parts.append(f"Preferred language: {lang_label}")

    products = sorted(profile.top_products, key=lambda p: -p.count)[:5]
    if products:
        names = "、".join(p.name for p in products)
        parts.append(f"Frequently asked products: {names}")

    if profile.depth_pref:
        parts.append(f"Report depth preference: {profile.depth_pref}")

    if not parts:
        return ""

    lines = ["[User preferences]"]
    lines.extend(f"  - {p}" for p in parts)
    return "\n".join(lines)
