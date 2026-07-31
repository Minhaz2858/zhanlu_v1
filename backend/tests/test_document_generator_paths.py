"""Generated files live outside the public uploads tree; file_url is the
authenticated download route; the relocation script is idempotent."""
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings
from app.database import SessionLocal
from app.models.automation_execution import AutomationExecution
from app.models.automation_file import AutomationFile
from app.models.automation_task import AutomationTask
from app.services import automation_executor as ax
from app.services import document_generator as dg


def test_generate_document_writes_outside_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GENERATED_DIR", str(tmp_path / "gen"))
    path, url, mime = dg.generate_document(
        output_format="html", content="# Hi", title="R", task_id="t1", exec_id="e1",
    )
    assert str(path).startswith(str(tmp_path / "gen"))
    assert not str(path).startswith(str(settings.upload_path.resolve()))
    assert url == ""  # no public URL — authenticated route only
    assert mime.startswith("text/html")


def test_render_and_save_files_persists_authenticated_download_url():
    task = MagicMock()
    task.id = "t1"; task.name = "R"; task.output_format = "html"
    task.org_id = "o"; task.app_id = "a"; task.created_by_id = "u"
    execution = MagicMock(); execution.id = "exec1"
    fake_path = MagicMock()
    fake_path.exists.return_value = True
    fake_path.stat.return_value = MagicMock(st_size=1)
    db = MagicMock()
    with patch(
        "app.services.document_generator.generate_document",
        return_value=(fake_path, "", "text/html"),
    ):
        files, _ = ax._render_and_save_files(
            db, task, execution, "## R\nx", "prompt", fsm_meta=None,
        )
    assert files[0].file_url == f"/api/automations/files/{files[0].id}/download"


def _seed_file_row(db, path: Path, org_id="o"):
    task = AutomationTask(id=str(uuid.uuid4()), name="T", type="data_sync", org_id=org_id)
    db.add(task); db.flush()
    execution = AutomationExecution(
        id=str(uuid.uuid4()), automation_task_id=task.id, status="completed", org_id=org_id,
    )
    db.add(execution); db.flush()
    row = AutomationFile(
        id=str(uuid.uuid4()), execution_id=execution.id, automation_task_id=task.id,
        name="r.html", file_type="html", size=1, file_path=str(path),
        file_url="/api/uploads/automation/t1/e1/r.html", mime_type="text/html",
        org_id=org_id,
    )
    db.add(row); db.commit()
    return row


def test_relocate_moves_files_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GENERATED_DIR", str(tmp_path / "gen"))
    old_root = tmp_path / "up" / "automation"
    old_file = old_root / "t1" / "e1" / "r.html"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "up"))

    from scripts.relocate_automation_files import relocate

    db = SessionLocal()
    try:
        row = _seed_file_row(db, old_file)
        stats1 = relocate(db)
        assert stats1["moved"] == 1
        assert not old_file.exists()
        db.refresh(row)
        new_path = Path(row.file_path)
        assert str(new_path).startswith(str(tmp_path / "gen"))
        assert new_path.exists()
        assert row.file_url == f"/api/automations/files/{row.id}/download"
        stats2 = relocate(db)
        assert stats2["moved"] == 0 and stats2["skipped"] >= 1
        # cleanup so other tests don't see the row
        db.delete(row); db.commit()
    finally:
        db.rollback(); db.close()
