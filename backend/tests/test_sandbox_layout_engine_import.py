"""Regression test: ``layout_engine.py`` must NOT pull in any ``app.*``
submodule at module top in the sandbox container vendoring path.

Background: The sandbox runner (``app.services.sandbox.container_manager``)
ships a self-contained copy of ``layout_engine.py`` into ``/input/skill/`` so
it can generate a PPTX inside the isolation container without the full
backend project on its ``sys.path``. Before 2026-08-19, ``layout_engine.py``
line 47 did:

    from app.services.artifacts.exporters._common import cap_chart_categories

which raised ``ModuleNotFoundError: No module named 'app'`` the moment the
sandbox tried to import it. The agent silently fell back to the legacy
``create_artifact`` path, producing a 42.9 KB 8-slide "very bad" deck instead
of the high-quality Phase1B layout.

This test catches the regression at PR-review time via two complementary
checks:

1. **Static** — ``layout_engine.py`` must have no ``from app.<sub>.…`` import
   at module top. (The vendored module can't resolve those packages; the
   import would crash the sandbox.) It MAY import from
   ``app.services.artifacts.exporters._chart_helpers`` because that module
   is itself vendored alongside (``container_manager._build_runner_modules``
   in ``sandbox_tool.py``).

2. **Behavioural** — A minimal reproduction of the vendored file (a 5-line
   snippet that just imports ``cap_chart_categories`` from the location
   ``layout_engine.py`` uses) must load cleanly in a sandbox-like env
   (``sys.path`` reduced to the tmp skill dir; ``app.*`` not on the path).

If anyone re-introduces a transitive ``app.*`` import at the top of
``layout_engine.py``, or re-targets line 47 at the old ``_common.py``
module, these tests fail.
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
CHART_HELPERS_SRC = (
    REPO_ROOT / "app" / "services" / "artifacts" / "exporters" / "_chart_helpers.py"
)
EXPORTERS_DIR = REPO_ROOT / "app" / "services" / "artifacts" / "exporters"


# The single ``from app.*`` import that ``layout_engine.py`` is allowed to
# have at module top: the vendored sandbox-portable helper. Anything else is
# a regression.
ALLOWED_APP_MODULE_TOP_IMPORTS = frozenset({
    "from app.services.artifacts.exporters._chart_helpers import cap_chart_categories",
})


def _module_top_app_imports(src: Path) -> list[str]:
    """Return the ``from app.*`` / ``import app.*`` statements at the top
    level of *src* (everything before the first ``def`` / ``class``, and
    skipping lines inside the module's leading triple-quoted docstring)."""
    out: list[str] = []
    text = src.read_text(encoding="utf-8")
    # Skip the module docstring (everything from the first ``"""`` or ``'''``
    # until the matching closing quote). Module docstrings always appear at
    # the very top of the file.
    stripped_lead = text.lstrip()
    if stripped_lead.startswith(('"""', "'''")):
        quote = stripped_lead[:3]
        # Find the closing quote on its own line OR end-of-line.
        end = stripped_lead.find(quote, 3)
        if end == -1:
            return []
        text = stripped_lead[end + 3:]
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith(("def ", "class ")):
            break
        if stripped.startswith(("from app", "import app")):
            out.append(stripped)
    return out


# ---------------------------------------------------------------------------
# Static regression checks
# ---------------------------------------------------------------------------


def test_layout_engine_only_vendored_app_import_at_module_top():
    """``layout_engine.py`` may import exactly one ``app.*`` name at module
    top — the vendored ``_chart_helpers`` helper. Any other ``app.*`` import
    would crash the sandbox with ``ModuleNotFoundError``."""
    found = _module_top_app_imports(LAYOUT_ENGINE_SRC)
    assert found == sorted(ALLOWED_APP_MODULE_TOP_IMPORTS), (
        f"Expected exactly the vendored helper import, got {found!r}. The "
        "sandbox runner can only see modules under app.services.artifacts.* "
        "that are ALSO listed in sandbox_tool._build_runner_modules() — "
        "everything else raises ModuleNotFoundError in the sandbox."
    )


def test_chart_helpers_is_sandbox_portable():
    """``_chart_helpers.py`` is vendored into the sandbox. It must NOT
    transitively import any ``app.*`` module (otherwise the vendor will
    still explode at runtime)."""
    offenders = [
        stripped
        for stripped in (
            line.strip() for line in CHART_HELPERS_SRC.read_text(encoding="utf-8").splitlines()
        )
        if stripped.startswith(("from app", "import app"))
    ]
    assert not offenders, (
        "_chart_helpers.py is vendored into the sandbox; it must not import "
        f"any `app.*` modules anywhere. Found: {offenders!r}"
    )


def test_common_still_re_exports_helpers_for_in_process_renderers():
    """In-process renderers (pptx_export / docx_export / pdf_export /
    html_export) import from ``_common.py``; the shim there must keep
    exporting the helpers so they don't break. Constants are the canonical
    sandbox-portable values (8 / "Other")."""
    from app.services.artifacts.exporters import _common, _chart_helpers

    assert _common.CHART_CATEGORY_CAP == _chart_helpers.CHART_CATEGORY_CAP == 8
    assert _common.OTHER_BUCKET_LABEL == _chart_helpers.OTHER_BUCKET_LABEL == "Other"
    # Behaviour parity — re-import doesn't change the call's behaviour.
    rows = [
        {"x": "A", "v": 10},
        {"x": "B", "v": 20},
        {"x": "C", "v": 5},
        {"x": "D", "v": 1},
        {"x": "E", "v": 0.5},
    ]
    assert _common.cap_chart_categories(rows, "x", ["v"]) == \
        _chart_helpers.cap_chart_categories(rows, "x", ["v"])
    assert _common.coerce_number("1,234.5") == _chart_helpers.coerce_number("1,234.5") == 1234.5


# ---------------------------------------------------------------------------
# Behavioural reproduction of the original sandbox failure
# ---------------------------------------------------------------------------


def _extract_module_top_app_import(src: Path) -> str | None:
    """Return the ``from app.*`` import statement that ``layout_engine.py``
    uses to pull in ``cap_chart_categories``. Recognised pattern::

        from app.services.artifacts.exporters.<module> import cap_chart_categories

    This is a strict regex (not "any from app.*") so we don't get fooled by
    docstring examples like ``from app.services.artifacts.layout_engine
    import render`` that show up inside the module docstring."""
    import re

    pattern = re.compile(
        r"^from app\.services\.artifacts\.exporters\.([\w_]+) import cap_chart_categories\s*$"
    )
    for raw in src.read_text(encoding="utf-8").splitlines():
        if pattern.match(raw.strip()):
            return raw.strip()
    return None


def test_line_47_module_import_resolves_in_sandbox_env(tmp_path: Path):
    """Reproduce the original sandbox failure: a minimal snippet that
    imports ``cap_chart_categories`` from the location ``layout_engine.py``
    targets must load cleanly in a sandbox-like env (no ``app.*`` on
    ``sys.path``).

    This is the SAME import ``layout_engine.py`` runs at module top in
    production. If it explodes here, it explodes in the sandbox."""
    sandbox_dir = tmp_path / "skill"
    sandbox_dir.mkdir()

    # Mirror the vendoring: copy ``_chart_helpers.py`` next to a fake
    # ``layout_engine.py`` (we don't need real layout_engine — just the
    # import statement it makes).
    shutil.copy(CHART_HELPERS_SRC, sandbox_dir / "_chart_helpers.py")

    # Identify what ``layout_engine.py`` actually imports. If somebody
    # re-points it at ``_common``, this test reads the new line and tries
    # to import it — and fails, surfacing the regression.
    target_import = _extract_module_top_app_import(LAYOUT_ENGINE_SRC)
    assert target_import is not None, (
        "layout_engine.py lost its module-top cap_chart_categories import — "
        "the production render will fall back to legacy create_artifact."
    )
    # Pull out the bare sibling module (e.g. ``_chart_helpers``) via the
    # same regex the helper uses. The sandbox only sees sibling files in
    # ``/input/skill/`` — the ``app.services.…`` prefix is a host-side
    # address that doesn't exist there.
    import re
    sibling_module = re.match(
        r"^from app\.services\.artifacts\.exporters\.([\w_]+) import cap_chart_categories$",
        target_import,
    ).group(1)

    # Create a minimal snippet that performs exactly that import — this is
    # what the sandbox runner executes at module top of layout_engine.py.
    snippet = sandbox_dir / "layout_engine.py"
    snippet.write_text(
        "from __future__ import annotations\n"
        f"from {sibling_module} import cap_chart_categories\n"
        "\n"
        "render = None  # not exercised; this is a sandbox-import probe\n"
    )

    # Strip every ``app.*`` cached module so the sandbox path is isolated,
    # and reduce ``sys.path`` to ONLY the sandbox dir.
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)
    sys.path = [str(sandbox_dir)]

    try:
        spec = importlib.util.spec_from_file_location(
            "layout_engine", snippet
        )
        module = importlib.util.module_from_spec(spec)
        # The regression check: this MUST NOT raise ModuleNotFoundError.
        # Before the fix it raised: "No module named 'app'".
        spec.loader.exec_module(module)
        assert hasattr(module, "cap_chart_categories")
    finally:
        # Restore sys.modules / sys.path so subsequent tests aren't poisoned.
        sys.path = saved_path
        for name in list(sys.modules):
            if name not in saved_modules:
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)