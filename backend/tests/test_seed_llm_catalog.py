"""Structural (AST) tests: the well-known LLM catalog must be seeded on
startup and from a standalone script so Kimi K2 / MiniMax M2 rows exist
without a manual step.

Red-first: these fail until ``main.py`` wires ``seed_llm_catalog`` into
the startup hook and ``scripts/seed_llm_catalog.py`` exists.
"""
import ast
import os

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_MAIN_PATH = os.path.join(_BACKEND_ROOT, "main.py")
_SCRIPT_PATH = os.path.join(_BACKEND_ROOT, "scripts", "seed_llm_catalog.py")


def _load_source(path: str) -> str:
    with open(path) as f:
        return f.read()


def _calls_helper(source: str, name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == name:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == name:
                return True
    return False


def _imports_helper(source: str, module: str, name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            if any(a.name == name for a in node.names):
                return True
    return False


def test_main_startup_seeds_llm_catalog():
    source = _load_source(_MAIN_PATH)
    assert _imports_helper(source, "app.services.llm_catalog", "seed_llm_catalog"), (
        "main.py must import seed_llm_catalog from app.services.llm_catalog"
    )
    assert _calls_helper(source, "seed_llm_catalog"), (
        "main.py startup must call seed_llm_catalog(db)"
    )


def test_seed_script_exists_and_calls_service():
    assert os.path.exists(_SCRIPT_PATH), (
        "scripts/seed_llm_catalog.py must exist for manual reseeding"
    )
    source = _load_source(_SCRIPT_PATH)
    assert _imports_helper(source, "app.services.llm_catalog", "seed_llm_catalog")
    assert _calls_helper(source, "seed_llm_catalog")
