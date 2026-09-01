"""Tests for the thinking-mode disable knob + main-loop reasoning passback.

Background: qwen3.6-27b (Qwen3 family) runs with thinking ON by default in
vLLM (``--reasoning-parser qwen3``). Its tool loops died after the first tool
result because the main loop rebuilt the assistant tool-call message WITHOUT
the model's real ``reasoning_content`` — vLLM rejects the next request (the
same reasoning-passback 400 class as DeepSeek thinking mode). Two fixes:

1. ``LLM_DISABLE_THINKING_MODELS`` -> the chat/completions payload gains
   ``chat_template_kwargs: {"enable_thinking": false}`` per request (vLLM >=
   0.9.0 with the qwen3 parser supports the override), so the model behaves
   like a plain chat model.
2. The v3 tool-batch append echoes ``final_reasoning`` into the assistant
   tool-call message via ``_build_tool_call_assistant_message``.
"""
import asyncio
import ast
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_ROOT = _THIS_DIR.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def _fake_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _endpoint(model_id="qwen3.6-27b", base_url="http://10.10.10.46:8000/v1", provider="vllm"):
    from app.services.llm_router import LLMEndpoint
    return LLMEndpoint(
        base_url=base_url,
        api_key="EMPTY",
        model_id=model_id,
        provider=provider,
    )


class TestModelShouldDisableThinking:
    def test_matches_qwen3_6_27b(self):
        from app.services.llm_service import model_should_disable_thinking
        assert model_should_disable_thinking("qwen3.6-27b") is True
        # substring match also covers suffixed/namespaced ids
        assert model_should_disable_thinking("qwen3.6-27b-awq4") is True

    def test_does_not_match_other_models(self):
        from app.services.llm_service import model_should_disable_thinking
        assert model_should_disable_thinking("deepseek-chat") is False
        assert model_should_disable_thinking("deepseek-v4-flash") is False
        assert model_should_disable_thinking("gpt-4o") is False
        assert model_should_disable_thinking("") is False

    def test_maybe_thinking_disable_kwargs(self):
        from app.services.llm_service import maybe_thinking_disable_kwargs
        assert maybe_thinking_disable_kwargs("qwen3.6-27b") == {
            "chat_template_kwargs": {"enable_thinking": False}
        }
        assert maybe_thinking_disable_kwargs("deepseek-chat") == {}


class TestToolCallAssistantMessageBuilder:
    def test_echoes_reasoning_content(self):
        from app.routers.agents import _build_tool_call_assistant_message
        msg = _build_tool_call_assistant_message(
            tool_call_id="tc_1",
            tool_name="ask_data_agent",
            args_str='{"question": "sales last month"}',
            reasoning_content="I need monthly sales totals",
        )
        assert msg["role"] == "assistant"
        assert msg["content"] is None
        assert msg["reasoning_content"] == "I need monthly sales totals"
        assert msg["tool_calls"] == [{
            "id": "tc_1",
            "type": "function",
            "function": {
                "name": "ask_data_agent",
                "arguments": '{"question": "sales last month"}',
            },
        }]

    def test_defaults_to_empty_reasoning(self):
        from app.routers.agents import _build_tool_call_assistant_message
        msg = _build_tool_call_assistant_message("tc_1", "ask_data_agent", "{}")
        assert msg["reasoning_content"] == ""


