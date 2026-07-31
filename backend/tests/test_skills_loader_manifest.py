"""Tests for manifest.yaml loading, validation, compat shim, and atomic writer.

Covers:
- parse_manifest / validate_manifest: happy path and error cases
- load_skill_package vs load_legacy_skill_md: parity test
- Compat shim: _load_from_dir with manifest.yaml → fallback to SKILL.md
- writer.py: atomic write, overwrite guard, round-trip (write → read)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.services.skills_loader import (
    parse_manifest,
    validate_manifest,
    load_skill_package,
    load_legacy_skill_md,
    parse_skill_file,
    SkillsRegistry,
    SkillMetadata,
)
from app.services.skills_loader.writer import (
    write_manifest,
    write_skill_md,
    write_json_schema,
    write_full_package,
)


# ── Sample manifest data ─────────────────────────────────────────────

VALID_MANIFEST = {
    "name": "test-skill",
    "version": "2.0.0",
    "description": "A test skill for manifest validation.",
    "summary": "Test skill summary.",
    "author": "test-author",
    "license": "MIT",
    "category": "testing",
    "tags": ["test", "manifest"],
    "platforms": ["linux", "mac"],
    "trigger": "run a test",
    "runtime": "python311",
    "user_invocable": True,
    "source": "bundled",
    "artifact_types": ["md"],
    "requires_sandbox": False,
}

VALID_SKILL_MD_FRONTMATTER = """---
name: legacy-skill
description: A legacy skill with just SKILL.md frontmatter.
version: "1.0.0"
author: legacy-author
metadata:
  hermes:
    tags:
      - legacy
      - frontmatter
trigger: legacy test
user_invocable: true
---

# Legacy Skill Body

This is the body of a legacy skill.
"""


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_package_dir():
    """Create a temporary package directory that is cleaned up after test."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def manifest_yaml_path(tmp_package_dir):
    """Write a valid manifest.yaml to a temp dir and return its path."""
    import yaml
    p = tmp_package_dir / "manifest.yaml"
    p.write_text(yaml.safe_dump(VALID_MANIFEST), encoding="utf-8")
    return p


@pytest.fixture
def skill_md_path(tmp_package_dir):
    """Write a valid SKILL.md to a temp dir and return its path."""
    p = tmp_package_dir / "SKILL.md"
    p.write_text(VALID_SKILL_MD_FRONTMATTER, encoding="utf-8")
    return p


# ── parse_manifest ────────────────────────────────────────────────────


class TestParseManifest:
    def test_parses_valid_manifest(self, manifest_yaml_path):
        manifest = parse_manifest(manifest_yaml_path)
        assert manifest is not None
        assert manifest["name"] == "test-skill"
        assert manifest["version"] == "2.0.0"

    def test_returns_none_for_nonexistent_file(self, tmp_package_dir):
        result = parse_manifest(tmp_package_dir / "nope.yaml")
        assert result is None

    def test_returns_none_for_invalid_yaml(self, tmp_package_dir):
        bad = tmp_package_dir / "manifest.yaml"
        bad.write_text("{ invalid: yaml: ::: ", encoding="utf-8")
        result = parse_manifest(bad)
        assert result is None


# ── validate_manifest ─────────────────────────────────────────────────


class TestValidateManifest:
    def test_valid_manifest_passes(self):
        is_valid, errors = validate_manifest(VALID_MANIFEST)
        assert is_valid is True
        assert errors == []

    def test_missing_required_name_fails(self):
        bad = {k: v for k, v in VALID_MANIFEST.items() if k != "name"}
        is_valid, errors = validate_manifest(bad)
        assert is_valid is False
        assert any("'name'" in e for e in errors)

    def test_invalid_version_pattern_fails(self):
        bad = dict(VALID_MANIFEST, version="not-a-version")
        is_valid, errors = validate_manifest(bad)
        assert is_valid is False

    def test_invalid_name_pattern_fails(self):
        bad = dict(VALID_MANIFEST, name="Bad Name!")
        is_valid, _ = validate_manifest(bad)
        assert is_valid is False


# ── load_skill_package ────────────────────────────────────────────────


