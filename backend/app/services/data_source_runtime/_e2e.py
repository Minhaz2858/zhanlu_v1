"""End-to-end verification of the data-source integration.

Strategy
--------
1. Create a temp SQLite file with a small `sales` table.
2. Insert a KnowledgeBase row pointing at that file.
3. Create an AgentApp with that KB bound (no `use_data_agent` flag — always on).
4. Call `prepare_data_source_runtime()` and assert:
     - `ask_data_agent` is in the tool list
     - the 4 granular tools are NOT (they are still registered for the
       subagent, but not auto-injected onto the calling agent)
     - the system prompt has a "Bound Data Sources" section
     - `ctx_extras["bound_kb_ids"]` contains the KB id
5. With no bound KBs, assert the function is a no-op.
6. Call the real `ask_data_agent` handler with a mocked LLM that
   emits a `describe_schema` then `execute_query` tool call, and assert
   the handler returns a structured payload with the expected rows.

Run with:
    /root/zhanlu/backend/venv/bin/python -m app.services.data_source_runtime._e2e
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# Make `app` importable.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_kb(db, path: str, kb_id: str = "kb_e2e") -> None:
    """Insert a real KnowledgeBase row pointing at the SQLite file."""
    from app.models.knowledge_base import KnowledgeBase
    row = KnowledgeBase(
        id=kb_id,
        name="E2E Sales",
        source_kind="database",
        db_type="sqlite",
        api_url=path,
        file_url=path,
        status="active",
    )
    db.add(row)
    db.commit()


def _make_agent(db, kb_id: str) -> "AgentApp":
    from app.models.agent_app import AgentApp
    agent = AgentApp(
        name="E2E Agent",
        description="e2e",
        project="global",
        capabilities=[],
        model="automatic",
        agent_type="sequential",
        prompt_identity="",
        prompt_boundary="",
        prompt_reasoning="",
        prompt_tools="",
        prompt_output="",
        skills=[],
        knowledge_bases=[kb_id],
        topology="standalone",
        sub_agents=[],
        max_call_count=50,
        max_retries=3,
        max_iterations=5,
        data_read=True,
        data_write=False,
        human_fallback=True,
        trace_enabled=True,
        log_level="info",
        temperature=0.7,
        top_p=1.0,
        max_tokens=4096,
        status="active",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _make_sqlite_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE sales (
                id INTEGER PRIMARY KEY,
                region TEXT NOT NULL,
                amount REAL NOT NULL
            );
            INSERT INTO sales (id, region, amount) VALUES
              (1, 'EU', 100.0), (2, 'EU', 250.0),
              (3, 'US', 75.0),  (4, 'US', 600.0);
            """
        )
        con.commit()
    finally:
        con.close()
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class _RuntimeInjection(unittest.TestCase):
    def setUp(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        # Use a temp-file metadata DB so it survives per-thread connections
        # spawned by `asyncio.to_thread()` inside the tool handlers.
        self.meta_fd, self.meta_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(self.meta_fd)
        self.engine = create_engine(f"sqlite:///{self.meta_path}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.path = _make_sqlite_db()
        _make_kb(self.db, self.path, "kb_e2e")
        self.kb_id = "kb_e2e"

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        for p in (self.path, self.meta_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _names(self, tools):
        return {t["function"]["name"] for t in tools}

    def test_bound_kb_injects_ask_data_agent(self):
        """Any agent with a bound DB KB gets ask_data_agent — no flag, no opt-out."""
        from app.services.data_source_runtime import prepare_data_source_runtime
        agent = _make_agent(self.db, self.kb_id)
        tools, prompt, extras = prepare_data_source_runtime(
            self.db, agent, base_tools=[], base_system_prompt="BASE",
        )
        names = self._names(tools)
        self.assertIn("ask_data_agent", names)
        # Granular tools are NOT auto-injected onto the calling agent
        for n in ("list_data_sources", "describe_schema", "execute_query", "answer_from_database"):
            self.assertNotIn(n, names)
        self.assertIn("Bound Data Sources", prompt)
        self.assertIn("E2E Sales", prompt)
        self.assertEqual(extras.get("bound_kb_ids"), [self.kb_id])

    def test_no_kb_is_noop(self):
        from app.services.data_source_runtime import prepare_data_source_runtime
        agent = _make_agent(self.db, self.kb_id)
        agent.knowledge_bases = []  # simulate agent with no bindings
        self.db.commit()
        base_tools = [{"function": {"name": "web_search"}}]
        tools, prompt, extras = prepare_data_source_runtime(
            self.db, agent, base_tools=base_tools, base_system_prompt="BASE",
        )
        self.assertEqual(tools, base_tools)
        self.assertEqual(prompt, "BASE")
        self.assertEqual(extras, {})

    def test_does_not_double_inject(self):
        """If ask_data_agent is already in base_tools, don't add it twice."""
        from app.services.data_source_runtime import prepare_data_source_runtime
        agent = _make_agent(self.db, self.kb_id)
        base_tools = [{
            "type": "function",
            "function": {"name": "ask_data_agent", "description": "pre-existing"},
        }]
        tools, _, _ = prepare_data_source_runtime(
            self.db, agent, base_tools=base_tools, base_system_prompt="BASE",
        )
        # Count ask_data_agent schemas
        count = sum(1 for t in tools if t["function"]["name"] == "ask_data_agent")
        self.assertEqual(count, 1)


class _AskDataAgentHandler(unittest.TestCase):
    def setUp(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        self.meta_fd, self.meta_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(self.meta_fd)
        self.engine = create_engine(f"sqlite:///{self.meta_path}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.path = _make_sqlite_db()
        _make_kb(self.db, self.path, "kb_e2e")
        self.kb_id = "kb_e2e"

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        for p in (self.path, self.meta_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_handler_with_mocked_llm(self):
        """Mock the LLM to emit describe_schema + execute_query, then assert
        the handler returns a structured payload with the expected rows."""
        from app.services.tool_handlers import db_tools  # ensure registration
        from app.services.tool_handlers import delegation_tools
        from app.services.tool_handlers.delegation_tools import _ask_data_agent

        # Two LLM turns: first emits execute_query, second emits final text.
        # (We skip describe_schema because the connector.list_tables + the
        # rows for SELECT region, SUM(amount) GROUP BY region is what the
        # test cares about.)
        first = {
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": (
                        '{"data_source_id": "kb_e2e", "sql": '
                        '"SELECT region, SUM(amount) AS total '
                        'FROM sales GROUP BY region ORDER BY region"}'
                    ),
                },
            }],
        }
        second = {
            "content": "EU total is 350 and US total is 675.",
            "tool_calls": [],
        }

        async def fake_chat(messages, tools):
            # First turn = execute_query; second turn = final text
            if any(m.get("role") == "tool" for m in messages):
                return second
            return first

        # Patch the module-level _call_llm in delegation_tools.
        with patch.object(delegation_tools, "_call_llm", new=fake_chat):
            result = asyncio.run(_ask_data_agent(
                args={"question": "What are the regional sales totals?"},
                db=self.db,
                user_id=None,
                context={"bound_kb_ids": [self.kb_id]},
            ))

        self.assertTrue(result["success"], result)
        self.assertEqual(result["answer"], "EU total is 350 and US total is 675.")
        self.assertEqual(result["source_id"], self.kb_id)
        self.assertEqual(result["source_name"], "E2E Sales")
        # The execute_query result should have populated rows + sql
        self.assertIsNotNone(result["rows"])
        self.assertEqual(len(result["rows"]), 2)
        by_region = {r["region"]: r["total"] for r in result["rows"]}
        self.assertAlmostEqual(by_region["EU"], 350.0)
        self.assertAlmostEqual(by_region["US"], 675.0)
        self.assertIn("SELECT", result["sql"])
        self.assertGreaterEqual(result["iterations"], 2)

    def test_handler_rejects_unbound_kb(self):
        from app.services.tool_handlers import db_tools  # registration
        from app.services.tool_handlers.delegation_tools import _ask_data_agent
        result = asyncio.run(_ask_data_agent(
            args={"question": "x", "data_source_id": "kb_other"},
            db=self.db,
            user_id=None,
            context={"bound_kb_ids": [self.kb_id]},
        ))
        self.assertFalse(result["success"])
        self.assertIn("not bound", result["error"])


class _MainAgentAnswersDBQuestion(unittest.TestCase):
    """Live agentic E2E: prove the main (user-facing) agent answers a real
    database question by delegating to the Data Agent subagent.

    Flow under test:
      1. User asks a question in a conversation on an agent that has a
         bound database KB.
      2. The chat runtime builds the system prompt + tool list via
         `prepare_data_source_runtime` — this MUST auto-inject
         `ask_data_agent` and the "Bound Data Sources" section.
      3. The main-agent LLM is mocked to emit ONE tool call: `ask_data_agent`
         with the user's question.
      4. The runtime executes `ask_data_agent`. Inside, the subagent LLM
         is mocked to emit `execute_query` then a final text reply.
      5. The main agent's final assistant message contains a prose
         answer with the data the user asked for.
    """

    def setUp(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        self.meta_fd, self.meta_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(self.meta_fd)
        self.engine = create_engine(f"sqlite:///{self.meta_path}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.path = _make_sqlite_db()
        _make_kb(self.db, self.path, "kb_e2e")
        self.kb_id = "kb_e2e"

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        for p in (self.path, self.meta_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_main_agent_uses_ask_data_agent_to_get_data(self):
        """End-to-end: the main agent sees `ask_data_agent` in its tool
        list, calls it, gets back a structured payload, and the user
        sees the answer in the final assistant content."""
        from app.services.tool_handlers import db_tools  # registration
        from app.services.tool_handlers import delegation_tools
        from app.services.data_source_runtime import prepare_data_source_runtime

        # ---- Arrange: create an AgentApp with a bound DB KB ----
        agent = _make_agent(self.db, self.kb_id)

        # ---- Arrange: build the runtime tool list + prompt ----
        base_tools = [
            # The main agent has its own non-DB tools available.
            {"type": "function", "function": {"name": "web_search",
             "description": "search", "parameters": {"type": "object", "properties": {}}}},
        ]
        base_prompt = "You are a helpful assistant."
        tools, system_prompt, ctx_extras = prepare_data_source_runtime(
            self.db, agent, base_tools=base_tools, base_system_prompt=base_prompt,
        )
        tool_names = {t["function"]["name"] for t in tools}
        self.assertIn("ask_data_agent", tool_names)
        # The granular DB tools are NOT on the main agent's tool list.
        for n in ("list_data_sources", "describe_schema", "execute_query", "answer_from_database"):
            self.assertNotIn(n, tool_names)
        # The system prompt tells the main agent how to use the subagent.
        self.assertIn("ask_data_agent", system_prompt)
        self.assertIn("Bound Data Sources", system_prompt)
        self.assertEqual(ctx_extras.get("bound_kb_ids"), [self.kb_id])

        # ---- Arrange: mock the main-agent LLM ----
        # It will emit a single `ask_data_agent` tool call, then on the
        # second turn, summarize the answer.
        main_agent_turn1 = {
            "content": "",
            "tool_calls": [{
                "id": "call_outer_1",
                "type": "function",
                "function": {
                    "name": "ask_data_agent",
                    "arguments": json.dumps({
                        "question": "What are the regional sales totals?",
                    }),
                },
            }],
        }
        main_agent_turn2 = {
            "content": "Regional sales totals: EU = 350, US = 675.",
            "tool_calls": [],
        }

        async def fake_main_llm(messages, tools, **_):
            # First turn: any tool call. Second turn: no tool call.
            if any(m.get("role") == "tool" for m in messages):
                return main_agent_turn2
            return main_agent_turn1

        # ---- Arrange: mock the subagent LLM (inside ask_data_agent) ----
        subagent_turn1 = {
            "content": "",
            "tool_calls": [{
                "id": "call_inner_1",
                "type": "function",
                "function": {
                    "name": "execute_query",
                    "arguments": json.dumps({
                        "data_source_id": self.kb_id,
                        "sql": "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region",
                    }),
                },
            }],
        }
        subagent_turn2 = {
            "content": "EU total is 350.0 and US total is 675.0.",
            "tool_calls": [],
        }

        async def fake_sub_llm(messages, tools):
            if any(m.get("role") == "tool" for m in messages):
                return subagent_turn2
            return subagent_turn1

        # ---- Act: simulate the chat runtime's tool-call loop ----
        # We replicate the relevant slice of `routers/agents.py:add_message`:
        # call LLM, execute any tool call, feed result back, repeat.
        from app.services.agent_tools import execute_tool

        async def run_loop():
            llm_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "What are the regional sales totals?"},
            ]
            final_content = ""
            for _ in range(5):
                llm_response = await fake_main_llm(llm_messages, tools)
                tool_calls = llm_response.get("tool_calls") or []
                if not tool_calls:
                    final_content = llm_response.get("content", "")
                    break
                llm_messages.append({
                    "role": "assistant",
                    "content": llm_response.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id", "x"),
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                })
                for tc in tool_calls:
                    tool_name = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"]) \
                        if isinstance(tc["function"]["arguments"], str) \
                        else tc["function"]["arguments"]
                    ctx = {
                        "conversation_id": "conv-e2e",
                        "agent_app_id": agent.id,
                        "agent_name": agent.name,
                        **ctx_extras,
                    }
                    result = await execute_tool(tool_name, args, self.db, None, context=ctx)
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "x"),
                        "content": json.dumps(result, default=str),
                    })
            return final_content, llm_messages

        with patch.object(delegation_tools, "_call_llm", new=fake_sub_llm):
            final_content, llm_messages = asyncio.run(run_loop())

        # ---- Assert: the user got the answer ----
        self.assertIn("350", final_content)
        self.assertIn("675", final_content)
        # The system prompt was visible to the main agent with the
        # "Bound Data Sources" section.
        system_msg = llm_messages[0]["content"]
        self.assertIn("ask_data_agent", system_msg)
        self.assertIn("Bound Data Sources", system_msg)
        # The LLM emitted exactly one `ask_data_agent` tool call.
        outer_calls = [
            tc for m in llm_messages
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        ]
        self.assertEqual(len(outer_calls), 1)
        self.assertEqual(outer_calls[0]["function"]["name"], "ask_data_agent")
        # The tool result was a structured payload from the subagent.
        tool_results = [m["content"] for m in llm_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_results), 1)
        result_payload = json.loads(tool_results[0])
        self.assertTrue(result_payload.get("success"))
        self.assertIn("answer", result_payload)
        # The subagent's answer contains the per-region numbers.
        self.assertIn("350", result_payload["answer"])
        self.assertIn("675", result_payload["answer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
