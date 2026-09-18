"""Unit tests for ``llm_service._merge_schema_response``.

This is the single place where a schema-constrained LLM reply becomes the
documented ``{data, response, ...}`` result dict.  It exists because of a
real production defect:

``plan_dag`` asks for a TOP-LEVEL array schema (its plan is a JSON array of
steps).  The old inline parser only populated ``data`` when the provider
returned a JSON *object*, so for the array case it set ``response`` to the
parsed list and left ``data`` unset.  ``plan_dag`` then read
``data -> None``, fell into its fallback branch, and called ``.strip()`` on
that list -> ``AttributeError: 'list' object has no attribute 'strip'``.
The exception was swallowed by a broad ``except`` into "using default plan",
so every enabled LLM-planner turn silently planned with the generic default
(whose node fails at runtime) and the FSM re-planned in a loop, burning a
full LLM call per attempt and persisting nothing.

The contract enforced here:
  - ``data``  = parsed JSON whenever a schema was requested and the reply
    was valid JSON (object OR top-level array).
  - ``response`` = raw text on the plain-text path; the parsed value too
    for non-object JSON (backward compatibility).
  - a dict reply is additionally spread onto the result (legacy).
"""

from __future__ import annotations

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


_ARRAY_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"node_type": {"type": "string"}, "name": {"type": "string"}},
        "required": ["node_type", "name"],
    },
}

_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {"headline": {"type": "string"}, "slides": {"type": "array"}},
}


def _merge(choice, schema):
    from app.services.llm_service import _merge_schema_response

    result = {"model": "test-model", "usage": {}}
    return _merge_schema_response(result, choice, schema)


class TestArraySchema(unittest.TestCase):
    """The shape that broke plan_dag: a bare JSON array reply."""

    def test_array_reply_populates_data(self):
        steps = [{"node_type": "nl2sql", "name": "q"}, {"node_type": "agent", "name": "answer"}]
        import json

        out = _merge(json.dumps(steps), _ARRAY_SCHEMA)
        self.assertEqual(out["data"], steps)
        self.assertEqual(out["response"], steps)

    def test_array_reply_keeps_model_and_usage(self):
        out = _merge("[]", _ARRAY_SCHEMA)
        self.assertEqual(out["model"], "test-model")
        self.assertIn("usage", out)

    def test_empty_array_still_sets_data(self):
        """An empty list is a valid (if useless) plan, not a parse failure —
        it must reach ``data`` so the caller decides, rather than the parser
        silently dropping it."""
        out = _merge("[]", _ARRAY_SCHEMA)
        self.assertEqual(out["data"], [])
        self.assertEqual(out["response"], [])

    def test_fenced_array_is_not_parsed_but_survives_as_text(self):
        """Providers that ignore json mode wrap the array in a code fence.
        The parser must not crash; ``response`` carries the raw text so the
        caller can strip the fence itself."""
        fenced = '```json\n[{"node_type": "tool", "name": "x"}]\n```'
        out = _merge(fenced, _ARRAY_SCHEMA)
        self.assertNotIn("data", out)
        self.assertEqual(out["response"], fenced)


