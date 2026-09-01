"""Phase 3 — Template generation.

Verifies ``scripts/build_pptx_templates.py`` produces one deterministic .pptx
master template per vendored theme.  Templates are generated into a temp dir
(no writes to the (read-only) deploy tree) and asserted on count, naming, and
byte-determinism (re-running yields identical SHA-256 digests).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "build_pptx_templates.py"

_THEMES_DIR = _ROOT / "data" / "themes"


def _load_script():
    spec = importlib.util.spec_from_file_location("_build_pptx_templates", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _theme_names() -> list[str]:
    import json

    names = []
    for p in sorted(_THEMES_DIR.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        names.append(data.get("name", p.stem))
    return names


def test_generates_one_template_per_theme(tmp_path):
    mod = _load_script()
    out = tmp_path / "templates"
    mod.main(["--out-dir", str(out)])

    names = _theme_names()
    assert len(names) == 11, names

    produced = {p.name for p in out.glob("*.pptx")}

    # Each theme yields exactly one template. zhanlu-blue keeps its legacy
    # master name (zhanlu_default.pptx) for backward compatibility; all other
    # themes use theme_<name>.pptx.
    expected = set()
    for n in names:
        expected.add("zhanlu_default.pptx" if n == "zhanlu-blue" else f"theme_{n}.pptx")
    assert expected == produced, produced.symmetric_difference(expected)


def test_template_generation_is_deterministic(tmp_path):
    mod = _load_script()
    out1 = tmp_path / "a"
    out2 = tmp_path / "b"
    mod.main(["--out-dir", str(out1)])
    mod.main(["--out-dir", str(out2)])

    for p in out1.glob("*.pptx"):
        other = out2 / p.name
        assert other.exists(), other
        # Byte-identical across runs → stable SHA-256.
        import hashlib

        d1 = hashlib.sha256(p.read_bytes()).hexdigest()
        d2 = hashlib.sha256(other.read_bytes()).hexdigest()
        assert d1 == d2, f"{p.name} differs across runs"


def test_generated_template_opens_as_presentation(tmp_path):
    """Each produced .pptx must be a valid python-pptx Presentation."""
    from pptx import Presentation

    mod = _load_script()
    out = tmp_path / "templates"
    mod.main(["--out-dir", str(out)])
    for p in out.glob("theme_*.pptx"):
        prs = Presentation(str(p))  # raises if malformed
        assert prs.slide_width is not None
