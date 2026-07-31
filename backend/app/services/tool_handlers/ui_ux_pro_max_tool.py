"""ui-ux-pro-max tool — design intelligence wrapper.

Wraps the upstream BM25 + regex search CLI shipped at
``backend/skills/ui-ux-pro-max/src/ui-ux-pro-max/scripts/search.py`` so the
Zhanlu global agent can call it like any other tool.

Two tools are exposed:

* ``uiux_search`` — single-domain search (style, color, chart, landing,
  product, ux, typography, google-fonts, icons, gsap, react, web).
* ``uiux_design_system`` — auto-aggregates a full design system spec
  (palette + typography + style + UX checklist) for a given query.

Both call the CLI via ``subprocess.run`` with a 30 s timeout, capture stdout
as the markdown response, and return a structured dict. Errors NEVER raise
— they return ``{"success": False, "error": ..., "fallback": ""}`` so the
SynexiaFSM planner can react gracefully (mirroring ``loop_guard_v2``).

The skill is not sandbox-required: ``search.py`` only reads local CSV files
under ``src/ui-ux-pro-max/data/`` and prints results. No network, no DB.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path resolution — the CLI is co-located with the skill package
# ---------------------------------------------------------------------------

_SKILL_DIR = Path(__file__).resolve().parents[3] / "skills" / "ui-ux-pro-max"
_SEARCH_PY = _SKILL_DIR / "src" / "ui-ux-pro-max" / "scripts" / "search.py"

# Defensive: if the package layout ever changes, fall back to a module
# import path that mirrors the upstream CLI.
_FALLBACK_CMD = [sys.executable, "-m", "ui_ux_pro_max"] if False else None

# Domains and stacks are intentionally unvalidated server-side — the
# upstream CLI does its own auto-detection and emits a clear error message
# when a value is unrecognized. Passing through lets the agent see the
# upstream's wording in the result.

_VALID_DOMAINS = {
    "style", "color", "chart", "landing", "product", "ux",
    "typography", "google-fonts", "icons", "gsap", "react", "web",
}

_VALID_STACKS = {
    "html-tailwind", "react", "nextjs", "vue", "nuxtjs", "nuxt-ui",
    "svelte", "astro", "swiftui", "react-native", "flutter", "shadcn",
    "jetpack-compose", "threejs", "angular", "laravel", "javafx", "wpf",
    "winui", "avalonia", "uno", "uwp",
}

_TIMEOUT_SECONDS = 30


def _build_cli_argv(args: list[str], design_system: bool = False) -> list[str]:
    """Build the upstream CLI argv list from a flat list of args."""
    if not _SEARCH_PY.is_file():
        raise FileNotFoundError(
            f"ui-ux-pro-max CLI not found at {_SEARCH_PY}. "
            "Reinstall the skill or update _SEARCH_PY."
        )
    cmd = [sys.executable, str(_SEARCH_PY)]
    if design_system:
        cmd.append("--design-system")
    cmd.extend(args)
    return cmd


def _run_cli(cmd: list[str]) -> dict:
    """Run the CLI with a hard timeout. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            cwd=str(_SKILL_DIR),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        logger.warning("ui-ux-pro-max: timeout after %ss", _TIMEOUT_SECONDS)
        return {
            "success": False,
            "error": f"timeout after {_TIMEOUT_SECONDS}s",
            "fallback": "",
        }
    except FileNotFoundError as exc:
        logger.error("ui-ux-pro-max: CLI missing — %s", exc)
        return {"success": False, "error": str(exc), "fallback": ""}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("ui-ux-pro-max: subprocess failed: %s", exc)
        return {"success": False, "error": str(exc), "fallback": ""}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        logger.warning(
            "ui-ux-pro-max: non-zero exit %s\nstderr=%s",
            proc.returncode, stderr[:500],
        )
        return {
            "success": False,
            "error": stderr or f"exit {proc.returncode}",
            "fallback": stdout,
            "returncode": proc.returncode,
        }
    return {
        "success": True,
        "result": stdout,
        "command": " ".join(shlex.quote(p) for p in cmd),
    }


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _uiux_search(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Single-domain search across the 12 ui-ux-pro-max domains."""
    query = (args.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "query is required"}

    domain = (args.get("domain") or "").strip().lower() or None
    stack = (args.get("stack") or "").strip().lower() or None
    raw_max = args.get("max_results")
    if raw_max is None or raw_max == "":
        max_results = 3
    else:
        try:
            max_results = int(raw_max)
        except (TypeError, ValueError):
            max_results = 3
    max_results = max(1, min(max_results, 5))

    if domain and domain not in _VALID_DOMAINS:
        return {
            "success": False,
            "error": (
                f"invalid domain {domain!r}. Valid: "
                + ", ".join(sorted(_VALID_DOMAINS))
            ),
        }
    if stack and stack not in _VALID_STACKS:
        return {
            "success": False,
            "error": (
                f"invalid stack {stack!r}. Valid: "
                + ", ".join(sorted(_VALID_STACKS))
            ),
        }

    argv_extra: list[str] = [query]
    if domain:
        argv_extra += ["--domain", domain]
    if stack:
        argv_extra += ["--stack", stack]
    argv_extra += ["--max-results", str(max_results)]

    try:
        cmd = _build_cli_argv(argv_extra, design_system=False)
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc), "fallback": ""}

    result = _run_cli(cmd)
    if result["success"]:
        result["query"] = query
        result["domain"] = domain or "auto"
        result["stack"] = stack
        result["max_results"] = max_results
    return result


async def _uiux_design_system(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Generate a full design system spec (palette + typography + UX checklist)."""
    query = (args.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "query is required"}

    project = (args.get("project") or "").strip() or None

    def _int_dial(name: str) -> Optional[int]:
        v = args.get(name)
        if v is None or v == "":
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return max(1, min(n, 10))

    variance = _int_dial("variance")
    motion = _int_dial("motion")
    density = _int_dial("density")

    argv_extra: list[str] = [query]
    if project:
        argv_extra += ["-p", project]
    if variance is not None:
        argv_extra += ["--variance", str(variance)]
    if motion is not None:
        argv_extra += ["--motion", str(motion)]
    if density is not None:
        argv_extra += ["--density", str(density)]

    try:
        cmd = _build_cli_argv(argv_extra, design_system=True)
    except FileNotFoundError as exc:
        return {"success": False, "error": str(exc), "fallback": ""}

    result = _run_cli(cmd)
    if result["success"]:
        result["query"] = query
        result["project"] = project
        result["dials"] = {
            k: v for k, v in (
                ("variance", variance),
                ("motion", motion),
                ("density", density),
            ) if v is not None
        }
    return result


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------


UIUX_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "uiux_search",
        "description": (
            "Search the ui-ux-pro-max design intelligence database. 12 domains: "
            "style, color, chart, landing, product, ux, typography, google-fonts, "
            "icons, gsap, react, web. 22 stacks: html-tailwind (default), react, "
            "nextjs, vue, shadcn, etc. Returns markdown with 1-3 vetted results "
            "(color palettes, font pairings, chart types, UX rules, etc). ALWAYS "
            "call this BEFORE generating dashboards or HTML artifacts so the "
            "output uses vetted design tokens rather than ad-hoc CSS."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text description of what you're building, e.g. "
                        "'monthly sales dashboard' or 'enterprise SaaS landing page'."
                    ),
                },
                "domain": {
                    "type": "string",
                    "enum": sorted(_VALID_DOMAINS),
                    "description": (
                        "Search domain. Omit to let the engine auto-detect. "
                        "Use 'chart' for chart-type recommendations, 'color' "
                        "for palette, 'typography' for font pairings, 'ux' for "
                        "pre-delivery checklists."
                    ),
                },
                "stack": {
                    "type": "string",
                    "enum": sorted(_VALID_STACKS),
                    "description": (
                        "Target stack. Default 'html-tailwind'. Use 'react', "
                        "'shadcn', etc. when building for that framework."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 3,
                    "description": "Max results to return (default 3, cap 5).",
                },
            },
            "required": ["query"],
        },
    },
}