class TestPayloadInjection:
    """_call_llm_with_tools must inject chat_template_kwargs for qwen3.6-27b."""

    def test_call_llm_with_tools_injects_thinking_disable(self):
        from app.routers.agents import _call_llm_with_tools
        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["json"] = json
            return _fake_response({
                "choices": [{"message": {"content": "hi", "tool_calls": []}}],
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            asyncio.run(
                _call_llm_with_tools(
                    [{"role": "user", "content": "hello"}],
                    None,
                    endpoint=_endpoint(),
                )
            )

        assert captured["json"]["model"] == "qwen3.6-27b"
        assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_call_llm_with_tools_omits_kwargs_for_other_models(self):
        from app.routers.agents import _call_llm_with_tools
        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["json"] = json
            return _fake_response({
                "choices": [{"message": {"content": "hi", "tool_calls": []}}],
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            asyncio.run(
                _call_llm_with_tools(
                    [{"role": "user", "content": "hello"}],
                    None,
                    endpoint=_endpoint(
                        "deepseek-chat",
                        "https://api.deepseek.com/v1",
                        "deepseek",
                    ),
                )
            )

        assert captured["json"]["model"] == "deepseek-chat"
        assert "chat_template_kwargs" not in captured["json"]


class TestAssistantMessageEchoGuards:
    """Merged AST guard over ALL assistant tool-call append sites in agents.py.

    Loop variants (confirmed by source inspection):
      - add_message                  -> v2 chat loop
      - resume_conversation          -> resume/approval loop
      - add_message_stream ->
          event_stream -> _run_tool_batch   -> v3 streaming loop

    There is NO "v2.7" variant. The ONLY live echo of a model-produced tool
    call is the v3 ``_run_tool_batch`` site, which must route through
    ``_build_tool_call_assistant_message`` with the real ``final_reasoning``
    (falling back to ``""``). Every other assistant tool-call append is
    synthetic (data-contract/approval guard injection) or a history rebuild,
    so reasoning passback does not apply there.
    """

    LOOP_VARIANT_NAMES = {"add_message", "resume_conversation", "add_message_stream"}

    @staticmethod
    def _enclosing_funcs(node, tree):
        found = []

        class _V(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, n):
                self.stack.append(n.name)
                self.generic_visit(n)
                self.stack.pop()

            def visit_AsyncFunctionDef(self, n):
                self.stack.append(n.name)
                self.generic_visit(n)
                self.stack.pop()

            def visit_Call(self, n):
                if n is node:
                    found.extend(self.stack)
                self.generic_visit(n)

        _V().visit(tree)
        return found

    @classmethod
    def _sites(cls):
        """Return (helper_call_sites, inline_assistant_tool_call_appends)."""
        src = (_BACKEND_ROOT / "app" / "routers" / "agents.py").read_text()
        tree = ast.parse(src)
        helper_sites, append_sites = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "_build_tool_call_assistant_message"
            ):
                helper_sites.append(
                    (node.lineno, cls._enclosing_funcs(node, tree), ast.unparse(node))
                )
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "append":
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Dict):
                    keys = set()
                    for k in arg.keys:
                        try:
                            keys.add(ast.unparse(k).strip("'\""))
                        except Exception:
                            keys.add("*")
                    if "role" in keys and "tool_calls" in keys:
                        append_sites.append((node.lineno, cls._enclosing_funcs(node, tree)))
        return helper_sites, append_sites

    @staticmethod
    def _live_echo_ok(helper_sites):
        """True iff the live (v3-loop) helper call echoes final_reasoning with a
        ``or ""`` fallback. This is the check the next two tests exercise."""
        live = [
            unparsed
            for _, funcs, unparsed in helper_sites
            if any(f in ("event_stream", "_run_tool_batch") for f in funcs)
        ]
        if not live:
            return False
        return all(
            "final_reasoning" in u
            and (("final_reasoning or ''" in u) or ('final_reasoning or ""' in u))
            for u in live
        )

    def test_loop_variant_inventory_confirms_no_v27(self):
        """Only the v2/resume/v3 loops append assistant tool-call messages —
        there is no v2.7 variant."""
        _, append_sites = self._sites()
        tops = {funcs[0] for _, funcs in append_sites}
        assert tops == self.LOOP_VARIANT_NAMES, f"unexpected loop variants: {tops}"
        assert not any(re.search(r"v2\.?7", name) for name in tops)

    def test_live_echo_uses_helper_with_reasoning(self):
        """v3's live tool-call echo must use the helper + final_reasoning."""
        helper_sites, _ = self._sites()
        assert helper_sites, "agents.py must use _build_tool_call_assistant_message"
        assert self._live_echo_ok(helper_sites), (
            "the v3 live echo must pass final_reasoning with an empty fallback"
        )

    def test_guard_detects_dropped_reasoning_echo(self):
        """Self-validation: the guard above must FAIL when reasoning is dropped."""
        helper_sites, _ = self._sites()
        assert helper_sites
        assert self._live_echo_ok(helper_sites)  # sanity: guard passes on real code
        mutated = [
            (line, funcs, u.replace("final_reasoning or ''", "''").replace('final_reasoning or ""', '""'))
            for line, funcs, u in helper_sites
        ]
        assert not self._live_echo_ok(mutated), (
            "guard must detect a regression that drops the reasoning echo"
        )
