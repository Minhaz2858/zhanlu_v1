"""Tests for Kimi-style folder-package write/read of skills.

Verifies that:
1. ``write_skill_md`` writes SKILL.md plus references/ and assets/ folders.
2. ``load_skill_package`` discovers the references and assets manifests.
3. The writer atomic helpers (write_reference / write_asset) create files under
   the expected sub-paths.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_folder.db")

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.skill_sync import write_skill_md
from app.services.skills_loader import load_skill_package


@pytest.fixture
def skill_dir():
    name = "test-folder-pkg"
    category = "custom"
    body = "# Test Folder Package\n\nA skill used by the folder-package test.\n"
    references = {
        "output-formats.md": "# Output Formats\n\nDOCX/PDF/PPTX guidance.",
        "report-structures.md": "# Structures\n\nExec vs ops layouts.",
    }
    assets = {
        "templates/report.docx": b"PK\x03\x04 fake docx bytes",
        "templates/report.pdf": b"%PDF-1.4 fake pdf bytes",
    }
    with patch(
        "app.services.skill_dry_run.trigger_dry_run_after_save",
        return_value=None,
    ):
        md_path = write_skill_md(
            name=name,
            description="A test folder-package skill",
            body=body,
            category=category,
            references=references,
            assets=assets,
        )
    root = Path(md_path).parent
    yield root
    # Cleanup
    import shutil

    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def test_write_creates_skill_md_and_folders(skill_dir):
    assert (skill_dir / "SKILL.md").exists()
    assert (skill_dir / "references" / "output-formats.md").exists()
    assert (skill_dir / "references" / "report-structures.md").exists()
    assert (skill_dir / "assets" / "templates" / "report.docx").exists()
    assert (skill_dir / "assets" / "templates" / "report.pdf").exists()


def test_reference_content_preserved(skill_dir):
    content = (skill_dir / "references" / "output-formats.md").read_text(encoding="utf-8")
    assert "DOCX/PDF/PPTX" in content


def test_asset_bytes_preserved(skill_dir):
    data = (skill_dir / "assets" / "templates" / "report.pdf").read_bytes()
    assert data.startswith(b"%PDF")


def test_load_skill_package_discovers_resources(skill_dir):
    # load_skill_package requires a manifest.yaml; write a minimal valid one so
    # the folder scan can pick up references/assets (matches the Kimi layout).
    manifest = (
        "name: test-folder-pkg\n"
        "description: A test folder-package skill\n"
        "version: 1.0.0\n"
        "category: custom\n"
        "references_manifest:\n"
        "  output-formats.md: DOCX/PDF/PPTX guidance\n"
        "  report-structures.md: Exec vs ops layouts\n"
        "assets_manifest:\n"
        "  templates/report.docx: Branded docx template\n"
        "  templates/report.pdf: Branded pdf template\n"
    )
    (skill_dir / "manifest.yaml").write_text(manifest, encoding="utf-8")
    meta = load_skill_package(skill_dir, source="user")
    assert meta is not None
    assert meta.name == "test-folder-pkg"
    # Folder scan should pick up references and assets by filename.
    assert "output-formats.md" in meta.references
    assert any("report.docx" in a for a in meta.assets)
