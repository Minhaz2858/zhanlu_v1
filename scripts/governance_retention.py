#!/usr/bin/env python3
"""Zhanlu Data Governance — Retention Enforcement. Daily cron job."""

import os
import sys
import json
import shutil
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://zhanlu_app:zhanlu_app_secret_replace_in_production@pgbouncer:6432/zhanlu")
SOFT_DELETE_DAYS = int(os.environ.get("GOV_SOFT_DELETE_DAYS", "30"))
SNAPSHOT_DAYS = int(os.environ.get("GOV_SNAPSHOT_DAYS", "7"))
SANDBOX_HOURS = int(os.environ.get("GOV_SANDBOX_HOURS", "24"))
SANDBOX_TMP = os.environ.get("SANDBOX_TMP_ROOT", "/tmp/zhanlu_sandbox")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [governance] %(levelname)s %(message)s")
logger = logging.getLogger("governance_retention")


def get_conn():
    url = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://")
    return psycopg2.connect(url)


def audit_log(conn, detail):
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit.audit_trail (table_name, record_id, action, new_data, changed_by, application) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                ("system_governance", f"ret_{datetime.now(timezone.utc):%Y%m%d}",
                 "DELETE", json.dumps(detail), "governance_retention", "zhanlu-governance"))
            conn.commit()
    except Exception as e:
        logger.warning(f"Audit log failed: {e}")
        conn.rollback()


def purge_soft_deleted(conn):
    logger.info(f"Purging soft-deleted records >{SOFT_DELETE_DAYS}d...")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM governance.purge_soft_deleted(%s)", (SOFT_DELETE_DAYS,))
            rows = cur.fetchall()
            conn.commit()
            total = sum(r[1] for r in rows) if rows else 0
            logger.info(f"Purged {total} soft-deleted rows across {len(rows)} tables")
            audit_log(conn, {"action": "purge_soft_deleted", "retention_days": SOFT_DELETE_DAYS, "total": total})
            return total
    except Exception as e:
        logger.error(f"Purge failed: {e}")
        conn.rollback()
        return 0


def expire_snapshots(conn):
    logger.info(f"Expiring snapshots >{SNAPSHOT_DAYS}d...")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT governance.expire_snapshots(%s)", (SNAPSHOT_DAYS,))
            count = cur.fetchone()[0] or 0
            conn.commit()
            logger.info(f"Expired {count} snapshots")
            audit_log(conn, {"action": "expire_snapshots", "retention_days": SNAPSHOT_DAYS, "total": count})
            return count
    except Exception as e:
        logger.error(f"Snapshot expiry failed: {e}")
        conn.rollback()
        return 0


def clean_sandbox():
    path = Path(SANDBOX_TMP)
    if not path.exists():
        logger.info(f"Sandbox tmp not found: {SANDBOX_TMP}")
        return 0
    cutoff = datetime.now() - timedelta(hours=SANDBOX_HOURS)
    count = 0
    bytes_freed = 0
    for item in path.rglob("*"):
        if item.is_file():
            mtime = datetime.fromtimestamp(item.stat().st_mtime)
            if mtime < cutoff:
                try:
                    bytes_freed += item.stat().st_size
                    item.unlink()
                    count += 1
                except OSError:
                    pass
    # Clean empty dirs
    for item in sorted(path.rglob("*"), reverse=True):
        if item.is_dir() and not any(item.iterdir()):
            try:
                item.rmdir()
            except OSError:
                pass
    logger.info(f"Cleaned {count} stale sandbox files ({bytes_freed} bytes freed)")
    return count


def main():
    logger.info("Starting governance retention run")
    conn = get_conn()
    try:
        deleted = purge_soft_deleted(conn)
        expired = expire_snapshots(conn)
        cleaned = clean_sandbox()
        summary = {"soft_deleted_rows": deleted, "snapshots_expired": expired, "sandbox_files_cleaned": cleaned}
        logger.info(f"Done: {json.dumps(summary)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
