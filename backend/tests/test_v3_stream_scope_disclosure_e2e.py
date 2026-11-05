"""Review requirement A (2B-3 acceptance gate): ONE integrated test drives

    synthetic sqlite DB → real query_database tool → SynexiaFSM → real
    NLAnswerService → real scope authorization (P-SV + execution gate) →
    scope disclosure → the actual v3 SSE stream → done.content → persisted
    assistant message

with exact content equality after normalizing trailing whitespace.

Real everything on the data path: the tool executor, the FSM pipeline, the
NL2SQL funnel, the P-SV plan gate, the execution gate, the sqlite connector,
the SSE stream, and persistence. The ONLY stubs are the LLM boundaries
(``_call_llm_with_tools`` and the NL service's table-selection/SQL-generation
calls), mirroring test_react_stream_recovery_e2e.py. The final-answer stub
REPLAYS the scope disclosure it received from the pipeline verbatim — so if
any stage drops the disclosure, the content-equality assertion fails.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.deps import get_db, get_current_user_required  # noqa: E402

QUESTION = "show me all customers across the whole company"


def _make_synthetic_db() -> str:
    """A synthetic customer DB whose only table is a regional partition —
    no company-wide dataset exists. Generic schema, no ERP vocabulary."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT)")
    con.execute("INSERT INTO customers VALUES (1,'Alice','north'),(2,'Bob','north')")
    con.commit()
    con.close()
    return path


def _seed_env(db):
    from app.models.agent_app import AgentApp
    from app.models.agent_conversation import AgentConversation
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta
    from app.models.user import User
    from app.services import workspace_settings_service as wss

    org_id = f"org-{uuid.uuid4().hex[:10]}"
    user = User(
        email=f"scope-e2e-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Scope E2E",
        role="admin",
        password_hash="x",
        org_id=org_id,
        app_id="app-e2e",
    )
    db.add(user)
    db.flush()

    app_row = AgentApp(
        id=f"app-{uuid.uuid4().hex[:12]}",
        # NOT general_assistant: the workspace-default agent's anti-leak
        # guard (data_source_runtime.prepare_data_source_runtime) zeroes
        # bound KBs when no project is selected — by design. A named agent
        # keeps its own/auto-bind scope, which is what this test exercises.
        name="sales_analyst",
        org_id=org_id,
        app_id="app-e2e",
        status="published",
        capabilities=["query_database"],
    )
    db.add(app_row)
    db.flush()

    conv = AgentConversation(
        id=str(uuid.uuid4()),
        agent_name=app_row.name,
        org_id=org_id,
        app_id="app-e2e",
        created_by_id=user.id,
        title="2b3-scope-e2e",
        messages=[],
    )
    db.add(conv)
    db.flush()

    ds_path = _make_synthetic_db()
    kb = KnowledgeBase(
        id=f"kb-{uuid.uuid4().hex[:12]}",
        name="synthetic_customers",
        org_id=org_id,
        app_id="app-e2e",
        type="database",
        source_kind="database",
        db_type="sqlite",
        file_url=ds_path,
        database_name=ds_path,
        catalog_status="ready",
        nl2sql_state="nl2sql_ready",
    )
    db.add(kb)
    db.flush()
    db.add(KBTableMeta(
        id=str(uuid.uuid4()), kb_id=kb.id, schema_name="",
        table_name="customers", table_type="TABLE",
        description_en="customer records",
        dataset_scope="partition:region", dataset_scope_confidence=0.6,
        dataset_scope_signals_json={"partition_value": "north"},
    ))
    db.flush()

    # The agent reads every database KB in the workspace (explicit opt-in).
    wss.set_value(
        db, wss.KEY_AUTO_BIND_ALL_DATASOURCES, True,
        org_id=org_id, app_id="app-e2e",
    )
    wss.clear_cache()
    db.commit()
    return user, conv, kb


def _extract_scope_disclosure(messages) -> str:
    """Pull the scope disclosure the REAL pipeline attached to the tool
    result out of the conversation messages handed to the LLM."""
    for m in reversed(messages or []):
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, str) or "scope_disclosure" not in c:
            continue
        mm = re.search(r'"scope_disclosure"\s*:\s*"((?:[^"\\]|\\.)*)"', c)
        if mm:
            return json.loads('"' + mm.group(1) + '"')
    return ""