class TestArraySchemaEnvelope(unittest.TestCase):
    """The shape DeepSeek ACTUALLY returns for an array schema.

    ``response_format={"type": "json_object"}`` cannot express a bare array,
    and ``build_llm_payload`` injects the raw schema as a "match this
    schema" hint — so the model fills the schema template literally and
    echoes the envelope back, with the real steps inside ``items``.
    Verified live: ``{'type': 'array', 'items': [ {...steps...} ]}``.
    """

    def test_schema_echo_envelope_is_unwrapped(self):
        import json

        steps = [{"node_type": "nl2sql", "name": "q"}, {"node_type": "sandbox", "name": "r"}]
        envelope = {"type": "array", "items": steps}
        out = _merge(json.dumps(envelope), _ARRAY_SCHEMA)
        self.assertEqual(out["data"], steps)
        self.assertEqual(out["response"], steps)
        # the envelope keys are still spread onto the result (legacy)
        self.assertEqual(out["items"], steps)

    def test_single_key_wrapper_is_unwrapped(self):
        steps = [{"node_type": "nl2sql", "name": "q"}]
        out = _merge('{"steps": [{"node_type": "nl2sql", "name": "q"}]}', _ARRAY_SCHEMA)
        self.assertEqual(out["data"], steps)

    def test_semantic_plan_key_wrapper_is_unwrapped(self):
        """Live variant 2: the model keeps ``type`` but names the payload key
        ``plan`` — observed from DeepSeek after the ``items`` variant."""
        import json

        steps = [{"node_type": "nl2sql", "name": "fetch"}, {"node_type": "sandbox", "name": "render"}]
        out = _merge(json.dumps({"type": "array", "plan": steps}), _ARRAY_SCHEMA)
        self.assertEqual(out["data"], steps)

    def test_unknown_single_list_of_objects_key_is_unwrapped(self):
        import json

        steps = [{"node_type": "tool", "name": "lookup"}]
        out = _merge(json.dumps({"type": "array", "whatever": steps}), _ARRAY_SCHEMA)
        self.assertEqual(out["data"], steps)

    def test_two_list_of_objects_values_is_ambiguous(self):
        """Two candidate lists with no conventional key = genuinely
        ambiguous; do not guess."""
        out = _merge('{"a": [{"x": 1}], "b": [{"y": 2}]}', _ARRAY_SCHEMA)
        self.assertEqual(out["data"], {"a": [{"x": 1}], "b": [{"y": 2}]})

    def test_empty_envelope_array_is_unwrapped_to_empty_list(self):
        out = _merge('{"type": "array", "items": []}', _ARRAY_SCHEMA)
        self.assertEqual(out["data"], [])

    def test_envelope_without_items_list_is_not_unwrapped(self):
        """A dict that is not a recognisable array payload must stay a dict
        (``data`` = the parsed object) rather than being guessed at."""
        out = _merge('{"type": "array", "items": "oops"}', _ARRAY_SCHEMA)
        self.assertEqual(out["data"], {"type": "array", "items": "oops"})

    def test_multi_key_dict_is_not_unwrapped(self):
        """Only a pure envelope (keys ⊆ {type, items}) is unwrapped — a
        richer dict could carry legitimate metadata keys."""
        out = _merge('{"items": [1, 2], "total": 2}', _ARRAY_SCHEMA)
        self.assertEqual(out["data"], {"items": [1, 2], "total": 2})


class TestObjectSchema(unittest.TestCase):
    """Object-schema callers must be unaffected by the array fix."""

    def test_object_reply_sets_data_and_spreads_keys(self):
        out = _merge('{"headline": "H", "slides": [1, 2]}', _OBJECT_SCHEMA)
        self.assertEqual(out["data"], {"headline": "H", "slides": [1, 2]})
        # legacy: object keys are spread onto the result itself
        self.assertEqual(out["headline"], "H")
        self.assertEqual(out["slides"], [1, 2])

    def test_array_reply_under_object_schema_does_not_set_data(self):
        """A misbehaving provider returning an array for an object schema
        must NOT put a list in ``data``: callers across the codebase do
        ``(result.get("data") or {}).get(...)`` and would crash on a list.
        They get the old safe behavior (no ``data`` key) instead."""
        out = _merge('[{"node_type": "tool"}]', _OBJECT_SCHEMA)
        self.assertNotIn("data", out)
        self.assertEqual(out["response"], [{"node_type": "tool"}])

    def test_dict_schema_value_without_type_key_is_conservative(self):
        out = _merge("[1, 2]", {"properties": {}})
        self.assertNotIn("data", out)


class TestMalformed(unittest.TestCase):
    def test_invalid_json_falls_back_to_raw_text(self):
        out = _merge("I cannot help with that.", _ARRAY_SCHEMA)
        self.assertNotIn("data", out)
        self.assertEqual(out["response"], "I cannot help with that.")

    def test_non_string_choice_is_tolerated(self):
        """``json.loads(None)`` raises TypeError, not JSONDecodeError — the
        old code caught it, and this test pins that."""
        out = _merge(None, _ARRAY_SCHEMA)
        self.assertIsNone(out["response"])

    def test_dict_reply_sets_data_even_without_schema(self):
        """Legacy semantics, pinned deliberately: a JSON OBJECT reply sets
        ``data`` regardless of whether a schema was requested (the helper is
        only reached when one was, but the rule is schema-independent).  The
        schema gates ONLY the list case — that asymmetry is what makes the
        array fix safe for the ~16 object-schema callers."""
        out = _merge('{"a": 1}', None)
        self.assertEqual(out["data"], {"a": 1})
        self.assertEqual(out["a"], 1)

    def test_list_reply_without_array_schema_never_sets_data(self):
        out = _merge("[1, 2]", None)
        self.assertNotIn("data", out)
        self.assertEqual(out["response"], [1, 2])


if __name__ == "__main__":
    unittest.main()
