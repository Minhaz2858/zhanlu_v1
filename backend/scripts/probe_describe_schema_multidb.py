#!/usr/bin/env python3
"""DB-agnosticism proof for describe_schema(table=X) — PostgreSQL + MariaDB.

Runs the fixed ``_describe_schema`` against NON-Ecisco bound data sources and
checks the two properties the fix must hold for ANY database:

  1. an explicit ``table`` is honoured (that table appears in the payload), and
  2. two different tables return two DIFFERENT payloads.

Table names are discovered from each KB's no-table call, so nothing is
hardcoded and a brand-new database behaves the same way.

Verdict is honest: a KB that could not be checked is reported as SKIP and does
NOT count as a pass. PASS requires at least one KB actually verified with zero
failures.
"""
import asyncio
import hashlib
import sys

sys.path.insert(0, "/app")

KBS = [
    ("postgres / acme_erp", "d3d59b9c-74d8-4238-a0aa-2f96c4dbddc7"),
    ("mariadb  / acme_mariadb", "2ef3db2d-d7eb-4142-a245-d4e3dd6771f1"),
    ("mysql    / aipdp_data_warehouse_prod",
     "59c116e3-9b90-4434-baa4-bc3439cacbf2"),
]


def _discover_tables(result: dict) -> list[str]:
    """Pull table names out of whatever shape the no-table call returns.

    The no-table path renders a ``schema`` blob of ``Table: <name>`` blocks;
    other paths return ``tables`` as a list of dicts or strings.
    """
    names: list[str] = []
    for entry in (result.get("tables") or []):
        if isinstance(entry, dict):
            t = entry.get("table") or entry.get("table_name")
            if isinstance(t, str):
                names.append(t)
        elif isinstance(entry, str):
            names.append(entry)

    blob = result.get("schema") or ""
    if isinstance(blob, str):
        for line in blob.splitlines():
            s = line.strip()
            if s.startswith("Table: "):
                t = s[len("Table: "):].strip()
                if t and t not in names:
                    names.append(t)

    if not names and isinstance(result.get("table"), str):
        names.append(result["table"])
    return names


async def main():
    from app.database import SessionLocal
    from app.models.user import User
    from app.services.tool_handlers import db_tools

    db = SessionLocal()
    checked: list[str] = []
    failures: list[str] = []
    skipped: list[str] = []
    try:
        u = db.query(User).filter(User.role == "admin").first()
        uid = u.id if u else None

        for label, kb in KBS:
            def ctx():
                return {
                    "bound_kb_ids": [kb],
                    "schema_linking_enabled": True,
                    "schema_graph_enabled": True,
                    "schema_validator_enabled": True,
                }

            print(f"\n=== {label}  kb={kb}")

            listing = await db_tools._describe_schema(
                {"data_source_id": kb}, db, uid, ctx())
            tables = _discover_tables(listing)
            print(f"  discovered tables: {tables[:8]}")
            if len(tables) < 2:
                skipped.append(label)
                print(f"  SKIP: need 2+ tables to compare (got {len(tables)}); "
                      f"not counted as a pass")
                print(f"  raw head: {str(listing)[:180]}")
                continue

            hashes: dict[str, str] = {}
            missing: list[str] = []
            for t in tables[:3]:
                res = await db_tools._describe_schema(
                    {"data_source_id": kb, "table": t}, db, uid, ctx())
                p = str(res)
                hashes[t] = hashlib.md5(p.encode()).hexdigest()[:12]
                present = t.lower() in p.lower()
                if not present:
                    missing.append(t)
                print(f"  table={t:22s} len={len(p):6d} md5={hashes[t]} "
                      f"present={present}")

            distinct = len(set(hashes.values())) == len(hashes)
            print(f"  -> distinct payloads: {distinct}")
            if missing:
                failures.append(f"{label}: {missing} absent from their payload")
                print(f"  -> FAIL: requested table missing from payload {missing}")
            elif not distinct:
                failures.append(f"{label}: identical payloads across tables")
                print("  -> FAIL: different tables returned the SAME payload")
            else:
                checked.append(label)
                print("  -> OK: per-table payloads are honoured and distinct")
    finally:
        db.close()

    print("\n---  summary ---")
    print("verified :", checked or "none")
    print("skipped  :", skipped or "none")
    print("failures :", failures or "none")
    if failures:
        print("RESULT: FAIL")
    elif checked:
        print("RESULT: PASS (DB-agnostic)")
    else:
        print("RESULT: INCONCLUSIVE (nothing was actually verified)")


asyncio.run(main())
