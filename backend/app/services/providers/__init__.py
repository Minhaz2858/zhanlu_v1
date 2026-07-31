"""Provider Profile multi-vendor system — adapted from OpenHarness.

Manages named LLM provider profiles with independent API key/base_url/model.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProviderProfile:
    name: str
    api_key_env: str = ""
    base_url_env: str = ""
    model_env: str = ""
    default_model: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""

    def resolve(self) -> dict[str, str]:
        api_key = self.api_key or os.environ.get(self.api_key_env, "")
        base_url = self.base_url or os.environ.get(self.base_url_env, "")
        model = self.model or os.environ.get(self.model_env, "") or self.default_model
        return {"api_key": api_key, "base_url": base_url, "model": model}

    def to_dict(self) -> dict[str, Any]:
        r = self.resolve()
        return {"name": self.name, "default_model": self.default_model,
                "base_url": r["base_url"], "model": r["model"], "has_api_key": bool(r["api_key"])}


BUILTIN_PROFILES: dict[str, ProviderProfile] = {
    "deepseek": ProviderProfile(name="deepseek", api_key_env="DEEPSEEK_API_KEY", base_url_env="DEEPSEEK_BASE_URL", model_env="DEEPSEEK_MODEL", default_model="deepseek-v4-flash", base_url="https://api.deepseek.com/v1"),
    "openai": ProviderProfile(name="openai", api_key_env="OPENAI_API_KEY", base_url_env="OPENAI_BASE_URL", model_env="OPENAI_MODEL", default_model="gpt-4o-mini", base_url="https://api.openai.com/v1"),
    "claude": ProviderProfile(name="claude", api_key_env="ANTHROPIC_API_KEY", base_url_env="ANTHROPIC_BASE_URL", model_env="CLAUDE_MODEL", default_model="claude-3-5-sonnet-20241022", base_url="https://api.anthropic.com"),
    "moonshot": ProviderProfile(name="moonshot", api_key_env="MOONSHOT_API_KEY", base_url_env="MOONSHOT_BASE_URL", model_env="MOONSHOT_MODEL", default_model="moonshot-v1-128k", base_url="https://api.moonshot.cn/v1"),
    "gemini": ProviderProfile(name="gemini", api_key_env="GEMINI_API_KEY", base_url_env="GEMINI_BASE_URL", model_env="GEMINI_MODEL", default_model="gemini-pro", base_url="https://generativelanguage.googleapis.com/v1"),
    "qwen": ProviderProfile(name="qwen", api_key_env="QWEN_API_KEY", base_url_env="QWEN_BASE_URL", model_env="QWEN_MODEL", default_model="qwen-turbo", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
}


class ProviderManager:
    def __init__(self):
        self._profiles: dict[str, ProviderProfile] = dict(BUILTIN_PROFILES)
        self._active_profile: str = ""

    def get_profile(self, name: str) -> ProviderProfile | None:
        return self._profiles.get(name)

    def list_profiles(self) -> list[ProviderProfile]:
        return list(self._profiles.values())

    def add_profile(self, profile: ProviderProfile) -> None:
        self._profiles[profile.name] = profile

    def set_active(self, name: str) -> bool:
        if name not in self._profiles:
            return False
        self._active_profile = name
        return True

    def get_active(self) -> ProviderProfile | None:
        if not self._active_profile:
            return None
        return self._profiles.get(self._active_profile)

    def get_active_config(self) -> dict[str, str]:
        active = self.get_active()
        if active:
            return active.resolve()
        return {"api_key": os.environ.get("OPENAI_API_KEY", ""), "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), "model": os.environ.get("LLM_MODEL", "gpt-4o-mini")}


_manager: ProviderManager | None = None


def get_provider_manager() -> ProviderManager:
    global _manager
    if _manager is None:
        _manager = ProviderManager()
    return _manager


__all__ = ["ProviderProfile", "ProviderManager", "BUILTIN_PROFILES", "get_provider_manager"]
