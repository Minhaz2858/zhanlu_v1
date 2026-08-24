"""ui-ux-pro-max tool — design intelligence wrapper.

Wraps the upstream BM25 + regex search CLI shipped at
``backend/skills/ui-ux-pro-max/scripts/search.py`` so the
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
under ``scripts/data/`` and prints results. No network, no DB.

When ``persist=True``, the tool writes ``MASTER.md`` (agent-readable) AND a
``design-system.json`` sidecar (machine-readable tokens) so downstream
generators (``create_fullstack_dashboard``) can inject the design system into
the React frontend without parsing markdown.
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

# The CLI is co-located with the skill's scripts/ directory. Older layouts
# nested it one level deeper (src/ui-ux-pro-max/scripts/search.py); we probe
# both so the tool keeps working across skill versions.
def _resolve_search_py() -> Path:
    for candidate in (
        _SKILL_DIR / "scripts" / "search.py",
        _SKILL_DIR / "src" / "ui-ux-pro-max" / "scripts" / "search.py",
    ):
        if candidate.is_file():
            return candidate
    return _SKILL_DIR / "scripts" / "search.py"


_SEARCH_PY = _resolve_search_py()

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


def _builtin_fallback(query: str, *, domain: str | None = None, design_system: bool = False) -> dict:
    """Deterministic local fallback when the optional ui-ux-pro-max CLI is absent.

    Keeps dashboard generation moving in production images where the skill's
    Python search package is not installed. The guidance is intentionally
    conservative and aligned with the dashboard viewer's token system.

    When ``design_system=True`` the result also carries a ``structured`` dict
    with the same shape as the CLI's ``design_system.json`` sidecar, so
    ``create_fullstack_dashboard`` can inject tokens even in fallback mode.
    """
    if design_system:
        structured = {
            "project_name": query,
            "category": "dashboard",
            "style": {
                "name": "Enterprise Clean",
                "type": "General",
                "keywords": "clean, modern, structured, enterprise",
                "best_for": "Operational BI dashboards",
                "card_radius": "8px",
            },
            "colors": {
                "primary": "#2563eb",
                "on_primary": "#ffffff",
                "secondary": "#64748b",
                "accent": "#f59e0b",
                "background": "#f8fafc",
                "foreground": "#0f172a",
                "muted": "#f1f5f9",
                "border": "#e2e8f0",
                "destructive": "#ef4444",
                "ring": "#2563eb",
                "chart_palette": [
                    "#2563eb", "#10b981", "#f59e0b",
                    "#ef4444", "#8b5cf6", "#06b6d4",
                ],
                "dark": {
                    "background": "#020617",
                    "foreground": "#f8fafc",
                    "muted": "#1e293b",
                    "border": "#334155",
                },
            },
            "typography": {
                "heading": "Inter",
                "body": "Inter",
                "mood": "modern, professional",
                "google_fonts_url": "",
                "css_import": "",
            },
            "spacing_scale": {
                "xs": "2px", "sm": "4px", "md": "8px",
                "lg": "12px", "xl": "16px", "2xl": "24px", "3xl": "32px",
            },
            "anti_patterns": (
                "Avoid decorative hero layouts; keep charts near their KPI "
                "context; do not exceed 6 chart colors."
            ),
        }
        result = f"""# Built-in Dashboard Design System

Query: {query}

- Layout: KPI row first, trend charts second, breakdown charts third, detail table last.
- Density: compact enterprise BI spacing with restrained borders and 8px card radius.
- Palette: use zhanlu tokens `hsl(var(--primary))`, `hsl(var(--chart-2))`, `hsl(var(--chart-3))`, `bg-card`, and `text-muted-foreground`.
- Typography: clear numeric hierarchy; KPI values use the largest type, chart labels stay small and readable.
- Charts: line or area for time trends, bar for product/customer comparison, table for detail and auditability.
- States: show skeleton loading, per-widget query errors, empty states, and live refresh time.
- Interaction: prefer dashboard filters/drilldowns over static explanatory text.
""".strip()
    else:
        structured = None
        topic = domain or "dashboard"
        result = f"""# Built-in UI UX Guidance

Query: {query}
Domain: {topic}

