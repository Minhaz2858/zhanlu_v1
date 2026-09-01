"""P2: skill script execution in the sandbox — TDD tests.

Covers the pieces that let a Claude skill's bundled ``scripts/`` actually
run inside the existing Docker sandbox (progressive-disclosure Layer 3):

* ``prepare_input_package`` materializes a ``skill_bundle`` (SKILL.md +
  scripts/) into ``/input/skill_bundle/`` so the sandbox container can
  exec the skill's bundled entry point.
* ``get_skill_dir`` / ``list_skill_scripts`` discover a skill's folder +
  runnable scripts.
* ``_build_skill_bundle`` packages a skill's SKILL.md + scripts/ as base64
  (bounded — never bundles large font/asset trees).
* ``_run_skill_script`` enqueues a sandbox job carrying the bundle + a
  generic runner; unknown skills are rejected (never raw-exec'd).
"""
import base64
import os
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_ROOT = _THIS_DIR.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


# ---------------------------------------------------------------------------
# prepare_input_package — skill_bundle materialization
# ---------------------------------------------------------------------------

def test_prepare_input_package_writes_skill_bundle(tmp_path):
    from app.services.sandbox.container_manager import prepare_input_package
    inp = tmp_path / "input"
    skill_md = base64.b64encode(b"# Skill\nbody").decode()
    script = base64.b64encode(b"echo hi").decode()
    pkg = {
        "skill_config": {"entry_point": "scripts/run.sh"},
        "skill_bundle": [
            {"path": "SKILL.md", "data_base64": skill_md},
            {"path": "scripts/run.sh", "data_base64": script},
        ],
    }
    prepare_input_package(str(inp), pkg)
    assert (inp / "skill_bundle" / "SKILL.md").read_text() == "# Skill\nbody"
    assert (inp / "skill_bundle" / "scripts" / "run.sh").read_text() == "echo hi"
    # config.json still written (existing behavior preserved)
    assert (inp / "config.json").exists()


def test_prepare_input_package_without_skill_bundle_is_unchanged(tmp_path):
    from app.services.sandbox.container_manager import prepare_input_package
    inp = tmp_path / "input"
    prepare_input_package(str(inp), {"skill_config": {"x": 1}})
    # No skill_bundle key → no skill_bundle dir created (existing flow intact)
    assert (inp / "skill_bundle").exists() is False
    assert (inp / "config.json").exists()


# ---------------------------------------------------------------------------
# Shared fixture: a skills dir with a skill that has a scripts/ folder
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_skills_with_scripts(tmp_path):
    sk = tmp_path / "alpha"
    (sk / "scripts").mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\n# Alpha\n")
    (sk / "scripts" / "build.sh").write_text("echo build")
    (sk / "scripts" / "analyze.py").write_text("print('analyze')")
    # a large asset that must NOT be bundled
    (sk / "assets").mkdir()
    (sk / "assets" / "big.bin").write_bytes(b"\0" * 5000)
    return tmp_path


def _patch_registry(monkeypatch, skills_dir):
    import app.services.skills_loader as sl
    from app.services.skills_loader import SkillsRegistry
    reg = SkillsRegistry(skills_dir=str(skills_dir))
    reg.load()
    monkeypatch.setattr(sl, "get_skills_registry", lambda: reg)
    return reg


# ---------------------------------------------------------------------------
# get_skill_dir / list_skill_scripts — discovery
# ---------------------------------------------------------------------------

def test_get_skill_dir_resolves_absolute(tmp_skills_with_scripts, monkeypatch):
    from app.services.skills_loader import get_skill_dir
    _patch_registry(monkeypatch, tmp_skills_with_scripts)
    d = get_skill_dir("alpha")
    assert d is not None
    assert os.path.isabs(d)
    assert d.endswith("alpha")
    assert os.path.isfile(os.path.join(d, "SKILL.md"))


def test_get_skill_dir_unknown_skill_returns_none(tmp_skills_with_scripts, monkeypatch):
    from app.services.skills_loader import get_skill_dir
    _patch_registry(monkeypatch, tmp_skills_with_scripts)
    assert get_skill_dir("nope") is None


