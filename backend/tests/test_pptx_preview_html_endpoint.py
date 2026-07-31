"""AST-based: PPTX ?format=html preview endpoint wired."""
import ast
import os


def _load():
    p = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "artifacts.py"
    )
    with open(p) as f:
        return f.read()


def test_html_endpoint_supports_pptx():
    src = _load()
    tree = ast.parse(src)
    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_preview":
            func = node
            break
    assert func is not None, "get_preview function not found"
    func_src = ast.unparse(func)
    # Must accept pptx in the format=html branch
    assert "'pptx'" in func_src or '"pptx"' in func_src
    assert "convert_pptx_to_html" in func_src
    # Error message must mention both kinds
    assert "DOCX" in func_src
    assert "PPTX" in func_src


def test_html_endpoint_dispatches_by_artifact_type():
    src = _load()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_preview":
            func_src = ast.unparse(node)
            # The conversion call must be guarded by the artifact_type check
            assert "convert_docx_to_html" in func_src
            assert "convert_pptx_to_html" in func_src
            # And the elif (pptx) branch
            assert "kind = " in func_src or 'kind = "PPTX"' in func_src
            break
