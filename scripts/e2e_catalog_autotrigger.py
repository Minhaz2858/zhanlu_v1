#!/usr/bin/env python3
"""Live E2E: a KB created through the GENERIC ENTITY API (no /catalog/reindex
call, no UI) must auto-index its semantic catalog.

Proves both fixes at once:
  F1  the create hook now reads the dict that create_record returns
  F2  mariadb (a dialect the old hardcoded allowlist rejected) is cataloged

Run from the repo root against the copy stack on :5012.
"""
import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://localhost:5012"
APP = "default-app"


def call(method, path, body=None, token=None, timeout=60):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def login():
    code, data = call("POST", f"/api/apps/{APP}/auth/login",
                      {"email": "admin@zhanlu.dev", "password": "admin123"})
    assert code == 200, (code, data)
    return data["access_token"]


def create_kb(token, label, spec):
    code, data = call("POST", f"/api/apps/{APP}/entities/KnowledgeBase",
                      {"name": label, "source_kind": "database", "status": "active",
                       **spec}, token)
    if code not in (200, 201):
        print(f"  !! create failed {code}: {data}")
        return None
    return data


def watch(token, kb_id, seconds=100):
    """Poll catalog_status until ready/error. NO manual reindex is ever called."""
    seen = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        code, data = call("GET", f"/api/apps/{APP}/knowledge_bases/{kb_id}/catalog/status", None, token)
        status = (data or {}).get("catalog_status", f"http_{code}")
        if not seen or seen[-1][1] != status:
            seen.append((round(time.time() - t0, 1), status))
        if status in ("ready", "error"):
            break
        time.sleep(2)
    return seen


def catalog_tables(token, kb_id):
    code, data = call("GET", f"/api/apps/{APP}/knowledge_bases/{kb_id}/catalog/tables", None, token)
    if code != 200:
        return code, data
    return code, data


CASES = [
    # same credentials the existing working ACME ERP KB uses (host = compose
    # service name, reachable from inside the backend container)
    ("PG-AUTO-API-ONLY", {
        "db_type": "postgresql", "host": "postgres", "port": 5432,
        "database_name": "acme_erp", "username": "zhanlu",
        "password": "***DB- password-REDACTED***",
    }),
    ("MARIADB-AUTO-API-ONLY", {
        "db_type": "mariadb", "host": "zhanlu-copy-mariadb", "port": 3306,
        "database_name": "acme_mariadb", "username": "root",
                       "password": os.environ.get("ACME_MARIADB_PASSWORD", ""),
    }),
]

token = login()
print(f"logged in (token {len(token)} chars)\n")

results = {}
for label, spec in CASES:
    print(f"=== {label}  db_type={spec['db_type']} ===")
    kb = create_kb(token, label, spec)
    if not kb:
        results[label] = "CREATE FAILED"
        continue
    kb_id = kb["id"]
    print(f"  created kb={kb_id}  catalog_status={kb.get('catalog_status')!r}")
    print("  (no /catalog/reindex call — watching for the backend auto-trigger)")
    timeline = watch(token, kb_id)
    print("  timeline: " + "  ".join(f"t+{t}s={s}" for t, s in timeline))
    code, tables = catalog_tables(token, kb_id)
    n = len(tables.get("tables", [])) if isinstance(tables, dict) else 0
    names = [t["table_name"] for t in tables.get("tables", [])] if isinstance(tables, dict) else []
    print(f"  catalog tables: {n} {names}")
    if isinstance(tables, dict):
        desc = [(t["table_name"], (t.get("description_zh") or "")[:40]) for t in tables["tables"]]
        for nm, d in desc:
            print(f"    - {nm}: {d}")
        print(f"  item_count={tables.get('item_count')}")
    results[label] = timeline[-1][1] if timeline else "no-status"
    print()

print("=== SUMMARY ===")
for k, v in results.items():
    print(f"  {'PASS' if v == 'ready' else 'FAIL'}  {k}: {v}")
