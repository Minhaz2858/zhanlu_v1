"""Shared helpers for the dashboard E2E suite. Not a test module."""
from __future__ import annotations

import os
import socket
import subprocess

import pytest

RDS_HOST_MARK = "rds.aliyuncs.com"
BAD_HOST = "10.10.10.49"  # reachable nowhere from here -> fast "no route" fail

NOW_WIDGET = {"id": "w_ts", "type": "kpi", "title": "Live Time",
              "sql": "SELECT NOW(6) AS ts", "options": {"value_column": "ts"}}
VERSION_WIDGET = {"id": "w_ver", "type": "kpi", "title": "DB Version",
                  "sql": "SELECT VERSION() AS v", "options": {"value_column": "v"}}


def parse_psql_row(line: str) -> dict | None:
    parts = [p.strip() for p in (line or "").split("|")]
    if len(parts) < 6 or not parts[0]:
        return None
    try:
        port = int(parts[2] or 3306)
    except ValueError:
        return None
    return {"kb_id": parts[0], "host": parts[1], "port": port,
            "database_name": parts[3], "username": parts[4], "password": parts[5]}


def _docker_psql(sql: str) -> str | None:
    cmd = ["docker", "exec", "zhanlu-postgres", "psql", "-U", "zhanlu",
           "-d", "zhanlu", "-t", "-A", "-F", "|", "-c", sql]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def get_mysql_kb_config() -> dict | None:
    """Env override (CI) else docker-psql discovery. None if unavailable."""
    if os.environ.get("E2E_MYSQL_HOST"):
        return {"kb_id": None, "host": os.environ["E2E_MYSQL_HOST"],
                "port": int(os.environ.get("E2E_MYSQL_PORT", "3306")),
                "database_name": os.environ["E2E_MYSQL_DB"],
                "username": os.environ["E2E_MYSQL_USER"],
                "password": os.environ["E2E_MYSQL_PASS"]}
    sql = ("SELECT id, host, port, database_name, username, password "
           "FROM knowledge_bases WHERE source_kind='database' AND db_type='mysql' "
           "AND host LIKE '%rds.aliyuncs.com' AND is_deleted=false "
           "ORDER BY updated_date DESC LIMIT 1;")
    out = _docker_psql(sql)
    if not out:
        return None
    return parse_psql_row(out.splitlines()[0] if out else "")


def rds_reachable(host: str, port: int = 3306, timeout: int = 4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def make_scratch_kb(db, cfg, org_id, host_override=None):
    """Create a real KnowledgeBase row (in the test sqlite DB) pointing at the
    real MySQL. get_connector(kb) reads host/port/db/user/pass to connect."""
    from app.models.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(
        name="e2e-scratch-mysql", source_kind="database", db_type="mysql",
        host=host_override or cfg["host"], port=cfg["port"],
        database_name=cfg["database_name"], username=cfg["username"],
        password=cfg["password"], org_id=org_id, app_id="default-app",
    )
    db.add(kb)
    db.commit()
    return kb


def require_live_or_skip():
    """Return the MySQL config or pytest.skip with a clear reason."""
    cfg = get_mysql_kb_config()
    if not cfg:
        pytest.skip("live DB config unavailable (docker/psql or E2E_MYSQL_* env)")
    if not rds_reachable(cfg["host"], cfg["port"]):
        pytest.skip(f"MySQL RDS unreachable: {cfg['host']}:{cfg['port']}")
    return cfg
