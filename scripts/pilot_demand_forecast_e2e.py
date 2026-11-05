#!/usr/bin/env python3
"""P0 Phase 2 pilot E2E: Demand Forecasting Agent on a sandbox enterprise DB.

Run INSIDE the backend container (needs DATABASE_URL for the sandbox password
and reaches the API on localhost:5002):

    docker exec -w /app -e PYTHONPATH=/app zhanlu-backend python \
        /app/scripts/pilot_demand_forecast_e2e.py

Prerequisite: scripts/seed_sandbox_auto.py has run (DB zhanlu_sandbox_auto).

Flow: login → create KB → watch auto-catalog → verify catalog → forecast-series
discovery → ontology object seeding + discover_bindings → bind KB to the
Demand Forecasting Agent → real v2 conversations (grounded G1, refusal R1)
→ agent_invocations evidence check.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:5002"
APP = "default-app"
AGENT_NAME = "Demand Forecasting Agent"
DB_NAME = "zhanlu_sandbox_auto"
ACCEPT_2025_FALCON = "332538514"  # from seed_sandbox_auto.py acceptance print


def call(method, path, body=None, token=None, timeout=120):
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
        return e.code, e.read().decode()[:600]


def stream_message(token, conversation_id, content, timeout=900):
    """Send a user message via the production v3 SSE endpoint and return
    (assistant_text, tool_progress_count, done_event)."""
    req = urllib.request.Request(
        BASE + f"/api/apps/{APP}/agents/conversations/v3/{conversation_id}/messages/stream",
        method="POST",
        data=json.dumps({"role": "user", "content": content}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    text_parts, tool_events, done = [], 0, None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            et = ev.get("type")
            if et == "delta":
                text_parts.append(ev.get("content") or "")
            elif et == "tool_progress":
                tool_events += 1
            elif et in ("done", "paused", "stream_error"):
                done = ev
                break
    text = "".join(text_parts)
    if not text and done:
        # Some delivery paths emit the full answer ONLY in the terminal
        # done event's content field (no delta chunks). Prefer delta
        # accumulation, fall back to done.content.
        text = done.get("content") or ""
    return text, tool_events, done


def login():
    code, data = call("POST", f"/api/apps/{APP}/auth/login",
                      {"email": "admin@zhanlu.dev", "password": "admin123"})
    assert code == 200, (code, data)
    return data["access_token"]


def _pg_password():
    from sqlalchemy.engine import make_url
    url = os.environ.get("DATABASE_URL", "")
    return make_url(url).password or "" if url else ""


def find_kb(token):
    code, data = call(
        "GET",
        f"/api/apps/{APP}/entities/KnowledgeBase?q="
        + urllib.parse.quote(json.dumps({"database_name": DB_NAME})),
        token=token,
    )
    rows = data if isinstance(data, list) else (data or {}).get("records") or []
    return rows[0] if rows else None


def create_kb(token):
    existing = find_kb(token)
    if existing:
        print(f"KB exists: {existing['id']}")
        return existing
    code, data = call(
        "POST", f"/api/apps/{APP}/entities/KnowledgeBase",
        {
            "name": "Auto Sales Sandbox (Pilot)",
            "source_kind": "database",
            "status": "active",
            "db_type": "postgresql",
            "host": "postgres",
            "port": 5432,
            "database_name": DB_NAME,
            "username": "zhanlu_app",
            "password": _pg_password(),
        },
        token,
    )
    assert code in (200, 201), (code, data)
    print(f"KB created: {data['id']}  catalog_status={data.get('catalog_status')!r}")
    return data


def watch_catalog(token, kb_id, seconds=300):
    t0 = time.time()
    timeline = []
    while time.time() - t0 < seconds:
        code, data = call(
            "GET", f"/api/apps/{APP}/knowledge_bases/{kb_id}/catalog/status", None, token)
        status = (data or {}).get("catalog_status", f"http_{code}")
        if not timeline or timeline[-1][1] != status:
            timeline.append((round(time.time() - t0, 1), status))
        if status in ("ready", "error"):
            break
        time.sleep(3)
    return timeline


def main():
    token = login()
    print("logged in")

    kb = create_kb(token)
    kb_id = kb["id"]
    print("catalog timeline:", watch_catalog(token, kb_id))

    code, tables = call(
        "GET", f"/api/apps/{APP}/knowledge_bases/{kb_id}/catalog/tables", None, token)
    tnames = [t["table_name"] for t in (tables or {}).get("tables", [])] if isinstance(tables, dict) else []
    print(f"catalog tables ({len(tnames)}):", tnames)
    assert len(tnames) >= 4, "catalog must describe the sandbox tables"

    # ── forecast-series discovery ──
    code, disc = call("POST", f"/api/apps/{APP}/knowledge_bases/{kb_id}/discover", {}, token)
    print("forecast discovery:", {k: disc.get(k) for k in
          ("success", "tables_scanned", "candidates_found", "targets_written")} if isinstance(disc, dict) else disc)

    # ── ontology mapping: seed objects, then HUMAN-bind via the API ──
    # (design-correct flow: discovery proposes, a human bind grants
    # 'verified' evidence — services/ontology/evidence.py stamp_human_bind)
    from app.database import SessionLocal
    from app.models.ontology import OntologyDomain, OntologyObject

    db = SessionLocal()
    try:
        org_id = "default-org"
        domain = db.query(OntologyDomain).filter(
            OntologyDomain.org_id == org_id, OntologyDomain.code == "auto_sales"
        ).first()
        if domain is None:
            domain = OntologyDomain(org_id=org_id, code="auto_sales", name="Automotive Sales")
            db.add(domain)
            db.flush()
        spec = {
            "VehicleModel": ("vehicle_model",
                             ["model_code", "model_name", "segment", "launch_year", "msbp"]),
            "Dealer": ("dealer", ["dealer_code", "dealer_name", "region"]),
            "SalesOrder": ("sales_order",
                           ["order_id", "order_date", "model_code", "dealer_code",
                            "units", "unit_price", "revenue"]),
            "MonthlySales": ("monthly_sales",
                             ["month", "model_code", "units_sold", "avg_price", "revenue"]),
        }
        objs = {}
        for name, (table, props) in spec.items():
            obj = db.query(OntologyObject).filter(
                OntologyObject.org_id == org_id,
                OntologyObject.domain_id == domain.id,
                OntologyObject.name == name,
                OntologyObject.is_deleted == False,  # noqa: E712
            ).first()
            if obj is None:
                obj = OntologyObject(
                    org_id=org_id, domain_id=domain.id, name=name,
                    description=f"Pilot {name} business object",
                    properties_json=[{"name": p} for p in props],
                )
                db.add(obj)
                db.flush()
            objs[name] = obj
        db.commit()
        obj_ids = {n: o.id for n, o in objs.items()}
    finally:
        db.close()

    for name, (table, props) in spec.items():
        code, resp = call(
            "PATCH",
            f"/api/ontology/objects/{obj_ids[name]}/bind",
            {
                "backing_kind": "table",
                "backing_ref": f"kb:{kb_id}:{table}",
                "properties": [{"name": p, "column": p} for p in props],
            },
            token,
        )
        assert code in (200, 201), (name, code, resp)
    print("ontology: 4 objects human-bound (verified) via PATCH /ontology/objects/{id}/bind")

    # agent → domain binding (read scope) so the pilot can use query_ontology
    code, resp = call(
        "POST", "/api/ontology/bindings",
        {"domain_code": "auto_sales", "agent_name": AGENT_NAME,
         "permission_scope": "read"},
        token,
    )
    print("agent domain binding:", code,
          "ok" if code in (200, 201) else resp)

    # ── bind KB to the pilot agent ──
    code, agents = call(
        "GET",
        f"/api/apps/{APP}/entities/AgentApp?q="
        + urllib.parse.quote(json.dumps({"name": AGENT_NAME})),
        token=token,
    )
    rows = agents if isinstance(agents, list) else (agents or {}).get("records") or []
    assert rows, "pilot AgentApp must exist"
    agent_id = rows[0]["id"]
    code, _ = call(
        "PUT", f"/api/apps/{APP}/entities/AgentApp/{agent_id}",
        {"knowledge_bases": [kb_id]}, token,
    )
    assert code in (200, 201), (code, _)
    print(f"agent bound: {AGENT_NAME} ({agent_id}) → KB {kb_id}")

    # ── real conversation: grounded answer ──
    code, conv = call(
        "POST", f"/api/apps/{APP}/agents/conversations",
        {"agent_name": AGENT_NAME, "metadata": {"name": "P0 pilot"}}, token)
    assert code in (200, 201), (code, conv)
    conv_id = conv["id"]
    print(f"conversation: {conv_id}")

    g1 = ("Which vehicle model generated the highest total sales revenue in "
          "2025, and what was the exact revenue figure? Use the sales data.")
    answer, tool_events, done = stream_message(token, conv_id, g1)
    print(f"G1 events: tool_progress={tool_events}, done={bool(done)}")
    print("G1 answer:", answer[:900])
    norm = answer.replace(",", "").replace(" ", "")
    falcon_mentioned = "falcon" in answer.lower()
    number_ok = ACCEPT_2025_FALCON in norm
    g1_ok = falcon_mentioned and number_ok
    print(f"G1 verdict: {'PASS' if g1_ok else 'CHECK MANUALLY'} "
          f"(falcon={falcon_mentioned}, figure={number_ok})")

    # ── refusal behavior ──
    r1 = "How many employees took vacation leave last month?"
    a2, r1_tools, r1_done = stream_message(token, conv_id, r1)
    print(f"R1 events: tool_progress={r1_tools}")
    print("R1 answer:", a2[:600])

    # ── observability evidence ──
    from app.models.agent_invocation import AgentInvocation
    from app.database import SessionLocal as _SL
    s = _SL()
    try:
        invs = (
            s.query(AgentInvocation)
            .filter(AgentInvocation.conversation_id == conv_id)
            .all()
        )
        print(f"invocations linked to conversation {conv_id}: {len(invs)}")
        for i in invs:
            print(f"  type={i.invocation_type} status={i.status} model={i.model_name} "
                  f"tools={i.tool_call_count} dur={i.duration_ms}ms app={i.agent_app_id}")
        assert invs, "turns must be recorded and linked to the conversation"
    finally:
        s.close()

    print("PILOT E2E DONE")


if __name__ == "__main__":
    main()