def test_list_skill_scripts_enumerates_scripts_dir(tmp_skills_with_scripts, monkeypatch):
    from app.services.skills_loader import list_skill_scripts
    _patch_registry(monkeypatch, tmp_skills_with_scripts)
    scripts = list_skill_scripts("alpha")
    names = {s["name"] for s in scripts}
    assert names == {"build.sh", "analyze.py"}
    for s in scripts:
        assert s["path"].startswith("scripts/")
        assert isinstance(s["size"], int) and s["size"] > 0


def test_list_skill_scripts_unknown_skill_returns_empty(tmp_skills_with_scripts, monkeypatch):
    from app.services.skills_loader import list_skill_scripts
    _patch_registry(monkeypatch, tmp_skills_with_scripts)
    assert list_skill_scripts("nope") == []


# ---------------------------------------------------------------------------
# _build_skill_bundle — package SKILL.md + scripts/ as base64 (bounded)
# ---------------------------------------------------------------------------

def test_build_skill_bundle_packages_skill_md_and_scripts(tmp_skills_with_scripts, monkeypatch):
    from app.services.skills_loader import get_skill_dir
    from app.services.tool_handlers.skills_tool import _build_skill_bundle
    _patch_registry(monkeypatch, tmp_skills_with_scripts)
    skill_dir = get_skill_dir("alpha")
    bundle = _build_skill_bundle(skill_dir)
    paths = {b["path"] for b in bundle}
    assert "SKILL.md" in paths
    assert "scripts/build.sh" in paths
    assert "scripts/analyze.py" in paths
    # data decodes correctly
    for b in bundle:
        assert base64.b64decode(b["data_base64"])
    # large assets must NOT be bundled (bounded to SKILL.md + scripts/)
    assert not any("assets" in b["path"] for b in bundle)


# ---------------------------------------------------------------------------
# _run_skill_script — enqueue logic (mocked SandboxService)
# ---------------------------------------------------------------------------

def test_run_skill_script_rejects_unknown_skill(tmp_skills_with_scripts, monkeypatch):
    from app.services.tool_handlers.skills_tool import _run_skill_script
    _patch_registry(monkeypatch, tmp_skills_with_scripts)
    res = _run_skill_script(db=None, name="nope", entry_point="scripts/x.sh")
    assert res["success"] is False
    assert "not found" in res["error"].lower()


def test_run_skill_script_enqueues_job_with_bundle_and_runner(tmp_skills_with_scripts, monkeypatch):
    from app.services.tool_handlers import skills_tool as st
    _patch_registry(monkeypatch, tmp_skills_with_scripts)

    created = {}

    class _J:
        id = "job-123"

    class FakeService:
        def __init__(self, db):
            pass

        def create_job(self, **kw):
            created["kw"] = kw
            return _J()

    monkeypatch.setattr(
        "app.services.sandbox.sandbox_service.SandboxService", FakeService)

    res = st._run_skill_script(
        db=None, name="alpha", entry_point="scripts/build.sh",
        args=["--flag"], inputs={"k": "v"}, image="zhanlu-sandbox-python:latest",
    )
    assert res["success"] is True
    assert res["job_id"] == "job-123"
    ip = created["kw"]["input_package"]
    assert ip["skill_config"]["entry_point"] == "scripts/build.sh"
    assert ip["skill_config"]["args"] == ["--flag"]
    assert ip["skill_config"]["inputs"] == {"k": "v"}
    assert ip["runner_script"]  # base64 runner present
    assert ip["skill_bundle"]    # bundle present
    assert any(b["path"] == "scripts/build.sh" for b in ip["skill_bundle"])
    assert created["kw"]["image_name"] == "zhanlu-sandbox-python:latest"


def test_run_skill_script_rejects_entry_point_outside_skill_bundle(tmp_skills_with_scripts, monkeypatch):
    """A path-traversal entry_point (e.g. ../../etc/passwd) must be rejected."""
    from app.services.tool_handlers.skills_tool import _run_skill_script
    _patch_registry(monkeypatch, tmp_skills_with_scripts)
    res = _run_skill_script(db=None, name="alpha", entry_point="../../etc/passwd")
    assert res["success"] is False
    assert "entry_point" in res["error"].lower() or "invalid" in res["error"].lower()