def test_scope_disclosure_survives_v3_stream_to_persistence(monkeypatch):
    import app.routers.agents as agents_mod
    import app.services.db.nl_answer_service as nls

    db = SessionLocal()
    try:
        user, conv, kb = _seed_env(db)

        # ── Flags: scope machinery ON, flakiness OFF ────────────────────────
        monkeypatch.setattr(agents_mod, "STREAM_TOKEN_DELTAS", False)
        monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")
        monkeypatch.setattr(settings, "KG_FRESHNESS_SHORTCIRCUIT_ENABLED", False)
        monkeypatch.setattr(settings, "SCHEMA_LINKING_ENABLED", True)
        monkeypatch.setattr(settings, "SEMANTIC_CATALOG_ENABLED", True)
        monkeypatch.setattr(settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False)
        monkeypatch.setattr(
            settings, "SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED", False,
        )
        monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", False)
        monkeypatch.setattr(settings, "NL2SQL_PLAN_MEMORY_ENABLED", False)
        monkeypatch.setattr(settings, "NL2SQL_DB_ERROR_REPAIR_MAX", 0)
        monkeypatch.setattr(
            settings, "SCHEMA_CLASSIFICATION_RETRIEVAL_ENABLED", True,
        )
        monkeypatch.setattr(
            settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", True,
        )

        # ── LLM boundaries: deterministic; the data path stays REAL ────────
        # The task-spec parser calls llm_service.call_llm with no endpoint —
        # stub it with a deterministic analyze_data TaskSpec so the FSM plans
        # a data node (raising would fall back to a zero-node plan).
        _TASK_SPEC = {
            "task_kind": "analyze_data",
            "artifact_intents": [],
            "entities": {"metric": "customer_count"},
            "complexity": "simple",
            "requires_data": True,
            "dimensions": [],
            "standalone_question": QUESTION,
            "user_signal": "default",
            "is_followup": False,
            "refines_artifact_id": None,
        }

        async def fake_call_llm(*a, prompt=None, **kw):
            if prompt and "task_kind" in str(prompt):
                return {"data": dict(_TASK_SPEC), "response": None}
            raise RuntimeError("llm stubbed off in scope e2e test")

        monkeypatch.setattr(
            "app.services.llm_service.call_llm", fake_call_llm,
        )

        async def fake_chat(messages, **kw):
            return '["customers"]'

        monkeypatch.setattr(nls, "_chat", fake_chat)

        async def fake_text_to_sql(self, *a, **k):
            return "SELECT id, name FROM customers"

        monkeypatch.setattr(
            "app.services.db.nl_answer_service.NLAnswerService._text_to_sql",
            fake_text_to_sql,
        )

        llm_calls = {"n": 0}

        async def fake_orchestrator_llm(messages, tools, tool_choice=None, **kw):
            idx = min(llm_calls["n"], 1)
            llm_calls["n"] += 1
            if idx == 0:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1", "type": "function", "function": {
                            "name": "query_database",
                            "arguments": json.dumps({"question": QUESTION}),
                        }}],
                }
            # Final synthesis: REPLAY the scope disclosure the REAL pipeline
            # attached to the tool result. If any stage dropped it, the
            # equality assertions below fail.
            disclosure = _extract_scope_disclosure(messages)
            assert disclosure, (
                "the real pipeline never delivered a scope_disclosure to the "
                "orchestrator — the chain is broken before synthesis"
            )
            return {"content": disclosure, "tool_calls": []}

        monkeypatch.setattr(
            agents_mod, "_call_llm_with_tools", fake_orchestrator_llm,
        )

        # The loop's post-tool synthesis is also an LLM boundary — replay
        # whatever the pipeline qualified the report with.
        async def fake_synth(*args, **kwargs):
            prompt_text = str(args[0] if args else kwargs) or ""
            mm = re.search(
                r'"scope_disclosure"\s*:\s*"((?:[^"\\]|\\.)*)"', prompt_text,
            )
            base = "Summary of the requested company-wide customer report."
            if mm:
                return {"content": base + "\n\n" + json.loads('"' + mm.group(1) + '"')}
            return {"content": base}

        monkeypatch.setattr(agents_mod, "_call_synthesis_llm", fake_synth)

        # ── Drive the REAL v3 SSE route ─────────────────────────────────────
        def _override_db():
            yield db

        def _override_user():
            return user

        api = FastAPI()
        api.include_router(agents_mod.router)
        api.dependency_overrides[get_db] = _override_db
        api.dependency_overrides[get_current_user_required] = _override_user

        frames = []
        with TestClient(api) as client:
            with client.stream(
                "POST",
                f"/apps/app-e2e/agents/conversations/v3/{conv.id}/messages/stream",
                json={"content": QUESTION},
            ) as resp:
                assert resp.status_code == 200, resp.read()[:500]
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        try:
                            frames.append(json.loads(line[len("data: "):]))
                        except json.JSONDecodeError:
                            pass

        done = next((f for f in frames if f.get("type") == "done"), None)
        assert done, f"no terminal done frame; got {[f.get('type') for f in frames]}"
        streamed = str(done.get("content") or "")
        import os as _os

        if _os.environ.get("SCOPE_E2E_DEBUG"):
            print("\nSCOPE_E2E done.content:", repr(streamed[:400]))
        assert "single partition" in streamed or "company-wide" in streamed, (
            "the disclosure did not survive to the streamed done.content"
        )

        # ── Persisted assistant message: EXACT content equality ────────────
        db.expire_all()
        conv_row = db.refresh(conv) or conv
        persisted = [
            m for m in (conv_row.messages or [])
            if isinstance(m, dict) and m.get("role") == "assistant"
        ]
        assert persisted, "no assistant message persisted into conv.messages"
        persisted_content = str(persisted[-1].get("content") or "")

        def _norm(s: str) -> str:
            return "\n".join(line.rstrip() for line in s.strip().splitlines())

        if _os.environ.get("SCOPE_E2E_DEBUG"):
            print("SCOPE_E2E persisted:", repr(persisted_content[:400]))
            print("SCOPE_E2E equal:", _norm(streamed) == _norm(persisted_content))
        assert _norm(streamed) == _norm(persisted_content), (
            "streamed done.content and the persisted assistant message "
            "diverged:\nSTREAMED:\n%r\nPERSISTED:\n%r"
            % (streamed, persisted_content)
        )
        assert "single partition" in persisted_content or "company-wide" in (
            persisted_content
        )
    finally:
        db.close()
