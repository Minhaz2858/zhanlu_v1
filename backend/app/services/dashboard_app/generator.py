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
        return {
            "name": spec.get("name", spec["slug"]),
            "slug": spec["slug"],
            "description": spec.get("description"),
            "theme": spec.get("theme", "light"),
            "refresh_interval_seconds": int(spec.get("refresh_interval_seconds", 30)),
            "design_system_ref": spec.get("design_system_ref"),
            "metrics": metrics,
        }

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
