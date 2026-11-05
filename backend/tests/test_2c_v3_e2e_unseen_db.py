"""2C acceptance — reviewer P0 #5: actual query results agree with the
streamed AND persisted answers, over a database Zhanlu has NEVER seen.

ONE integrated test per unfamiliar synthetic schema:

    REAL discovery (catalog indexer + classification backfill) → real v3 SSE
    route → real query_database tool → SynexiaFSM → real NLAnswerService →
    real sqlite connector execution → rows → answer text built FROM those
    rows → done.content → persisted assistant message

The ONLY stubs are the LLM boundaries. The final-answer stub extracts the
rows the REAL pipeline attached to the tool result and phrases them verbatim
— so if any stage drops or alters the rows, the agreement assertions fail.
The selection stub additionally asserts the discovered table actually
reached the selection prompt (discovery → prompt → answer chain).
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import helpers_2c_schemas as fx  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.deps import get_db, get_current_user_required  # noqa: E402

#: question + selection + SQL per schema (the SQL is REAL and executes
#: against the synthetic sqlite file through the real connector).
_SCHEMA_PLAN = {
    "sales": {
        "question": (
            "show the total transaction amount booked across the whole company"
        ),
        "table": "txn_hdr",
        "sql": "SELECT SUM(total_amt) AS total_booked FROM txn_hdr",
        "fact": "txn_hdr_total_amt_sum",
    },
    "finance": {
        "question": (
            "show the total debit and credit amounts posted across the "
            "whole company"
        ),
        "table": "journal_line",
        "sql": (
            "SELECT SUM(amount_dr) AS dr_total, SUM(amount_cr) AS cr_total "
            "FROM journal_line"
        ),
        "fact": ("amount_dr_total", "amount_cr_total"),
    },
}


def _extract_rows(messages) -> list[dict]:
    """Pull the rows the REAL pipeline attached to the tool result."""
    for m in reversed(messages or []):
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, str) or '"rows"' not in c:
            continue
        mm = re.search(r'"rows"\s*:\s*(\[[^\]]*\])', c, re.DOTALL)
        if mm:
            try:
                rows = json.loads(mm.group(1))
                if isinstance(rows, list):
                    return rows
            except json.JSONDecodeError:
                continue
    return []


def _phrase_answer(rows: list[dict]) -> str:
    """Deterministic answer phrasing FROM the real rows — no invention."""
    values: list[str] = []
    for row in rows[:3]:
        if isinstance(row, dict):
            for v in row.values():
                if isinstance(v, (int, float)):
                    values.append(f"{v}")
                elif isinstance(v, str):
                    values.append(v)
    return "Requested figures: " + ", ".join(values) + "."


def _run_schema_e2e(monkeypatch, schema: str) -> tuple[str, dict, list[dict]]:
    import app.routers.agents as agents_mod
    import app.services.db.nl_answer_service as nls
    from app.models.agent_app import AgentApp
    from app.models.agent_conversation import AgentConversation
    from app.models.user import User
    from app.services import workspace_settings_service as wss

    plan = _SCHEMA_PLAN[schema]
    db = SessionLocal()
    try:
        # ── Seed: user, agent, conversation, KB, REAL discovery ─────────
        org_id = f"org-{uuid.uuid4().hex[:10]}"
        user = User(
            email=f"2c-e2e-{uuid.uuid4().hex[:8]}@test.local",
            full_name="2C E2E",
            role="admin",
            password_hash="x",
            org_id=org_id,
            app_id="app-e2e",
        )
        db.add(user)
        db.flush()
        app_row = AgentApp(
            id=f"app-{uuid.uuid4().hex[:12]}",
            name="data_analyst_2c",
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
            title=f"2c-{schema}-e2e",
            messages=[],
        )
        db.add(conv)
        db.flush()

        path, facts = fx.build_sqlite_db(schema)
        kb = fx.seed_kb(
            db, path=path, schema=schema, org_id=org_id, app_id="app-e2e",
        )
        # REAL discovery — the catalog the pipeline answers from is built,
        # not declared.
        fx.discover(db, kb, monkeypatch)

        # The agent reads every database KB in the workspace (opt-in).
        wss.set_value(
            db, wss.KEY_AUTO_BIND_ALL_DATASOURCES, True,
            org_id=org_id, app_id="app-e2e",
        )
        wss.clear_cache()
        db.commit()

        # ── Flags: real data path, flakiness OFF ─────────────────────────
        monkeypatch.setattr(agents_mod, "STREAM_TOKEN_DELTAS", False)
        monkeypatch.setattr(settings, "V3_DATA_ROUTE_MODE", "react")
        monkeypatch.setattr(
            settings, "KG_FRESHNESS_SHORTCIRCUIT_ENABLED", False,
        )
        monkeypatch.setattr(settings, "SCHEMA_LINKING_ENABLED", True)
        monkeypatch.setattr(settings, "SEMANTIC_CATALOG_ENABLED", True)
        monkeypatch.setattr(
            settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False,
        )
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

        # ── LLM boundaries ───────────────────────────────────────────────
        _TASK_SPEC = {
            "task_kind": "analyze_data",
            "artifact_intents": [],
            "entities": {"metric": "requested_figures"},
            "complexity": "simple",
            "requires_data": True,
            "dimensions": [],
            "standalone_question": plan["question"],
            "user_signal": "default",
            "is_followup": False,
            "refines_artifact_id": None,
        }

        async def fake_call_llm(*a, prompt=None, **kw):
            if prompt and "task_kind" in str(prompt):
                return {"data": dict(_TASK_SPEC), "response": None}
            raise RuntimeError("llm stubbed off in 2C e2e")

        monkeypatch.setattr(
            "app.services.llm_service.call_llm", fake_call_llm,
        )

        async def fake_chat(messages, **kw):
            # The discovered table must have reached the selection prompt —
            # discovery → TOC → prompt chain, or this assert fires.
            assert plan["table"] in str(messages), (
                f"{schema}: the discovered table {plan['table']!r} never "
                "reached the table-selection prompt"
            )
            return json.dumps([plan["table"]])

        monkeypatch.setattr(nls, "_chat", fake_chat)

        async def fake_text_to_sql(self, *a, **k):
            return plan["sql"]

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
                            "arguments": json.dumps(
                                {"question": plan["question"]}
                            ),
                        }}],
                }
            rows = _extract_rows(messages)
            assert rows, (
                "the real pipeline never delivered rows to the orchestrator"
            )
            return {"content": _phrase_answer(rows), "tool_calls": []}

        monkeypatch.setattr(
            agents_mod, "_call_llm_with_tools", fake_orchestrator_llm,
        )

        async def fake_synth(*args, **kwargs):
            prompt_text = str(args[0] if args else kwargs) or ""
            # The post-tool synthesis sees the tool result too; phrase FROM it.
            rows = []
            mm = re.search(r'"rows"\s*:\s*(\[[^\]]*\])', prompt_text, re.DOTALL)
            if mm:
                try:
                    rows = json.loads(mm.group(1))
                except json.JSONDecodeError:
                    rows = []
            if rows:
                return {"content": _phrase_answer(rows)}
            return {"content": "Requested figures: (no rows returned)."}

        monkeypatch.setattr(agents_mod, "_call_synthesis_llm", fake_synth)

        # ── Drive the REAL v3 SSE route ──────────────────────────────────
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
                json={"content": plan["question"]},
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

        db.expire_all()
        conv_row = db.refresh(conv) or conv
        persisted = [
            m for m in (conv_row.messages or [])
            if isinstance(m, dict) and m.get("role") == "assistant"
        ]
        return (
            str(done.get("content") or ""),
            (done or {}),
            [m.get("content") for m in persisted if isinstance(m, dict)],
            facts,
            plan,
        )
    finally:
        db.close()


def _norm(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.strip().splitlines())


@pytest.mark.parametrize("schema", ["sales", "finance"])
def test_rows_agree_with_streamed_and_persisted_answer(monkeypatch, schema):
    streamed, done, persisted, facts, plan = _run_schema_e2e(
        monkeypatch, schema,
    )

    # 1. The turn completed with a real answer (this branch's done frame
    #    predates the stable-answer completion_status field; the honest
    #    completion contract here is non-empty content + persistence parity,
    #    exactly as in the 2B-3 acceptance e2e).
    assert streamed.strip(), "empty done.content"

    # 2. The REAL executed rows agree with the fixture facts (the data the
    #    connector returned IS the data we seeded).
    fact_keys = plan["fact"] if isinstance(plan["fact"], tuple) else (
        plan["fact"],
    )
    expected = [facts[k] for k in fact_keys]

    # 3. The STREAMED answer contains the exact real figures — the answer
    #    was phrased from the rows the pipeline actually returned.
    for value in expected:
        assert f"{value}" in streamed, (
            f"{schema}: streamed answer does not contain the real figure "
            f"{value!r}; streamed={streamed[:300]!r}"
        )

    # 4. The PERSISTED assistant message equals the streamed content.
    assert persisted, "no assistant message persisted into conv.messages"
    persisted_content = str(persisted[-1] or "")
    assert _norm(streamed) == _norm(persisted_content), (
        f"{schema}: streamed done.content and persisted assistant message "
        f"differ:\nstreamed={streamed[:200]!r}\npersisted="
        f"{persisted_content[:200]!r}"
    )


def test_finance_conflicting_measures_both_figures_reported(monkeypatch):
    """The two bases (amount_dr / amount_cr) must BOTH survive to the
    answer — a blended or dropped basis is exactly the conflicting-measure
    failure the schema was built to catch."""
    streamed, _done, _persisted, facts, _plan = _run_schema_e2e(
        monkeypatch, "finance",
    )
    assert f"{facts['amount_dr_total']}" in streamed
    assert f"{facts['amount_cr_total']}" in streamed
    # They are genuinely different bases in the fixture — a blend would
    # produce a single number.
    assert facts["amount_dr_total"] != facts["amount_cr_total"]
