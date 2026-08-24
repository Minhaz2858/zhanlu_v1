"""DashboardAppManager — tracks generated dashboard apps (state + pollers).

T17: the per-app `app.mount(StaticFiles)` + router-include was removed. Serving
is now done by GLOBAL catch-all routes in `backend/app/routers/dashboards.py`
that resolve the on-disk app dir / generated module per-request. This manager
only owns DB state, the poller lifecycle, and on-disk path resolution.

Lifecycle:
    init_app(app)          → bind the FastAPI app (called in lifespan startup)
    create_app(record)     → persist DashboardApp row (status=building)
    mount(slug)            → warm the generated module + start its poller
    start_poller(slug)     → launch the generated realtime.poll_loop task
    unmount(slug)          → stop the poller
    regenerate(slug, spec) → re-generate files, reload module, remount (hot-reload)
    shutdown()             → stop all pollers (lifespan shutdown)

Status transitions on the DashboardApp row:
    building → running → stopped | error
"""

import asyncio
import importlib
import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI

from app.database import SessionLocal
from app.models.dashboard_app import DashboardApp
from app.services.dashboard_app.generator import get_generator, module_name
from app.services.dashboard_app.versioning import consume_latest, snapshot_app_dir

logger = logging.getLogger(__name__)

APP_PACKAGE = "app.dashboards"
MODULE_NAMES = ("api", "queries", "realtime")


class DashboardAppManager:
    """Tracks generated dashboard apps: DB state + pollers + on-disk path resolution."""

    def __init__(self) -> None:
        self._app: Optional[FastAPI] = None
        self._tasks: Dict[str, asyncio.Task] = {}

    # ── lifecycle hooks ──
    def init_app(self, app: FastAPI) -> None:
        self._app = app

    def load_persisted_apps(self) -> int:
        """Start every persisted app with status=running and start its poller. Returns count."""
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
                count += 1
            except Exception:
                logger.exception("load_persisted_apps: failed for %s", app.slug)
        logger.info("loaded %d persisted dashboard app(s)", count)
        return count

    def shutdown(self) -> None:
        for slug in list(self._tasks):
            self.stop_poller(slug)
        self._tasks.clear()

    # ── mounting / unmounting ──
    def mount(self, slug: str) -> None:
        """Warm the generated module (so the global routes can import it later) and start its poller."""
        try:
            importlib.import_module(f"{APP_PACKAGE}.{module_name(slug)}.api")
        except Exception:
            logger.warning("mount(%s): generated module not importable yet", slug)
        self.start_poller(slug)
        logger.info("dashboard app mounted: %s", slug)

    def unmount(self, slug: str) -> None:
        """Stop the poller for a slug. Static/REST/WS are served by global routes."""
        self.stop_poller(slug)
        logger.info("dashboard app unmounted: %s", slug)

    def resolve_app_dir(self, slug: str) -> Optional[Path]:
        """Resolve the on-disk directory for a dashboard app (the single source of truth).

        Cached per-process keyed on the ``dist`` directory mtime so hot apps pay
        no syscall on every request; a regenerate bumps the mtime and invalidates
        the cache. Works for apps created after process start (cache miss → recompute).
        """
        app_dir = get_generator().app_dir(slug)
        dist = app_dir / "dist"
        mtime = dist.stat().st_mtime if dist.exists() else 0.0
        return self._resolve_app_dir_cached(slug, round(mtime, 3))

    @lru_cache(maxsize=1024)
    def _resolve_app_dir_cached(self, slug: str, mtime: float) -> Optional[Path]:
        app_dir = get_generator().app_dir(slug)
        return app_dir if app_dir.exists() else None

    def regenerate(self, slug: str, spec: Dict[str, Any]) -> None:
        """Hot-reload: regenerate files, drop cached modules, remount, restart poller."""
        from app.services.dashboard_app.generator import get_generator

        # T6: capture the pre-change state so the user can say "undo" to revert.
        self.commit_version(slug, "pre-change")
        get_generator().generate(spec)
        self._reload_module(slug)
        self.unmount(slug)
        self.mount(slug)
        self.start_poller(slug)
        logger.info("dashboard app regenerated: %s", slug)

    # ── versioning (T6: snapshot-per-change + undo) ──
    def commit_version(self, slug: str, message: str) -> Optional[Path]:
        """Snapshot the current on-disk app (a restore point).

        Called after every accepted create/update so the user can say "undo".
        Returns the snapshot dir, or None when the app dir does not exist yet.
        """
        from app.services.dashboard_app.generator import get_generator

        app_dir = get_generator().app_dir(slug)
        return snapshot_app_dir(app_dir, message)

    def revert(self, slug: str) -> Dict[str, Any]:
        """Restore the most recent snapshot and hot-reload the app.

        "Undo" semantics: each accepted change created a snapshot, and the
        most recent one is restored + consumed. Returns a JSON-safe result.
        """
        from app.services.dashboard_app.generator import get_generator

        app_dir = get_generator().app_dir(slug)
        restored = consume_latest(app_dir)
        if restored is None:
            return {"success": False, "slug": slug,
                    "message": "Nothing to undo — no previous version found for this dashboard."}
        try:
            self._reload_module(slug)
            self.unmount(slug)
            self.mount(slug)
            self.start_poller(slug)
        except Exception as exc:
            logger.exception("revert(%s): remount failed", slug)
            return {"success": False, "slug": slug,
                    "message": f"Snapshot restored, but remount failed: {exc}"}
        self.set_status(slug, "running")
        return {"success": True, "slug": slug, "restored_snapshot": restored.name,
                "message": f"Reverted to the previous version ({restored.name})."}

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

    def list_apps(self, org_id: Optional[str] = None,
                  user_id: Optional[str] = None) -> List[DashboardApp]:
        """List dashboard apps the user may see, ordered newest-first.

        T10 scope rule: ``company`` apps are visible to the whole org;
        ``personal`` apps are visible only to their creator. When ``user_id``
        is omitted the caller opts out of scope filtering (returns all org
        apps) — used by internal tooling, never by the user-facing API.
        """
        db = SessionLocal()
        try:
            q = db.query(DashboardApp)
            if org_id:
                q = q.filter(DashboardApp.org_id == org_id)
            if user_id:
                from sqlalchemy import or_
                q = q.filter(or_(
                    DashboardApp.scope == "company",
                    (DashboardApp.scope == "personal")
                    & (DashboardApp.created_by_id == user_id),
                ))
            return q.order_by(DashboardApp.created_date.desc()).all()
        finally:
            db.close()

    # ── internals ──
    def _reload_module(self, slug: str) -> None:
        for name in MODULE_NAMES:
            sys.modules.pop(f"{APP_PACKAGE}.{module_name(slug)}.{name}", None)


# Module-level singleton shared by tools, lifespan hook and tests.
dashboard_app_manager = DashboardAppManager()
