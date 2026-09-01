"""Tests for the skill planner hook + manifest index."""

import os
import tempfile

import pytest

from app.services.skills_loader.manifest_index import (
    ManifestIndex,
    SkillManifest,
)
from app.services.skills_loader.skill_planner_hook import SkillPlannerHook


@pytest.fixture
def tmp_skills():
    """Create a tiny on-disk skills directory for the test."""
    with tempfile.TemporaryDirectory() as root:
        skill_a = os.path.join(root, "alpha")
        skill_b = os.path.join(root, "beta")
        os.makedirs(skill_a, exist_ok=True)
        os.makedirs(skill_b, exist_ok=True)

        with open(os.path.join(skill_a, "manifest.yaml"), "w") as f:
            f.write(
                "name: alpha\n"
                "description: The first test skill.\n"
                "version: 1.0\n"
                "tags: [file, test]\n"
            )
        with open(os.path.join(skill_a, "SKILL.md"), "w") as f:
            f.write("# Alpha\n\nThis skill does alpha things.\n")

        # Manifest with missing description → should be skipped.
        with open(os.path.join(skill_b, "manifest.yaml"), "w") as f:
            f.write("name: beta\n")

        yield root


def test_index_loads_valid_manifests(tmp_skills):
    idx = ManifestIndex(skills_dirs=[tmp_skills])
    idx.ensure_loaded()
    names = {m.name for m in idx.all()}
    assert "alpha" in names
    # "beta" has no description and must be skipped.
    assert "beta" not in names


def test_index_as_plan_prompt_is_bounded(tmp_skills):
    idx = ManifestIndex(skills_dirs=[tmp_skills])
    prompt = idx.as_plan_prompt()
    assert "alpha" in prompt
    assert "Available skills" in prompt


def test_index_search_finds_matching_skill(tmp_skills):
    idx = ManifestIndex(skills_dirs=[tmp_skills])
    results = idx.search("first test")
    assert any(r.name == "alpha" for r in results)


def test_hook_returns_load_skill_result(tmp_skills):
    idx = ManifestIndex(skills_dirs=[tmp_skills])
    hook = SkillPlannerHook(index=idx)
    out = hook.materialize_node({"type": "load_skill", "skill": "alpha"})
    assert out is not None
    assert out.name == "alpha"
    assert "alpha things" in out.body


def test_hook_returns_none_for_unknown_skill(tmp_skills):
    idx = ManifestIndex(skills_dirs=[tmp_skills])
    hook = SkillPlannerHook(index=idx)
    assert hook.materialize_node({"type": "load_skill", "skill": "nope"}) is None
    assert hook.materialize_node({"type": "create_artifact", "skill": "alpha"}) is None
    assert hook.materialize_node({"type": "load_skill"}) is None
    assert hook.materialize_node("not a dict") is None


def test_hook_plan_prompt_uses_index(tmp_skills, monkeypatch):
    idx = ManifestIndex(skills_dirs=[tmp_skills])
    hook = SkillPlannerHook(index=idx)
    # Force the index-fallback path: when the global SkillsRegistry is
    # unavailable, the hook must fall back to the injected ManifestIndex.
    # (Without this, a prior test may have already loaded the global
    # registry, which would be preferred and would not contain "alpha".)
    import app.services.skills_loader as sl

    def _unavailable():
        raise RuntimeError("registry forced unavailable for test")

    monkeypatch.setattr(sl, "get_skills_registry", _unavailable)
    extra = hook.build_plan_prompt_extra()
    assert "alpha" in extra


# ---------------------------------------------------------------------------
# P1: planner catalog curation — exclude bulk connector-doc collections
# (composio-skills) from the prompt injection. They stay SEARCHABLE on
# demand (unified_search / post_router_pick) but must not drown out the
# curated business skills in every planner prompt (progressive disclosure).
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_skills_mixed(tmp_path):
    """skills dir with a business skill, a composio connector doc, and a
    minimax skill, to verify the planner injection filters connector docs."""
    business = tmp_path / "alpha"
    business.mkdir()
    (business / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: A business skill.\n---\n# Alpha\n"
    )
    comp = tmp_path / "composio-skills" / "ahrefs-automation"
    comp.mkdir(parents=True)
    (comp / "SKILL.md").write_text(
        "---\nname: ahrefs-automation\ndescription: Connector doc.\n---\n# Ahrefs\n"
    )
    mini = tmp_path / "minimax_skills" / "minimax-music-gen"
    mini.mkdir(parents=True)
    (mini / "SKILL.md").write_text(
        "---\nname: minimax-music-gen\ndescription: Music gen.\n---\n# Music\n"
    )
    return tmp_path


def test_is_planner_curated_excludes_composio_keeps_business_and_minimax():
    """The planner-catalog filter excludes bulk composio connector docs
    (kept searchable on demand) but keeps business + minimax skills."""
    from app.services.skills_loader.skill_planner_hook import _is_planner_curated
    from app.services.skills_loader import SkillMetadata

    def mk(name, path):
        return SkillMetadata(name=name, description="", file_path=path)

    assert _is_planner_curated(
        mk("artifacts-builder", "skills/artifacts-builder/SKILL.md")) is True
    assert _is_planner_curated(
        mk("docx", "skills/document-skills/docx/SKILL.md")) is True
    assert _is_planner_curated(
        mk("minimax-music-gen", "skills/minimax_skills/minimax-music-gen/SKILL.md")) is True
    assert _is_planner_curated(
        mk("ahrefs-automation", "skills/composio-skills/ahrefs-automation/SKILL.md")) is False


def test_plan_prompt_injects_business_not_connector_docs(tmp_skills_mixed, monkeypatch):
    """build_plan_prompt_extra injects business + minimax skills but NOT
    composio connector docs (progressive disclosure: small curated catalog
    in the prompt, full catalog searchable on demand)."""
    import app.services.skills_loader as sl
    from app.services.skills_loader import SkillsRegistry

    reg = SkillsRegistry(skills_dir=str(tmp_skills_mixed))
    reg.load()
    monkeypatch.setattr(sl, "get_skills_registry", lambda: reg)

    hook = SkillPlannerHook()
    extra = hook.build_plan_prompt_extra()

    assert "alpha" in extra                  # business skill injected
    assert "minimax-music-gen" in extra      # minimax kept in planner catalog
    assert "ahrefs-automation" not in extra  # composio excluded from injection
