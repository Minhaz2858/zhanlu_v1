"""Lazy dependency installer for opt-in zhanlu tool backends.

Many tools (mistral TTS, elevenlabs TTS, MCP servers, etc.) require
Python packages that not every deployment needs. The historical approach
was to bundle them under ``requirements-extras.txt`` and install them
eagerly at setup time. That has two problems:

1. **Fragility.** When one extra's transitive dependency becomes
   unavailable on PyPI (quarantined for malware, yanked, broken upload),
   the entire extras resolve fails and fresh installs silently fall back
   to a stripped tier.

2. **Bloat.** A user who only ever talks to one provider pulls hundreds
   of packages they will never import.

The lazy-install pattern fixes both. Backends call :func:`ensure` at the
top of their first-import path. If the deps are missing, ``ensure`` runs
a venv-scoped pip install. If the user has explicitly disabled lazy
installs, ``ensure`` raises :class:`FeatureUnavailable` with a clear
remediation hint.

Security model:

* **Venv-scoped only.** Installs target ``sys.executable`` in the active
  venv. We never touch the system Python.
* **PyPI by package name only.** Specs may be ``"package>=1.0,<2"`` etc.
  We do NOT support ``--index-url`` overrides, ``git+https://``, file:
  paths, or any other input that could be hijacked by a malicious config.
* **Allowlist.** Only specs that appear in :data:`LAZY_DEPS` can be
  installed via this path. A typo in feature name doesn't get the user
  install-anything semantics.
* **Opt-out.** Setting ``ZHANLU_ALLOW_LAZY_INSTALLS=0`` disables runtime
  installs. Users in restricted networks can pin themselves to whatever
  was installed at setup time.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from typing import Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowlist: feature name -> list of pip spec strings
# ---------------------------------------------------------------------------

LAZY_DEPS: Dict[str, List[str]] = {
    # NOTE: The previous Playwright-based `browser` tool was replaced by
    # `agent_browser` (npm CLI wrapper). The CLI's presence on PATH is
    # checked at runtime via `shutil.which("agent-browser")` inside
    # `tool_handlers/agent_browser_tool.py` — it is NOT a pip dependency,
    # so it intentionally has no entry here.
    # Media
    "tts_elevenlabs": ["elevenlabs>=1.0"],
    "tts_openai": [],   # uses openai (already a transitive dep)
    "video_generation": ["replicate>=0.25", "fal-client>=0.4"],
    "neutts": ["neutts[cpu]>=0.1; platform_system != 'Darwin'"],
    "transcription": ["openai-whisper>=20231117"],
    "vision": ["Pillow>=10.0"],
    # MCP
    "mcp": ["mcp>=1.0"],
    # Communication
    "discord": ["discord.py>=2.3"],
    "feishu": ["lark-oapi>=1.2"],
    "microsoft_graph": ["msal>=1.20", "requests>=2.31"],
    "homeassistant": ["homeassistant-api>=3.4"],
    "yuanbao": ["requests>=2.31"],
    # Auxiliary
    "xai": ["requests>=2.31"],
    "openrouter": ["requests>=2.31"],
    "x_search": ["tweepy>=4.14"],
    # Skills platform
    "skill_manager": ["PyYAML>=6.0"],
    # Mixture of agents
    "mixture_of_agents": [],
    # Voice mode (live audio orchestration)
    "voice_mode": ["sounddevice>=0.4", "numpy>=1.24"],
    # Database drivers — auto-installed on first connection attempt.
    # Each entry is a pip spec list that the connector_factory triggers
    # via ensure(feature). This is the "agent solves it itself" piece —
    # the user never sees "ModuleNotFoundError: No module named 'pymysql'".
    "db_mysql":    ["pymysql>=1.1", "cryptography>=41.0"],
    "db_postgres": ["psycopg2-binary>=2.9,<3.0"],
    "db_mssql":    ["pyodbc>=5.0"],
    "db_oracle":   ["cx-Oracle>=8.3"],
    "db_sqlite":   [],  # stdlib — no pip needed
}


class FeatureUnavailable(ImportError):
    """Raised when a feature's deps are missing and lazy-installs are off."""


# Per-feature install status, so repeated ensure() calls are idempotent
# within a process lifetime. Values: "available" | "installing" | "unavailable".
_STATUS: Dict[str, str] = {}


