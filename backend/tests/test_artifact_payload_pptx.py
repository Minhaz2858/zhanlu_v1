"""AST-based: PPTX artifact payload advertises preview_modes + outline.

Mirrors the existing test_artifact_payload_docx.py pattern.
"""
import ast
import os


def _load_artifacts_module():
    p = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "artifacts.py"
    )
    with open(p) as f:
        return f.read()


def test_artifacts_routes_imports_extract_pptx_outline():
    src = _load_artifacts_module()
    assert "extract_pptx_outline" in src
    assert "from app.services.artifacts.preview_builder import" in src


def test_artifacts_get_endpoint_handles_pptx_branch():
    """The artifact GET endpoint must have a PPTX branch that advertises
    preview_modes = ['self_hosted_html'] and a slide outline."""
    src = _load_artifacts_module()
    # Find the get_artifact (or whatever) function
    tree = ast.parse(src)
    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and (
            node.name == "get_artifact"
            or node.name == "get_artifact_v2"
            or "artifact" in node.name.lower()
        ):
            if any(
                "preview_modes" in ast.unparse(stmt) or "preview_outline" in ast.unparse(stmt)
                for stmt in ast.walk(node)
            ):
                func = node
                break
    assert func is not None, "No artifact endpoint function with preview_modes found"
    func_src = ast.unparse(func)
    assert "'pptx'" in func_src, "PPTX branch missing from artifact endpoint"
    assert "self_hosted_html" in func_src
    assert "extract_pptx_outline" in func_src
    assert "preview_outline" in func_src


def test_docx_branch_preserved():
    """The DOCX branch must still exist and not be removed by the refactor."""
    src = _load_artifacts_module()
    assert "'docx'" in src or '"docx"' in src
    assert "extract_docx_outline" in src
    assert "ms_word_open_url" in src
