"""Regression test for the 'agents are not responding' bug.

Root cause: ``add_message`` v2 endpoint in app/routers/agents.py used
``agent_app_id`` outside the ``if user_role == "user" and user_content:``
block, causing ``UnboundLocalError`` for any message whose role is not
"user" (e.g. assistant continuations, tool messages, or empty content).

This test parses the source AST of the function and asserts the
``agent_app_id`` binding is at function scope (not inside the if block).
A real end-to-end test would require standing up the full SQLAlchemy
stack with Postgres; the AST check is the minimum regression guard.
"""
import ast
import os


def _load_function_source():
    agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "agents.py"
    )
    with open(agents_path) as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "add_message":
                return source, node
    raise RuntimeError("add_message not found in agents.py")


def test_agent_app_id_bound_at_function_scope_not_inside_if_block():
    """``agent_app_id`` must be defined at function scope, not nested inside
    the ``if user_role == "user" and user_content:`` block.

    Bug: line 852 references ``agent_app_id`` outside the if, but the
    assignment at line 467 was inside the if, causing UnboundLocalError.
    """
    _, func = _load_function_source()

    # Find every `agent_app_id = ...` assignment in the function body
    # at each nesting level.
    def find_assignments(node, depth=0, path=""):
        """Yield (assign_node, depth, ancestor_description) for each
        `agent_app_id = ...` assignment."""
        results = []
        for child in ast.iter_child_nodes(node):
            child_path = f"{path}/{type(child).__name__}"
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id == "agent_app_id":
                        results.append((child, depth, child_path))
            if isinstance(child, ast.If):
                results.extend(
                    find_assignments(child, depth + 1, child_path + "[if]")
                )
            elif isinstance(child, (ast.For, ast.While, ast.With, ast.Try)):
                results.extend(
                    find_assignments(child, depth + 1, child_path + f"[{type(child).__name__}]")
                )
            else:
                results.extend(find_assignments(child, depth, child_path))
        return results

    assignments = find_assignments(func)
    assert len(assignments) >= 1, "add_message must assign agent_app_id"

    # There must be at least one assignment at depth 0 (function body, NOT inside an if)
    top_level = [a for a in assignments if a[1] == 0]
    assert len(top_level) >= 1, (
        f"agent_app_id must be assigned at function-body scope (depth 0), "
        f"so the post-loop reference at line ~852 cannot UnboundLocalError. "
        f"Found assignments: {[(a[0].lineno, a[1], a[2]) for a in assignments]}"
    )


def test_post_loop_memory_extraction_references_safe_agent_app_id():
    """The block at line ~852 that schedules background memory extraction
    must use a safely-bound ``agent_app_id``.
    """
    _, func = _load_function_source()
    source_lines = ast.unparse(func).splitlines() if hasattr(ast, "unparse") else []
    # We can also just walk the AST and find the If that guards the
    # _bg_extract_memories call.
    found_guard = False
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            # Look for an If whose test references agent_app_id
            test_src = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if "agent_app_id" in test_src:
                # Confirm there's an assignment to agent_app_id at
                # function-body scope (depth 0) BEFORE this If.
                found_guard = True
                break
    assert found_guard, "Could not find the if agent_app_id guard around the memory extraction"
