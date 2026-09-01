"""Behavioral tests for ``_detect_tool_call_loop`` — verify the loop
detection is smart enough to ignore legitimate multi-call patterns
(same tool, different arguments) while still catching true loops
(same tool, same arguments, repeated).

Bug: the original implementation counted every assistant ``tool_calls``
entry with the same ``function.name`` regardless of arguments. That
caused the agent_builder to trip the cap after 3 legitimate
``skills(action=search, query=...)`` calls with different queries
— the model never got a chance to investigate multiple angles and
had to fall back to high-level, shallow clarifying questions.

A true tool-call loop is the same tool + same arguments, repeated.
Different arguments = the model is exploring, not looping.
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _tc(name: str, args: dict) -> dict:
    """Build a tool_call entry in the OpenAI format used by
    ``llm_messages``."""
    return {
        "id": f"call_{name}_{hash(json.dumps(args, sort_keys=True))}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, sort_keys=True),
        },
    }


def _msg(role: str, content: str = "", tool_calls=None, tool_call_id: str = None) -> dict:
    out = {"role": role, "content": content}
    if tool_calls:
        out["tool_calls"] = tool_calls
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    return out


def test_no_loop_when_same_tool_different_args():
    """The classic agent_builder flow calls ``skills`` multiple times
    with different queries. None of these is a loop. The guard must
    not trip."""
    from app.routers.agents import _detect_tool_call_loop
    messages = [
        _msg("system", "You are agent_builder."),
        _msg("user", "Build me a research agent"),
        _msg("assistant", "", tool_calls=[_tc("skills", {"action": "search", "query": "web_search"})]),
        _msg("tool", json.dumps({"success": True}), tool_call_id="c1"),
        _msg("assistant", "", tool_calls=[_tc("skills", {"action": "search", "query": "summarize"})]),
        _msg("tool", json.dumps({"success": True}), tool_call_id="c2"),
        _msg("assistant", "", tool_calls=[_tc("skills", {"action": "list_categories"})]),
        _msg("tool", json.dumps({"success": True}), tool_call_id="c3"),
        _msg("assistant", "", tool_calls=[_tc("list_tools", {})]),
        _msg("tool", json.dumps({"success": True}), tool_call_id="c4"),
    ]
    assert _detect_tool_call_loop(messages) is None, (
        "Loop-guard tripped on a legitimate 4-call investigation: "
        "skills(search, q=web_search), skills(search, q=summarize), "
        "skills(list_categories), list_tools. These are all different "
        "queries, not a loop."
    )


def test_loop_when_same_tool_same_args_repeated():
    """True loop: same tool called with the same arguments, repeated.
    The guard must trip. Since these calls all FAILED, the success-aware
    guard (Phase B) grants one extra reformulation chance — failures trip
    at cap+1 = 11, not cap = 10."""
    from app.routers.agents import _detect_tool_call_loop
    # 11 repeated failed calls with the same args — should trip (cap+1).
    tcs = [_tc("skills", {"action": "load", "name": "agent-development"}) for _ in range(11)]
    messages = [_msg("system", "You are agent_builder."), _msg("user", "load it")]
    for i, tc in enumerate(tcs, 1):
        messages.append(_msg("assistant", "", tool_calls=[tc]))
        messages.append(_msg("tool", json.dumps({"success": False, "error": "not found"}), tool_call_id=f"c{i}"))
    info = _detect_tool_call_loop(messages)
    assert info is not None, "Guard should trip on 11x same-arg failed retry (cap+1)"
    name, count = info
    assert name == "skills"
    assert count >= 11


def test_no_loop_when_arg_order_differs_but_meaning_same():
    """The same effective arguments, just with different key order in
    the JSON string, must still be treated as a loop. JSON
    canonicalization is needed. These calls all FAILED, so the
    success-aware guard (Phase B) trips at cap+1 = 11."""
    from app.routers.agents import _detect_tool_call_loop
    messages = [
        _msg("user", "load foo"),
    ]
    args_variants = [
        '{"action": "load", "name": "foo"}',
        '{"name": "foo", "action": "load"}',
        '{"action": "load", "name": "foo"}',
        '{"name": "foo", "action": "load"}',
        '{"action": "load", "name": "foo"}',
        '{"name": "foo", "action": "load"}',
        '{"action": "load", "name": "foo"}',
        '{"name": "foo", "action": "load"}',
        '{"action": "load", "name": "foo"}',
        '{"name": "foo", "action": "load"}',
        '{"action": "load", "name": "foo"}',
    ]
    for i, args_str in enumerate(args_variants, 1):
        messages.append(_msg("assistant", "", tool_calls=[{
            "id": f"c{i}", "type": "function",
            "function": {"name": "skills", "arguments": args_str},
        }]))
        messages.append(_msg("tool", json.dumps({"success": False}), tool_call_id=f"c{i}"))
    info = _detect_tool_call_loop(messages)
    assert info is not None, (
        "Guard should trip on same effective args (different key order)."
    )
