import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import shutil, tempfile
from pathlib import Path
import pytest

from app.database import Base, engine
import app.models  # noqa
Base.metadata.create_all(engine)


@pytest.fixture
def isolated_skills_dir(monkeypatch):
    temp_dir = Path(tempfile.mkdtemp(prefix="delete_test_"))
    import app.services.skill_sync as ss
    monkeypatch.setattr(ss, "USER_SKILLS_DIR", temp_dir)
    import app.services.skills_loader as sl
    monkeypatch.setattr(sl, "_registry", None)
    monkeypatch.setenv("ZHANLU_SKILLS_DIR", str(temp_dir))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_delete_skill_md_removes_file(isolated_skills_dir):
    from app.services.skill_sync import write_skill_md, delete_skill_md
    path = write_skill_md(name="to-delete", description="d", body="## Overview\n\nx", category="marketplace")
    assert Path(path).exists()
    result = delete_skill_md(name="to-delete", category="marketplace")
    assert result is True
    assert not Path(path).exists()


def test_delete_skill_md_returns_false_for_missing(isolated_skills_dir):
    from app.services.skill_sync import delete_skill_md
    result = delete_skill_md(name="never-existed", category="marketplace")
    assert result is False
