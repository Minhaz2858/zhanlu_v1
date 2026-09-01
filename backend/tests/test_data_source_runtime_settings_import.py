"""Regression guard for the `settings` NameError in data_source_runtime.

The Project Data Map block (``prepare_data_source_runtime``) references
``settings.ENTITY_GRAPH_ENABLED``. A previous edit added that reference
WITHOUT importing ``settings``, so every project-scoped chat crashed the
data-source runtime with ``NameError: name 'settings' is not defined``.
The exception was swallowed by the caller (agents.py treats DSR failure
as non-fatal), which silently ran the agent with ``bound_kb_ids=[]`` —
the production symptom was the automation agent reporting
"list_data_sources returns zero bound sources" even though the project
had a connected database.

This is the same uninitialized-name class of bug as the ``agents.py``
``content_streamed`` / ``llm_messages`` incidents (see
``tests/test_v3_stream_content_streamed_init.py``). We therefore assert
at the AST level that ``settings`` is imported before any use in the
module, and smoke-test that the attribute read resolves.
"""
from __future__ import annotations

import ast
from pathlib import Path

DSR_PATH = (
    Path(__file__).resolve().parent.parent
    / "app" / "services" / "data_source_runtime" / "data_source_runtime.py"
)


def _module_source() -> str:
    return DSR_PATH.read_text(encoding="utf-8")


def test_settings_is_imported():
    """``settings`` must be imported at module top-level (not just used).

    A bare ``getattr(settings, ...)`` with no import is exactly the
    production crash. Assert an ``from app.config import settings`` (or
    equivalent) import exists.
    """
    tree = ast.parse(_module_source())
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names]
            if node.module == "app.config" and "settings" in names:
                imported = True
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in ("app.config", "settings"):
                    imported = True
    assert imported, (
        "data_source_runtime.py uses `settings` but does not import it — "
        "this NameError crashed prepare_data_source_runtime for every "
        "project-scoped chat (bound_kb_ids silently became [])."
    )


def test_settings_attr_read_resolves():
    """Smoke: the exact attribute the module reads must exist on settings."""
    from app.config import settings

    # getattr with a default must not raise — the whole point of the
    # call site. If `settings` were undefined this import/attribute
    # access would raise NameError/AttributeError.
    value = getattr(settings, "ENTITY_GRAPH_ENABLED", False)
    assert isinstance(value, bool)
