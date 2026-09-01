"""Snapshot-based versioning for generated dashboard apps.

Replaces the git-per-app plan (T6): GitPython is not installed in the venv and
the backend container has no ``git`` binary, so git commits would work in unit
tests but silently fail in production. Snapshots are plain filesystem copies
under ``<app_dir>/.versions/`` — the same undo UX as ``git revert HEAD`` with
zero external dependencies.

Semantics:
- ``snapshot_app_dir`` copies the current app dir into ``.versions/<seq>-<ts>-<msg>``.
- ``restore_snapshot`` overwrites the app dir from a snapshot.
- ``consume_latest`` restores AND deletes the most recent snapshot, so calling
  undo repeatedly steps back through edit history one change at a time.

The ``.versions`` directory, ``__pycache__`` and ``*.pyc`` are never copied.
"""

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

VERSIONS_DIR = ".versions"
_SNAPSHOT_DIRNAME_RE = re.compile(r"^\d{4}-")


def snapshot_app_dir(app_dir: Path, message: str) -> Optional[Path]:
    """Copy ``app_dir`` into ``app_dir/.versions/`` and return the snapshot dir.

    Returns None when the app dir does not exist yet. Sequences snapshots so
    lexicographic sort == chronological order.
    """
    if not app_dir.exists():
        logger.warning("snapshot skipped: app dir missing (%s)", app_dir)
        return None
    versions = app_dir / VERSIONS_DIR
    versions.mkdir(parents=True, exist_ok=True)
    seq = len([p for p in versions.iterdir() if p.is_dir()])
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", (message or "change").strip()) or "change"
    snap = versions / f"{seq:04d}-{ts}-{safe[:40]}"
    shutil.copytree(
        app_dir,
        snap,
        ignore=shutil.ignore_patterns(VERSIONS_DIR, "__pycache__", "*.pyc"),
    )
    logger.info("snapshot saved: %s (%s)", snap, message)
    return snap


def list_snapshots(app_dir: Path) -> List[Path]:
    """Snapshots for an app dir, oldest first."""
    versions = app_dir / VERSIONS_DIR
    if not versions.exists():
        return []
    return sorted(p for p in versions.iterdir() if p.is_dir())


def latest_snapshot(app_dir: Path) -> Optional[Path]:
    snaps = list_snapshots(app_dir)
    return snaps[-1] if snaps else None


def restore_snapshot(app_dir: Path, snap: Path) -> None:
    """Overwrite app dir contents with the snapshot (keeping ``.versions``)."""
    for child in app_dir.iterdir():
        if child.name == VERSIONS_DIR:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.copytree(
        snap,
        app_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(VERSIONS_DIR, "__pycache__", "*.pyc"),
    )
    logger.info("snapshot restored: %s", snap)


def consume_latest(app_dir: Path) -> Optional[Path]:
    """Restore the most recent snapshot, then delete it. Returns the snapshot.

    Returns None when there is no snapshot to restore (nothing to undo).
    """
    snap = latest_snapshot(app_dir)
    if snap is None:
        return None
    restore_snapshot(app_dir, snap)
    shutil.rmtree(snap)
    logger.info("snapshot consumed: %s", snap)
    return snap
