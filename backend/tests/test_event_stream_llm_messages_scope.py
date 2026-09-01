"""AST guard: ``event_stream`` must never REBIND ``llm_messages``.

Regression (2026-07-29): the Phase 1 reactive-compaction branch did
``llm_messages, was_compacted = await auto_compact_if_needed(...)`` inside
the nested ``event_stream`` async generator. Python's scoping rules make
any assignment to a name local to the whole function — ``llm_messages``
was previously a closure read from ``add_message_stream``, so the
assignment converted it to a local and every earlier read (starting at
``_turn_start_idx = max(0, len(llm_messages) - 1)``) raised
``UnboundLocalError``, killing every v3 SSE stream before the first token
(frontend: "Sorry, the connection was interrupted. Please try again.").

The fix mutates in place (``llm_messages[:] = compacted``) instead of
rebinding. This test pins the invariant: no rebinding of ``llm_messages``
inside ``event_stream`` (in-place subscript mutation is allowed).
"""
import ast
import os

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AGENTS_PY = os.path.join(_BACKEND_ROOT, "app", "routers", "agents.py")


def _target_names(target, out):
    """Collect names REBOUND by an assignment target. Subscript/Attribute
    targets (``x[:] = ...``, ``x.y = ...``) are mutations, not rebinding."""
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _target_names(elt, out)
    elif isinstance(target, ast.Starred):
        _target_names(target.value, out)


def _assigned_names(node):
    """Names bound by assignment statements directly inside ``node``'s body
    (not descending into nested function/class scopes)."""
    names = set()

    def visit(n):
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue  # nested scope — not our bindings
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    _target_names(t, names)
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
                _target_names(child.target, names)
            elif isinstance(child, ast.NamedExpr):
                _target_names(child.target, names)
            visit(child)

    visit(node)
    return names


def test_event_stream_does_not_rebind_llm_messages():
    tree = ast.parse(open(AGENTS_PY, encoding="utf-8").read())
    event_streams = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "event_stream"
    ]
    assert event_streams, "event_stream not found in agents.py"
    for fn in event_streams:
        bound = _assigned_names(fn)
        assert "llm_messages" not in bound, (
            f"event_stream (line {fn.lineno}) rebinds llm_messages — this "
            "makes it function-local and breaks closure reads above the "
            "assignment (UnboundLocalError). Mutate in place instead: "
            "llm_messages[:] = compacted"
        )