def get_status(feature: str) -> str:
    """Return the current auto-install status for *feature*.

    Returns one of ``"available"``, ``"installing"``, ``"unavailable"``,
    or ``"unknown"`` if the feature has never been requested.
    """
    return _STATUS.get(feature, "unknown")


def _mark(feature: str, status: str) -> None:
    """Record an install-phase transition (internal helper)."""
    _STATUS[feature] = status


def is_lazy_installs_enabled() -> bool:
    """Return True unless the user has explicitly disabled lazy installs."""
    import os
    flag = os.environ.get("ZHANLU_ALLOW_LAZY_INSTALLS", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def ensure(feature: str) -> None:
    """Best-effort import for a lazy feature.

    1. Try ``importlib.import_module(feature_module)`` (the common entry
       point for the feature). If it succeeds, return immediately.
    2. If the import fails and the feature is in :data:`LAZY_DEPS` and
       :func:`is_lazy_installs_enabled` is True, run a venv-scoped pip
       install of the allowlisted specs, then retry the import.
    3. Otherwise, raise :class:`FeatureUnavailable` with a remediation hint.

    Note: ``feature`` is the LAZY_DEPS key, not necessarily a module name.
    Each entry should also declare a ``_module`` attribute (see below) so we
    know which import to test. To keep the API simple, we derive the
    module name from a convention map.
    """
    if feature not in LAZY_DEPS:
        raise FeatureUnavailable(
            f"Unknown lazy feature: {feature!r}. "
            f"Add it to LAZY_DEPS first."
        )
    module_name = _FEATURE_TO_MODULE.get(feature)
    if not module_name:
        # Convention: feature name is the module name
        module_name = feature
    try:
        importlib.import_module(module_name)
        _mark(feature, "available")
        return
    except ImportError as primary_exc:
        if not is_lazy_installs_enabled():
            _mark(feature, "unavailable")
            raise FeatureUnavailable(
                f"Feature {feature!r} requires deps {LAZY_DEPS[feature]} "
                f"but lazy installs are disabled (set ZHANLU_ALLOW_LAZY_INSTALLS=1). "
                f"Underlying error: {primary_exc}"
            ) from primary_exc
        specs = LAZY_DEPS[feature]
        if not specs:
            _mark(feature, "unavailable")
            raise FeatureUnavailable(
                f"Feature {feature!r} is not importable ({primary_exc}) "
                f"and has no pip specs to install. Add them to LAZY_DEPS."
            ) from primary_exc
        _mark(feature, "installing")
        _pip_install(specs)
        try:
            importlib.import_module(module_name)
            _mark(feature, "available")
        except ImportError as retry_exc:
            _mark(feature, "unavailable")
            raise FeatureUnavailable(
                f"Failed to import {module_name!r} even after pip install "
                f"({specs}): {retry_exc}"
            ) from retry_exc


# Map feature key -> module name to import-test. Defaults to the key.
_FEATURE_TO_MODULE: Dict[str, str] = {
    "tts_elevenlabs": "elevenlabs",
    "neutts": "neutts",
    "transcription": "whisper",
    "vision": "PIL",
    "mcp": "mcp",
    "discord": "discord",
    "feishu": "lark_oapi",
    "microsoft_graph": "msal",
    "homeassistant": "homeassistant_api",
    "xai": "requests",          # covered by base
    "openrouter": "requests",   # covered by base
    "x_search": "tweepy",
    "skill_manager": "yaml",
    "voice_mode": "sounddevice",
    # Database drivers
    "db_mysql": "pymysql",
    "db_postgres": "psycopg2",
    "db_mssql": "pyodbc",
    "db_oracle": "cx_Oracle",
    "db_sqlite": "sqlite3",
}


def _pip_install(specs: List[str]) -> None:
    """Run a venv-scoped pip install of the given specs.

    Each spec must be a plain package spec (``"pkg>=1.0,<2"``). Anything
    else (URLs, file paths, --index-url overrides) is rejected.
    """
    import re
    for spec in specs:
        if not re.match(r"^[A-Za-z0-9_.\-\[\]<>=!;,\s]+$", spec):
            raise FeatureUnavailable(
                f"Refusing to install non-PyPI spec: {spec!r}"
            )
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", *specs]
    logger.info("Lazy install: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise FeatureUnavailable(
            f"Lazy install timed out for {specs}"
        ) from exc
    if proc.returncode != 0:
        logger.warning("Lazy install failed: %s", proc.stderr)
        raise FeatureUnavailable(
            f"Lazy install failed for {specs}: {proc.stderr.strip()}"
        )