- Use KPI cards for headline revenue, volume, and count metrics.
- Use line/area charts for weekly trends and bar charts for ranked breakdowns.
- Keep chart color count low; use primary plus chart tokens for contrast.
- Put customer/product breakdowns below trend context, with a table for exact values.
- Avoid decorative hero layouts; this is an operational BI dashboard.
""".strip()
    payload = {
        "success": True,
        "result": result,
        "fallback_used": True,
        "query": query,
        "domain": domain or ("design-system" if design_system else "auto"),
    }
    if structured is not None:
        payload["structured"] = structured
    return payload


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
        logger.warning("ui-ux-pro-max CLI missing; using built-in search fallback: %s", exc)
        result = _builtin_fallback(query, domain=domain, design_system=False)
        result["error"] = str(exc)
        result["stack"] = stack
        result["max_results"] = max_results
        return result

    result = _run_cli(cmd)
    if result["success"]:
        result["query"] = query
        result["domain"] = domain or "auto"
        result["stack"] = stack
        result["max_results"] = max_results
    return result


def _design_out_dir(args: dict) -> Path:
    """Absolute directory where persisted design-system files are written.

    Must be absolute: the CLI subprocess runs with ``cwd=_SKILL_DIR``, so a
    relative ``settings.generated_path`` would land under the skill dir instead
    of the real generated dir.
    """
    from app.config import settings

    org_id = str(args.get("org_id") or context_org_id(args))
    output_dir = str(args.get("output_dir") or "design-system").strip("/") or "design-system"
    return (Path(settings.generated_path) / output_dir / org_id).resolve()


def _persist_design_system(result: dict, args: dict, project: Optional[str]) -> dict:
    """When ``persist=True``, write the design-system markdown + JSON to disk.

    Location: ``{generated_path}/{output_dir|'design-system'}/{org_id}/MASTER.md``
    plus a sibling ``design-system.json`` (machine-readable tokens, consumed by
    ``create_fullstack_dashboard``). Adds ``design_system_ref`` (relative path —
    referenced by DashboardSpec), ``design_system_json_ref`` and
    ``persisted_to`` (absolute path) to the result. Failures are logged, never
    raised — a successful generation without persistence is still usable.
    """
    if not args.get("persist") or not result.get("success"):
        return result
    text = (result.get("result") or "").strip()
    try:
        import json as json_module

        from datetime import datetime, timezone

        out_dir = _design_out_dir(args)
        out_dir.mkdir(parents=True, exist_ok=True)

        if text:
            master = out_dir / "MASTER.md"
            header = (
                f"# Design System — {project or result.get('query') or 'dashboard'}\n\n"
                f"> Persisted by uiux_design_system (persist=True) on "
                f"{datetime.now(timezone.utc).isoformat()}\n\n"
            )
            master.write_text(header + text + "\n", encoding="utf-8")
            result["design_system_ref"] = (
                f"{args.get('output_dir') or 'design-system'}/{context_org_id(args)}/MASTER.md"
            )
            result["persisted_to"] = str(master)

        # Structured sidecar — from the builtin fallback (no CLI) or already
        # written by the CLI via --json-out.
        structured = result.get("structured")
        json_path = out_dir / "design-system.json"
        if structured:
            json_path.write_text(
                json_module.dumps(structured, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if json_path.is_file():
            result["design_system_json_ref"] = (
                f"{args.get('output_dir') or 'design-system'}/{context_org_id(args)}/design-system.json"
            )
    except Exception as exc:
        logger.warning("persist design system failed: %s", exc)
        result["persist_error"] = str(exc)
    return result


def context_org_id(args: dict) -> str:
    """Best-effort org id from the tool args (agent passes it through context)."""
    return str(args.get("org_id") or "default-org")


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
    if args.get("persist"):
        json_out = _design_out_dir(args) / "design-system.json"
        argv_extra += ["--json-out", str(json_out)]

    try:
        cmd = _build_cli_argv(argv_extra, design_system=True)
    except FileNotFoundError as exc:
        logger.warning("ui-ux-pro-max CLI missing; using built-in design-system fallback: %s", exc)
        result = _builtin_fallback(query, design_system=True)
        result["error"] = str(exc)
        result["query"] = query
        result["project"] = project
        result["dials"] = {
            k: v for k, v in {"variance": variance, "motion": motion, "density": density}.items()
            if v is not None
        }
        _persist_design_system(result, args, project)
        return result

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
    _persist_design_system(result, args, project)
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
                "persist": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When True, persist the design system markdown to "
                        "design-system/{org_id}/MASTER.md and return "
                        "design_system_ref for use in create_fullstack_dashboard."
                    ),
                },
                "output_dir": {
                    "type": "string",
                    "default": "design-system",
                    "description": "Subdirectory under the generated dir to persist into.",
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