UIUX_DESIGN_SYSTEM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "uiux_design_system",
        "description": (
            "Generate a complete design system spec (palette + typography + "
            "style + UX checklist + landing patterns) for a topic. Aggregates "
            "5 domains in one call and applies 161 reasoning rules. Use this "
            "FIRST when building any visual artifact; follow up with "
            "uiux_search(domain='chart') for chart-type recommendations. "
            "Optional design dials: variance (1-10, centered→bold), motion "
            "(1-10, subtle→complex), density (1-10, spacious→dense/dashboard)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Description of the artifact / topic.",
                },
                "project": {
                    "type": "string",
                    "description": "Project name (used in the output spec).",
                },
                "variance": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "1 = centered/minimal, 10 = bold/asymmetric.",
                },
                "motion": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "1 = subtle, 10 = complex GSAP.",
                },
                "density": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "1 = spacious, 10 = dense/dashboard.",
                },
            },
            "required": ["query"],
        },
    },
}


def _uiux_check() -> bool:
    """Tool availability check — false when the CLI script is missing."""
    return _SEARCH_PY.is_file()


registry.register(
    name="uiux_search",
    schema=UIUX_SEARCH_SCHEMA,
    handler=_uiux_search,
    category="design",
    toolset="design",
    description=(
        "Search the ui-ux-pro-max design intelligence database (12 domains, "
        "22 stacks). Call BEFORE generating dashboards / HTML / slide decks."
    ),
    emoji="🎨",
    is_async=True,
    check_fn=_uiux_check,
    max_result_size_chars=20_000,
)

registry.register(
    name="uiux_design_system",
    schema=UIUX_DESIGN_SYSTEM_SCHEMA,
    handler=_uiux_design_system,
    category="design",
    toolset="design",
    description=(
        "Generate a complete design system spec (palette + typography + "
        "style + UX checklist) for a topic in one call."
    ),
    emoji="🎨",
    is_async=True,
    check_fn=_uiux_check,
    max_result_size_chars=30_000,
)


__all__ = [
    "UIUX_SEARCH_SCHEMA",
    "UIUX_DESIGN_SYSTEM_SCHEMA",
]