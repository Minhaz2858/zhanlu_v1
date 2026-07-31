"""Plugin system — adapted from OpenHarness.

Loads plugins from directories with plugin.json manifests.
Plugins can contribute: commands, hooks, agents, skills, tools.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PluginManifest:
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    tools_dir: str = ""
    commands: list[str] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    enabled: bool = True
    base_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version, "description": self.description,
            "author": self.author, "tools_dir": self.tools_dir,
            "commands": self.commands, "hooks": self.hooks, "agents": self.agents,
            "skills": self.skills, "enabled": self.enabled, "base_dir": self.base_dir,
        }


class PluginLoader:
    def __init__(self, plugins_dir: str = "plugins"):
        self.plugins_dir = Path(plugins_dir)
        self._plugins: dict[str, PluginManifest] = {}

    def load(self) -> dict[str, PluginManifest]:
        if not self.plugins_dir.exists():
            return self._plugins
        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue
            manifest_path = plugin_dir / "plugin.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = self._parse_manifest(manifest_path)
                if manifest:
                    manifest.base_dir = str(plugin_dir)
                    self._plugins[manifest.name] = manifest
                    logger.info("Loaded plugin: %s v%s", manifest.name, manifest.version)
            except Exception as e:
                logger.warning("Failed to load plugin from %s: %s", plugin_dir, e)
        return self._plugins

    def _parse_manifest(self, manifest_path: Path) -> PluginManifest | None:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return PluginManifest(
                name=data.get("name", manifest_path.parent.name),
                version=data.get("version", "1.0.0"),
                description=data.get("description", ""),
                author=data.get("author", ""),
                tools_dir=data.get("tools_dir", ""),
                commands=data.get("commands", []),
                hooks=data.get("hooks", []),
                agents=data.get("agents", []),
                skills=data.get("skills", []),
                enabled=data.get("enabled", True),
            )
        except Exception as e:
            logger.warning("Failed to parse manifest %s: %s", manifest_path, e)
            return None

    def get(self, name: str) -> PluginManifest | None:
        return self._plugins.get(name)

    def list_plugins(self) -> list[PluginManifest]:
        return list(self._plugins.values())

    def reload(self) -> dict[str, PluginManifest]:
        self._plugins.clear()
        return self.load()


_loader: PluginLoader | None = None


def get_plugin_loader() -> PluginLoader:
    global _loader
    if _loader is None:
        _loader = PluginLoader()
    return _loader


__all__ = ["PluginManifest", "PluginLoader", "get_plugin_loader"]