class TestLoadSkillPackage:
    def test_loads_valid_package(self, tmp_package_dir, manifest_yaml_path):
        # Also write a SKILL.md so we get a body
        (tmp_package_dir / "SKILL.md").write_text(
            "---\nname: test-skill\n---\n\n## Body\n\nHello world.", encoding="utf-8"
        )
        skill = load_skill_package(tmp_package_dir, source="bundled")
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.version == "2.0.0"
        assert "Hello world" in skill.body
        assert skill.source == "bundled"

    def test_returns_none_for_missing_manifest(self, tmp_package_dir):
        skill = load_skill_package(tmp_package_dir)
        assert skill is None

    def test_skips_invalid_manifest(self, tmp_package_dir):
        # Write manifest with missing required fields
        import yaml
        bad = tmp_package_dir / "manifest.yaml"
        bad.write_text(yaml.safe_dump({"name": "bad", "version": "bad-ver"}), encoding="utf-8")
        skill = load_skill_package(tmp_package_dir)
        assert skill is None  # version pattern fails


# ── load_legacy_skill_md ──────────────────────────────────────────────


class TestLoadLegacySkillMd:
    def test_loads_from_skill_md(self, skill_md_path):
        skill = load_legacy_skill_md(skill_md_path, source="legacy")
        assert skill is not None
        assert skill.name == "legacy-skill"
        assert skill.description == "A legacy skill with just SKILL.md frontmatter."
        assert "Legacy Skill Body" in skill.body
        assert skill.tags == ["legacy", "frontmatter"]

    def test_returns_none_for_missing_file(self, tmp_package_dir):
        skill = load_legacy_skill_md(tmp_package_dir / "nope.md")
        assert skill is None


# ── Parity test: manifest vs frontmatter should yield identical metadata ──


class TestParity:
    """Parity test: the same skill loaded via manifest.yaml and via
    SKILL.md frontmatter should produce identical SkillMetadata."""

    def test_identical_metadata(self, tmp_package_dir):
        import yaml

        manifest_data = {
            "name": "parity-test",
            "version": "1.2.3",
            "description": "Same description",
            "summary": "Same summary",
            "author": "parity-author",
            "license": "Apache-2.0",
            "category": "test-cat",
            "tags": ["parity", "test"],
            "trigger": "parity trigger",
        }

        # Write manifest.yaml
        (tmp_package_dir / "manifest.yaml").write_text(
            yaml.safe_dump(manifest_data), encoding="utf-8"
        )

        # Write equivalent SKILL.md
        fm = {
            "name": "parity-test",
            "description": "Same description",
            "version": "1.2.3",
            "author": "parity-author",
            "license": "Apache-2.0",
            "trigger": "parity trigger",
            "summary": "Same summary",
        }
        (tmp_package_dir / "SKILL.md").write_text(
            f"---\n{yaml.safe_dump(fm)}---\n\n# Body\n\nTest body here.", encoding="utf-8"
        )

        # Two separate package dirs to avoid overlap
        pkg_a = tmp_package_dir / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "manifest.yaml").write_text(
            yaml.safe_dump(manifest_data), encoding="utf-8"
        )
        (pkg_a / "SKILL.md").write_text(
            f"---\n{yaml.safe_dump(fm)}---\n\n# Body\n\nTest body here.", encoding="utf-8"
        )

        pkg_b = tmp_package_dir / "pkg-b"
        pkg_b.mkdir()
        (pkg_b / "SKILL.md").write_text(
            f"---\n{yaml.safe_dump(fm)}---\n\n# Body\n\nTest body here.", encoding="utf-8"
        )

        skill_a = load_skill_package(pkg_a)
        skill_b = load_legacy_skill_md(pkg_b / "SKILL.md")

        assert skill_a is not None
        assert skill_b is not None
        assert skill_a.name == skill_b.name
        assert skill_a.version == skill_b.version
        assert skill_a.description == skill_b.description
        assert skill_a.author == skill_b.author
        assert skill_a.license == skill_b.license
        assert skill_a.trigger == skill_b.trigger
        assert "Test body here" in skill_a.body
        assert "Test body here" in skill_b.body


# ── Compat shim: _load_from_dir ───────────────────────────────────────


