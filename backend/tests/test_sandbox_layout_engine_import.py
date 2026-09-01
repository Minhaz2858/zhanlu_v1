"""Regression tests: ``layout_engine.py`` must work in BOTH contexts.

The sandbox runner (``app.services.sandbox.container_manager``) ships a
self-contained copy of ``layout_engine.py`` into ``/input/skill/``. Inside
the sandbox, ``/input/skill/`` is the only thing on ``sys.path`` — the
``app.*`` package tree isn't there. So any ``from app.*`` import at module
top of ``layout_engine.py`` MUST be wrapped in a ``try: from <sibling> ...
except Exception: from app...`` dual-context block (the convention the
project already uses at lines 577 and 611 for ``branded_charts``).

History:
- 2026-08-19 first bug: line 47 ``from app.services..._common import …``
  raised ``ModuleNotFoundError: No module named 'app'`` in the sandbox.
  Fix: extracted ``_chart_helpers`` (``eb67be2``).
- 2026-08-19 second bug: line 47 still pointed at ``app.services.…``,
  so the sandbox still raised ``ModuleNotFoundError`` even after the
  helper existed. Fix: wrap in try/except with sibling fallback.
- If anyone re-introduces a top-level ``from app.*`` without a sibling
  fallback, or re-targets at a non-sibling module, these tests fail.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
LAYOUT_ENGINE_SRC = (
    REPO_ROOT / "app" / "services" / "artifacts" / "layout_engine.py"
)
BRANDED_CHARTS_SRC = (
    REPO_ROOT / "app" / "services" / "artifacts" / "charts" / "branded_charts.py"
)
CHART_HELPERS_SRC = (
    REPO_ROOT / "app" / "services" / "artifacts" / "exporters" / "_chart_helpers.py"
)


def _iter_module_top_non_docstring_lines(src: Path):
    """Yield non-blank module-top lines (before the first ``def`` /
    ``class``), skipping the leading triple-quoted docstring so
    documentation text like ``from app.services...`` doesn't get matched."""
    text = src.read_text(encoding="utf-8")
    stripped_lead = text.lstrip()
    if stripped_lead.startswith(('"""', "'''")):
        quote = stripped_lead[:3]
        end = stripped_lead.find(quote, 3)
        if end != -1:
            text = stripped_lead[end + 3:]
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith(("def ", "class ")):
            break
        yield s


def test_layout_engine_no_bare_app_import_at_module_top():
    """Defence-in-depth: NO bare ``from app.*`` / ``import app.*`` outside a
    ``try/except`` block at module top. Every such import must be paired
    with a sibling fallback that the sandbox can resolve."""
    # Strip the module docstring first (it can contain example imports that
    # match the pattern but are just documentation).
    text = LAYOUT_ENGINE_SRC.read_text(encoding="utf-8")
    stripped_lead = text.lstrip()
    if stripped_lead.startswith(('"""', "'''")):
        quote = stripped_lead[:3]
        end = stripped_lead.find(quote, 3)
        if end != -1:
            text = stripped_lead[end + 3:]
    lines = text.splitlines()

    in_try = False
    bare: list[str] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if s.startswith(("def ", "class ")):
            break
        if s.startswith("try:"):
            in_try = True
            continue
        # ``except`` is part of the same try-block (the ``from app.*``
        # fallback lives inside it) — stay in the covered scope.
        if in_try and s.startswith("except"):
            continue
        if in_try:
            continue
        if s.startswith(("from app", "import app")):
            bare.append(s)
    assert not bare, (
        "layout_engine.py has a bare `from app.*` at module top outside "
        "any try/except — this breaks the sandbox vendoring path. Wrap it "
        "in `try: from <sibling> import … / except Exception: from app.…` "
        f"like the existing branded_charts blocks do. Found: {bare!r}"
    )


def test_layout_engine_chart_helper_block_is_dual_context():
    """The ``cap_chart_categories`` import must follow the sibling-first /
    app-fallback dual-context pattern used at lines 577 and 611 for
    ``style_chart``. Without this, the sandbox crashes with
    ``ModuleNotFoundError: No module named 'app'``."""
    text = LAYOUT_ENGINE_SRC.read_text(encoding="utf-8")
    lines = text.splitlines()
    # Find the block containing the cap_chart_categories import. Walk back
    # up to 3 lines to grab the leading ``try:`` if present.
    block_text = ""
    for idx, raw in enumerate(lines):
        if "cap_chart_categories" in raw:
            start = max(0, idx - 3)
            end = min(len(lines), idx + 4)
            block_text = "\n".join(l.strip() for l in lines[start:end])
            break
    assert "from _chart_helpers import cap_chart_categories" in block_text, (
        "cap_chart_categories must have a sibling-style fallback "
        "(`from _chart_helpers import …`) for the sandbox to find it. "
        f"Got block:\n{block_text}"
    )
    assert "from app.services.artifacts.exporters._chart_helpers import cap_chart_categories" in block_text, (
        "cap_chart_categories must keep an in-process dotted-path fallback "
        "for the backend container (where /input/skill/ is not on sys.path). "
        f"Got block:\n{block_text}"
    )


@pytest.fixture
def sandbox_env(tmp_path: Path):
    """Recreate the sandbox ``/input/skill/``: vendor ``layout_engine.py``,
    ``branded_charts.py``, ``_chart_helpers.py`` to a tmp dir. Strip every
    ``app.*`` from ``sys.modules`` and reduce ``sys.path`` to ONLY that
    tmp dir."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    for src in (LAYOUT_ENGINE_SRC, BRANDED_CHARTS_SRC, CHART_HELPERS_SRC):
        if not src.exists():
            pytest.skip(f"required module missing on host: {src}")
        shutil.copy(src, skill_dir / src.name)

    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)

    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)
    sys.path = [str(skill_dir)]

    try:
        yield skill_dir / "layout_engine.py"
    finally:
        sys.path = saved_path
        for name in list(sys.modules):
            if name not in saved_modules:
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def test_shipped_layout_engine_loads_in_sandbox_env(sandbox_env: Path):
    """The ACTUAL ``layout_engine.py`` (not a snippet) must load without
    raising ``ModuleNotFoundError: No module named 'app'`` when vendored
    alongside ``branded_charts.py`` and ``_chart_helpers.py``. This is
    the EXACT failure the user reported twice (2026-08-19).

    Note: ``python-pptx`` is required at runtime but is only installed
    in the sandbox container (not the test venv). Skip if missing; the
    static checks above catch the same regression class without needing
    the heavy dependency."""
    pytest.importorskip("pptx")

    spec = importlib.util.spec_from_file_location("layout_engine", sandbox_env)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # The regression check.
    assert hasattr(module, "render"), "layout_engine.render missing"
    assert hasattr(module, "cap_chart_categories"), (
        "cap_chart_categories not exposed at module level — the sandbox "
        "fallback didn't fire."
    )


def test_in_process_layout_engine_still_imports():
    """Sanity check that the in-process path (backend container, ``app.*``
    on sys.path) still works after the dual-context change."""
    from app.services.artifacts import layout_engine
    assert hasattr(layout_engine, "render")
    from app.services.artifacts.exporters import _chart_helpers
    assert layout_engine.cap_chart_categories is _chart_helpers.cap_chart_categories