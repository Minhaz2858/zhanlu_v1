"""DashboardAppGenerator — fills Jinja2 templates with a DashboardSpec.

The generator is the ONLY writer of dashboard-app code. The agent never
writes raw files — it submits a spec (JSON dict), and the generator:

1. Renders ``api.py`` / ``queries.py`` / ``realtime.py`` from the Jinja2
   templates in ``app/dashboards/_template/``.
2. Writes ``config.json`` (metrics, design tokens, theme, refresh interval)
   used by the pre-built React frontend.
3. Copies the pre-built React ``dist/`` into the app directory.

Output lands under ``app/dashboards/{slug}/`` and is imported by
``DashboardAppManager`` as ``app.dashboards.{slug}.api``.
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

logger = logging.getLogger(__name__)

# backend/app
BASE_DIR = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = BASE_DIR / "dashboards" / "_template"
APPS_DIR = BASE_DIR / "dashboards"

# Slugs may contain '-' (e.g. "sales-overview"); the on-disk package dir + the
# importlib module path cannot. module_name() keeps them Python-safe.
_SLUG_MODULE_RE = re.compile(r"[^a-zA-Z0-9_]")


def module_name(slug: str) -> str:
    """Python-safe package/module name for a dashboard slug.

    "e2e-erp-sales-smoke" -> "e2e_erp_sales_smoke"; a leading digit is prefixed
    ("dash_") so the generated package remains importable. The URL slug is
    unaffected — it is only the filesystem dir + importlib path that changes.
    """
    name = _SLUG_MODULE_RE.sub("_", slug or "")
    if not name or name[0].isdigit():
        name = f"dash_{name}"
    return name

# Widget types the pre-built React frontend knows how to render.
KNOWN_TYPES = {"kpi", "line", "bar", "pie", "table", "area", "gauge", "radar"}


class DashboardAppGenerator:
    """Generate a deployable dashboard app module from a spec dict."""

    def __init__(
        self,
        template_dir: Optional[Path] = None,
        apps_dir: Optional[Path] = None,
    ) -> None:
        self.template_dir = template_dir or TEMPLATE_DIR
        self.apps_dir = apps_dir or APPS_DIR
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            undefined=StrictUndefined,
            autoescape=False,
            keep_trailing_newline=True,
        )
        # Python-source filter: tojson emits JSON (`null`/`true`/`false`) which is
        # NOT valid Python. topython emits real Python literals for *.py templates.
        self.env.filters["topython"] = repr

    # ── public API ──
    def generate(self, spec: Dict[str, Any]) -> Path:
        """Fill templates and write the app under ``apps_dir/{slug}/``. Returns the app dir."""
        slug = spec["slug"]
        config = self._frontend_config(spec)
        # The generated backend modules (api.py/queries.py/realtime.py) need the
        # FULL metric specs including the read-only SQL. The public frontend
        # config (config.json) must never contain raw SQL — that's `config`.
        full_metrics = spec.get("metrics", [])
        ctx: Dict[str, Any] = {
            "name": spec.get("name", slug),
            "slug": slug,
            "description": spec.get("description"),
            "datasource_id": spec["datasource_id"],
            "design_system_ref": spec.get("design_system_ref"),
            "metrics": full_metrics,
            "refresh_interval_seconds": int(spec.get("refresh_interval_seconds", 30)),
            "theme": spec.get("theme", "light"),
            "config": config,
        }
        app_dir = self.app_dir(slug)
        app_dir.mkdir(parents=True, exist_ok=True)
        for name in ("api.py", "queries.py", "realtime.py"):
            tmpl = self.env.get_template(f"{name}.jinja2")
            (app_dir / name).write_text(tmpl.render(**ctx), encoding="utf-8")
        (app_dir / "__init__.py").write_text(
            f'"""Generated dashboard app: {slug}. DO NOT EDIT."""\n', encoding="utf-8"
        )
        (app_dir / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._scaffold_gitignore(app_dir)
        self._copy_frontend(app_dir)
        logger.info("dashboard app generated at %s", app_dir)
        return app_dir

    def app_dir(self, slug: str) -> Path:
        """On-disk dir for a slug — uses the Python-safe module name.

        The URL slug (e.g. "sales-overview") stays user-facing; the directory
        name must be importable as a Python package.
        """
        return self.apps_dir / module_name(slug)

    # ── internals ──
    def _frontend_config(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Public config injected into config.json + api.py (never contains raw SQL)."""
        metrics = []
        for m in spec.get("metrics", []):
            metrics.append(
                {
                    "id": m["id"],
                    "title": m["title"],
                    "type": m["type"],
                    "options": m.get("options", {}),
                }
            )
        # Declared dimension filters across ALL metrics, deduped by key. Each
        # entry carries the display label (from the first metric that declared
        # it) so the frontend filter bar can render dropdowns. These map to
        # the backend's :dim_<key> tokens — only declared keys are accepted.
        dim_filters: Dict[str, dict] = {}
        for m in spec.get("metrics", []):
            for f in (m.get("options") or {}).get("filters") or []:
                key = f.get("key")
                if not key:
                    continue
                dim_filters.setdefault(
                    key,
                    {
                        "key": key,
                        "label": f.get("label") or key,
                        "column": f.get("column"),
                    },
                )
        # Top-level AI insight strip: { title, body } pairs rendered above the
        # widget grid. Computed server-side by the agent, never fabricated.
        insights = spec.get("insights") or []
        # Optional sectioned layout: [{title, widgets: [metric_id, ...]}] so the
        # dashboard reads as a story (KPI strip → trends → detail) instead of
        # one flat grid. Metrics not listed fall into a trailing "Other" group.
        # Sections may also carry page + panels (see dashboard_tools.py docs).
        layout = spec.get("layout") or []
        # Decision-center extras: multi-page tabs, typed AI-analysis panels,
        # executive header (greeting + market snapshot) and provenance footer.
        pages = spec.get("pages") or []
        panels = spec.get("panels") or []
        header = spec.get("header")
        footer = spec.get("footer")
        return {
            "name": spec.get("name", spec["slug"]),
            "slug": spec["slug"],
            "description": spec.get("description"),
            "theme": spec.get("theme", "light"),
            "style": spec.get("style", "standard"),
            "refresh_interval_seconds": int(spec.get("refresh_interval_seconds", 30)),
            "design_system_ref": spec.get("design_system_ref"),
            # Machine-readable design tokens consumed by the React frontend.
            # Empty dict when no design system is attached → frontend falls back
            # to its built-in slate defaults (backward compatible).
            "design": self._load_design_tokens(spec),
            "metrics": metrics,
            # Filter bar metadata (declared dims) + AI insight strip.
            "filters": list(dim_filters.values()),
            "insights": insights,
            "layout": layout,
            # Decision-center information architecture (2026-08-29).
            "pages": pages,
            "panels": panels,
            "header": header,
            "footer": footer,
        }

    # ── design-system token loading ──

    def _resolve_design_json(self, spec: Dict[str, Any]) -> Optional[Path]:
        """Resolve the design-system.json sidecar path from the spec.

        ``design_system_ref`` points at ``{generated_path}/{output_dir}/{org_id}/MASTER.md``.
        The machine-readable sidecar is a sibling ``design-system.json``.
        """
        ref = spec.get("design_system_ref") or spec.get("design_system_json_ref")
        if not ref:
            return None
        try:
            from app.config import settings

            ref_path = Path(str(ref))
            if ref_path.is_absolute():
                json_path = (
                    ref_path.with_name("design-system.json")
                    if ref_path.name == "MASTER.md"
                    else ref_path
                )
            else:
                base = Path(settings.generated_path)
                json_path = base / ref_path
                if ref_path.name == "MASTER.md":
                    json_path = base / ref_path.parent / "design-system.json"
            return json_path if json_path.is_file() else None
        except Exception as exc:
            logger.warning("resolve design-system.json failed: %s", exc)
            return None

    @staticmethod
    def _normalize_design(raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a ui-ux-pro-max design_system dict into frontend tokens.

        Produces a flat, CSS-friendly shape the React template consumes:
        colors (incl. a derived chart_palette + dark overrides), typography,
        spacing scale and a small style summary. Unknown/missing fields degrade
        gracefully — the frontend applies whatever it can.
        """
        colors = raw.get("colors") or {}
        style = raw.get("style") or {}
        typography = raw.get("typography") or {}
        spacing = raw.get("spacing_scale") or {}

        def _hex(k: str, default: str = "") -> str:
            v = str(colors.get(k) or "").strip()
            return v if v.startswith("#") else default

        def _luminance(hex_color: str) -> float:
            h = hex_color.lstrip("#")
            if len(h) != 6:
                return 255.0
            try:
                r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return 255.0
            return 0.299 * r + 0.587 * g + 0.114 * b

        background = _hex("background", "#f8fafc")
        foreground = _hex("foreground", "#0f172a")

        # Derive chart palette from the design's own colors first, then pad with
        # tasteful defaults so multi-series charts stay readable. Colors that
        # would be invisible on the design's background (near-white/near-black)
        # are skipped so every series stays distinguishable.
        def _contrasty(v: str) -> bool:
            return abs(_luminance(v) - _luminance(background)) > 60

        chart_palette: list[str] = []
        for k in ("primary", "accent", "secondary", "destructive", "ring", "on_primary"):
            v = _hex(k)
            if v and _contrasty(v) and v not in chart_palette:
                chart_palette.append(v)
            if len(chart_palette) >= 6:
                break
        _DEFAULT_PALETTE = ["#2563eb", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4"]
        for v in _DEFAULT_PALETTE:
            if len(chart_palette) >= 6:
                break
            if v not in chart_palette:
                chart_palette.append(v)
        chart_palette = chart_palette[:6]

        # Dark-mode overrides: the sidecar has a single color set. If the design
        # is natively dark we reuse it; otherwise provide conventional dark tokens.
        explicit_dark = colors.get("dark") or {}
        if _luminance(background) < 128:
            dark = {
                "background": background,
                "foreground": foreground,
                "muted": _hex("muted", "#1e293b"),
                "border": _hex("border", "#334155"),
            }
        else:
            dark = {
                "background": explicit_dark.get("background", "#020617"),
                "foreground": explicit_dark.get("foreground", "#f8fafc"),
                "muted": explicit_dark.get("muted", "#1e293b"),
                "border": explicit_dark.get("border", "#334155"),
            }

        return {
            "colors": {
                "primary": _hex("primary", "#2563eb"),
                "on_primary": _hex("on_primary", "#ffffff"),
                "secondary": _hex("secondary", "#64748b"),
                "accent": _hex("accent", "#f59e0b"),
                "background": background,
                "foreground": foreground,
                "muted": _hex("muted", "#f1f5f9"),
                "border": _hex("border", "#e2e8f0"),
                "destructive": _hex("destructive", "#ef4444"),
                "ring": _hex("ring", "#2563eb"),
                "chart_palette": chart_palette,
                "dark": dark,
            },
            "typography": {
                "heading": typography.get("heading") or "Inter",
                "body": typography.get("body") or "Inter",
                "google_fonts_url": typography.get("google_fonts_url") or "",
                "css_import": typography.get("css_import") or "",
            },
            "spacing": {
                "xs": spacing.get("xs") or "2px",
                "sm": spacing.get("sm") or "4px",
                "md": spacing.get("md") or "8px",
                "lg": spacing.get("lg") or "12px",
                "xl": spacing.get("xl") or "16px",
                "2xl": spacing.get("2xl") or "24px",
                "3xl": spacing.get("3xl") or "32px",
            },
            "style": {
                "name": style.get("name") or "Enterprise Clean",
                "keywords": style.get("keywords") or "",
                "card_radius": "8px",
            },
        }

    def _load_design_tokens(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Load + normalize design tokens, or return {} so the frontend falls back."""
        # Direct structured payload (builtin fallback path, no disk I/O).
        structured = spec.get("structured")
        if structured:
            return self._normalize_design(structured)
        json_path = self._resolve_design_json(spec)
        if json_path is None:
            return {}
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8"))
            return self._normalize_design(raw)
        except Exception as exc:
            logger.warning("load design-system.json failed (%s): %s", json_path, exc)
            return {}

    def _scaffold_gitignore(self, app_dir: Path) -> None:
        """Write a .gitignore so per-app versioning never tracks build artifacts.

        The per-app directory lives under app/dashboards/{slug}/ and, if the
        repo owner later chooses git-based versioning, this prevents dist/ and
        the .versions/ snapshot store from being committed.
        """
        (app_dir / ".gitignore").write_text(
            "# Generated dashboard app — build artifacts & version snapshots\n"
            "dist/\n"
            ".versions/\n"
            "__pycache__/\n"
            "*.pyc\n",
            encoding="utf-8",
        )

    def _copy_frontend(self, app_dir: Path) -> None:
        src = self.template_dir / "frontend" / "dist"
        dst = app_dir / "dist"
        if not src.exists():
            logger.warning(
                "template frontend dist missing (%s); dashboard UI will 404 until the "
                "viewer bundle is built once during setup", src,
            )
            dst.mkdir(parents=True, exist_ok=True)
            return
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        logger.info("frontend dist copied for %s (%d files)", app_dir.name, len(list(dst.rglob("*"))))


# Module-level singleton so tools, manager and tests share one generator.
_generator: Optional[DashboardAppGenerator] = None


def get_generator() -> DashboardAppGenerator:
    global _generator
    if _generator is None:
        _generator = DashboardAppGenerator()
    return _generator