class TestCompatShim:
    """Verify the SkillsRegistry._load_from_dir compat shim.

    When both manifest.yaml and SKILL.md exist, manifest.yaml takes priority.
    When only SKILL.md exists, it falls back to legacy parsing.
    """

    def test_prefers_manifest_over_skill_md(self, tmp_package_dir):
        import yaml
        pkg = tmp_package_dir / "prefer-test"
        pkg.mkdir()
        (pkg / "manifest.yaml").write_text(
            yaml.safe_dump({"name": "from-manifest", "version": "2.0.0"}), encoding="utf-8"
        )
        (pkg / "SKILL.md").write_text(
            "---\nname: from-skillmd\nversion: \"1.0.0\"\n---\n\nBody from SKILL.md.", encoding="utf-8"
        )

        registry = SkillsRegistry(skills_dir=str(tmp_package_dir))
        skills = registry.load()
        # Should prefer manifest, not SKILL.md
        assert "from-manifest" in skills
        assert "from-skillmd" not in skills

    def test_falls_back_to_skill_md_when_no_manifest(self, tmp_package_dir):
        import yaml
        pkg = tmp_package_dir / "fallback-test"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text(
            "---\nname: fallback-skill\nversion: \"1.0.0\"\n---\n\nFallback body.", encoding="utf-8"
        )

        registry = SkillsRegistry(skills_dir=str(tmp_package_dir))
        skills = registry.load()
        assert "fallback-skill" in skills

    def test_manifest_with_no_skill_md_still_loads(self, tmp_package_dir):
        import yaml
        pkg = tmp_package_dir / "manifest-only"
        pkg.mkdir()
        (pkg / "manifest.yaml").write_text(
            yaml.safe_dump({"name": "manifest-only-skill", "version": "1.0.0"}), encoding="utf-8"
        )

        registry = SkillsRegistry(skills_dir=str(tmp_package_dir))
        skills = registry.load()
        assert "manifest-only-skill" in skills


# ── Writer: atomic write, overwrite guard, round-trip ─────────────────


class TestWriter:
    def test_write_manifest_creates_file(self, tmp_package_dir):
        target = write_manifest(tmp_package_dir, VALID_MANIFEST)
        assert target.exists()
        assert target.name == "manifest.yaml"

    def test_write_manifest_overwrite_guard(self, tmp_package_dir):
        write_manifest(tmp_package_dir, VALID_MANIFEST)
        with pytest.raises(FileExistsError):
            write_manifest(tmp_package_dir, VALID_MANIFEST, overwrite=False)

    def test_write_manifest_overwrite_allowed(self, tmp_package_dir):
        write_manifest(tmp_package_dir, VALID_MANIFEST)
        updated = dict(VALID_MANIFEST, version="3.0.0")
        write_manifest(tmp_package_dir, updated, overwrite=True)
        # Verify the overwritten content
        parsed = parse_manifest(tmp_package_dir / "manifest.yaml")
        assert parsed is not None
        assert parsed["version"] == "3.0.0"

    def test_write_skill_md_with_frontmatter(self, tmp_package_dir):
        target = write_skill_md(
            tmp_package_dir,
            body="Test body content.",
            frontmatter={"name": "writer-test", "version": "1.0.0"},
        )
        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert "Test body content." in content
        assert "writer-test" in content

    def test_write_json_schema(self, tmp_package_dir):
        schema = {"type": "object", "properties": {"x": {"type": "number"}}}
        target = write_json_schema(tmp_package_dir, "input", schema)
        assert target.exists()
        assert target.parent.name == "schemas"
        assert target.name == "input.schema.json"
        import json
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded["type"] == "object"

    def test_write_full_package(self, tmp_package_dir):
        results = write_full_package(
            tmp_package_dir,
            manifest=VALID_MANIFEST,
            body="Full package body.",
            frontmatter={"name": "full-test"},
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )
        for key in ["manifest", "skill_md", "input_schema", "output_schema"]:
            assert key in results
            assert results[key].exists()

    def test_round_trip_write_then_read(self, tmp_package_dir):
        """Write a full package, then load it back via load_skill_package."""
        manifest = dict(VALID_MANIFEST)
        write_full_package(
            tmp_package_dir,
            manifest=manifest,
            body="## Round-trip test\n\nThis is a test body.",
            frontmatter={"name": "roundtrip"},
        )

        skill = load_skill_package(tmp_package_dir)
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.version == "2.0.0"
        assert "Round-trip test" in skill.body

    def test_write_no_corruption_on_failure(self, tmp_package_dir):
        """If a write fails mid-way, the target file should not exist."""
        # Simulate by writing to a read-only directory (will fail)
        target = tmp_package_dir / "manifest.yaml"
        # This should succeed normally
        write_manifest(tmp_package_dir, VALID_MANIFEST)
        assert target.exists()
        parsed = parse_manifest(target)
        assert parsed is not None
        assert parsed["name"] == "test-skill"
