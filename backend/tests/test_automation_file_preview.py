from types import SimpleNamespace

from app.routers import automation_api


class _QueryStub:
    def __init__(self, result):
        self.result = result

    def filter(self, *args):
        return self

    def first(self):
        return self.result


class _DbStub:
    def __init__(self, result):
        self.result = result

    def query(self, *args):
        return _QueryStub(self.result)


def test_serialize_automation_file_exposes_preview_metadata():
    file_record = SimpleNamespace(
        id="file-123",
        name="monthly-report.pdf",
        file_type="pdf",
        size=4096,
        mime_type="application/pdf",
        file_url="/api/automations/files/file-123/download",
        created_date=None,
    )

    payload = automation_api._serialize_automation_file(file_record)

    assert payload == {
        "id": "file-123",
        "automation_file_id": "file-123",
        "name": "monthly-report.pdf",
        "file_name": "monthly-report.pdf",
        "type": "pdf",
        "file_type": "pdf",
        "mime_type": "application/pdf",
        "size": 4096,
        "file_size": 4096,
        "url": "/api/automations/files/file-123/download",
        "file_url": "/api/automations/files/file-123/download",
        "preview_url": "/api/automations/files/file-123/preview",
        "has_preview": True,
        "source": "automation_file",
        "created_date": None,
    }


def test_preview_automation_file_returns_inline_response(tmp_path):
    report_path = tmp_path / "report.html"
    report_path.write_text("<h1>Monthly report</h1>", encoding="utf-8")
    file_record = SimpleNamespace(
        id="file-123",
        org_id="org-1",
        file_path=str(report_path),
        name="report.html",
        file_type="html",
        mime_type="text/html",
    )

    response = automation_api.preview_automation_file(
        "file-123",
        db=_DbStub(file_record),
        user=SimpleNamespace(org_id="org-1"),
    )

    assert response.media_type == "text/html"
    assert response.headers["content-disposition"].startswith("inline;")


def test_chat_message_artifacts_for_files_exposes_preview_metadata():
    files = [
        SimpleNamespace(
            id="file-1",
            name="monthly-report.pdf",
            file_type="pdf",
            size=2048,
            mime_type="application/pdf",
            file_url="/api/automations/files/file-1/download",
        )
    ]

    artifacts = automation_api._chat_message_artifacts_for_files(files)

    assert artifacts[0]["preview_url"] == "/api/automations/files/file-1/preview"
    assert artifacts[0]["has_preview"] is True
    assert artifacts[0]["file_size"] == 2048
    assert artifacts[0]["file_url"] == "/api/automations/files/file-1/download"
    assert artifacts[0]["title"] == "monthly-report.pdf"
    assert artifacts[0]["name"] == "monthly-report.pdf"
    assert artifacts[0]["type"] == "pdf"
    assert artifacts[0]["source"] == "automation_file"
    assert artifacts[0]["artifact_id"] == "file-1"


def test_preview_automation_file_docx_uses_extension_type(tmp_path, monkeypatch):
    """Regression: preview endpoint passed the MIME type to ``convert_to_preview``
    instead of the file-extension type ("docx"), so the conversion silently
    returned None and the route raised 415 — the user reported this with
    "Daily Sales Data Sync.docx" showing "Preview unavailable" in the chat
    right pane.

    Verifies the contract: ``convert_to_preview`` is called with the
    extension-style type ("docx"), not the MIME string. We mock
    convert_to_preview rather than depending on LibreOffice, which is
    brittle in test environments (python-docx's bare Document().save()
    produced files LibreOffice couldn't load on this install).
    """
    docx_path = tmp_path / "report.docx"
    docx_path.write_bytes(b"fake docx bytes")

    file_record = SimpleNamespace(
        id="file-docx-1",
        org_id="org-1",
        file_path=str(docx_path),
        name="report.docx",
        file_type="docx",
        # Real-world value: the full MIME string. The bug was passing
        # this to convert_to_preview (which expects "docx") — that caused
        # None return and 415 "Preview conversion is unavailable".
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    captured = {}

    def fake_convert_to_preview(data, name, artifact_type):
        captured["data"] = data
        captured["name"] = name
        captured["artifact_type"] = artifact_type
        # Return the tuple shape that preview_builder.convert_to_preview
        # documents: (preview_data, preview_file_name, preview_mime_type).
        return (b"%PDF-1.7 fake body", "report.pdf", "application/pdf")

    monkeypatch.setattr(
        "app.services.artifacts.preview_builder.convert_to_preview",
        fake_convert_to_preview,
    )

    response = automation_api.preview_automation_file(
        "file-docx-1",
        db=_DbStub(file_record),
        user=SimpleNamespace(org_id="org-1"),
    )

    # Contract assertions: convert_to_preview was called with the
    # file-extension type ("docx"), NOT the MIME string.
    assert captured["artifact_type"] == "docx", (
        f"convert_to_preview was called with {captured['artifact_type']!r}; "
        "expected 'docx'. Passing the MIME string causes convert_to_preview "
        "to fall through all branches and return None (the 415 bug)."
    )
    assert captured["artifact_type"] != file_record.mime_type

    # Response uses the (bytes, name, mime) tuple shape returned by
    # convert_to_preview — not a dataclass. The route previously crashed
    # with AttributeError when reaching this point.
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["content-disposition"].endswith('"report.pdf"')
    assert response.body.startswith(b"%PDF")
