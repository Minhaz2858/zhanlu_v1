"""DashboardAppManager — mounts/unmounts generated dashboard sub-routers.

Lifecycle:
    init_app(app)          → bind the FastAPI app (called in lifespan startup)
    create_app(record)     → persist DashboardApp row (status=building)
    mount(slug)            → import generated module + include_router
    start_poller(slug)     → launch the generated realtime.poll_loop task
    unmount(slug)          → stop poller + strip router routes
    regenerate(slug, spec) → re-generate files, reload module, remount (hot-reload)
    shutdown()             → stop all pollers (lifespan shutdown)

Status transitions on the DashboardApp row:
    building → running → stopped | error
"""

import asyncio
import importlib
import logging
import sys
from typing import Any, Dict, List, Optional

from fastapi import FastAPI

from app.database import SessionLocal
from app.models.dashboard_app import DashboardApp
from app.services.dashboard_app.generator import module_name

logger = logging.getLogger(__name__)

APP_PACKAGE = "app.dashboards"
MODULE_NAMES = ("api", "queries", "realtime")


class DashboardAppManager:
    """Tracks generated dashboard apps and their mounted routers + pollers."""

    def __init__(self) -> None:
        self._app: Optional[FastAPI] = None
        self._tasks: Dict[str, asyncio.Task] = {}
        self._mounted: Dict[str, str] = {}  # slug -> prefix

    # ── lifecycle hooks ──
    def init_app(self, app: FastAPI) -> None:
        self._app = app

    def load_persisted_apps(self) -> int:
        """Mount every persisted app with status=running and start its poller. Returns count."""
        count = 0
        try:
            db = SessionLocal()
            try:
                apps = db.query(DashboardApp).filter(DashboardApp.status == "running").all()
            finally:
                db.close()
        except Exception:
            logger.exception("load_persisted_apps: DB query failed")
            return 0
        for app in apps:
            try:
                self.mount(app.slug)
                self.start_poller(app.slug)
                count += 1
            except Exception:
                logger.exception("load_persisted_apps: failed for %s", app.slug)
        logger.info("loaded %d persisted dashboard app(s)", count)
        return count

    def shutdown(self) -> None:
        for slug in list(self._tasks):
            self.stop_poller(slug)
        self._tasks.clear()
        self._mounted.clear()

    # ── mounting / unmounting ──
    def mount(self, slug: str) -> None:
        """Import the generated module, register its router, and serve its static dist (idempotent)."""
        if self._app is None:
            raise RuntimeError("DashboardAppManager.init_app(app) must be called before mount()")
        if slug in self._mounted:
            return
        module = importlib.import_module(f"{APP_PACKAGE}.{module_name(slug)}.api")
        router = getattr(module, "router", None)
        if router is None:
            raise RuntimeError(f"generated module {slug}.api has no router")
        self._app.include_router(router)
        prefix = f"/api/dashboards/apps/{slug}"
        # Serve the pre-built React dist (relative base: './' so it works at any sub-path).
        from app.services.dashboard_app.generator import get_generator
        dist = get_generator().app_dir(slug) / "dist"
        if dist.exists():
            try:
                from fastapi.staticfiles import StaticFiles
                self._app.mount(
                    prefix,
                    StaticFiles(directory=str(dist), html=True),
                    name=f"dash_{slug}",
                )
            except Exception:
                logger.exception("failed to serve static dist for %s", slug)
        self._mounted[slug] = prefix
        logger.info("dashboard app mounted: %s", slug)

    def unmount(self, slug: str) -> None:
        """Stop the poller and strip every route registered under the app prefix."""
        self.stop_poller(slug)
        if self._app is None:
            return
        prefix = self._mounted.pop(slug, f"/api/dashboards/apps/{slug}")
        self._app.router.routes = [
            r for r in self._app.router.routes
            if not getattr(r, "path", "").startswith(prefix)
        ]
        logger.info("dashboard app unmounted: %s", slug)

    def regenerate(self, slug: str, spec: Dict[str, Any]) -> None:
        """Hot-reload: regenerate files, drop cached modules, remount, restart poller."""
        from app.services.dashboard_app.generator import get_generator

        get_generator().generate(spec)
        self._reload_module(slug)
        self.unmount(slug)
        self.mount(slug)
        self.start_poller(slug)
        logger.info("dashboard app regenerated: %s", slug)

    # ── pollers ──
    def start_poller(self, slug: str) -> None:
        if slug in self._tasks:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("start_poller(%s): no running event loop; skipping", slug)
            return
        poller = importlib.import_module(f"{APP_PACKAGE}.{module_name(slug)}.realtime")
        task = loop.create_task(poller.poll_loop())
        self._tasks[slug] = task
        logger.info("dashboard poller started: %s", slug)

    def stop_poller(self, slug: str) -> None:
        task = self._tasks.pop(slug, None)
        if task is not None:
            task.cancel()
            logger.info("dashboard poller stopped: %s", slug)

    # ── persistence helpers ──
    def create_app_record(self, spec: Dict[str, Any], org_id: str, created_by_id: str,
                          project_id: Optional[str] = None,
                          chat_thread_id: Optional[str] = None,
                          scope: str = "personal") -> DashboardApp:
        """Persist a new DashboardApp row (status=building). Returns the record."""
        db = SessionLocal()
        try:
            record = DashboardApp(
                name=spec["name"],
                slug=spec["slug"],
                description=spec.get("description"),
                datasource_kb_id=spec["datasource_id"],
                design_system_ref=spec.get("design_system_ref"),
                spec=spec,
                status="building",
                refresh_interval_seconds=int(spec.get("refresh_interval_seconds", 30)),
                org_id=org_id,
                project_id=project_id,
                created_by_id=created_by_id,
                chat_thread_id=chat_thread_id,
                scope=scope,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            return record
        finally:
            db.close()

    def touch_chat_thread(self, slug: str, chat_thread_id: str) -> bool:
        """Bind (or re-bind) the chat conversation that created this dashboard.

        Used by create/update tools when they have a conversation context. Also
        the reverse direction of the T5 link — a dashboard row points at its
        thread, and the thread's ``dashboard_id``/``chat_thread_id`` flows back
        through the router. Returns True when a record was updated.
        """
        db = SessionLocal()
        try:
            record = db.query(DashboardApp).filter(DashboardApp.slug == slug).first()
            if record is None:
                return False
            record.chat_thread_id = chat_thread_id
            db.commit()
            return True
        finally:
            db.close()

    def set_status(self, slug: str, status: str, app_url: Optional[str] = None) -> Optional[DashboardApp]:
        db = SessionLocal()
        try:
            record = db.query(DashboardApp).filter(DashboardApp.slug == slug).first()
            if record is None:
                return None
            record.status = status
            if app_url is not None:
                record.app_url = app_url
            db.commit()
            db.refresh(record)
            return record
        finally:
            db.close()

    def get_app(self, slug: str) -> Optional[DashboardApp]:
        db = SessionLocal()
        try:
            return db.query(DashboardApp).filter(DashboardApp.slug == slug).first()
        finally:
            db.close()

    def list_apps(self, org_id: Optional[str] = None) -> List[DashboardApp]:
        db = SessionLocal()
        try:
            q = db.query(DashboardApp)
            if org_id:
                q = q.filter(DashboardApp.org_id == org_id)
            return q.order_by(DashboardApp.created_date.desc()).all()
        finally:
            db.close()

    # ── internals ──
    def _reload_module(self, slug: str) -> None:
        for name in MODULE_NAMES:
            sys.modules.pop(f"{APP_PACKAGE}.{module_name(slug)}.{name}", None)


# Module-level singleton shared by tools, lifespan hook and tests.
dashboard_app_manager = DashboardAppManager()
