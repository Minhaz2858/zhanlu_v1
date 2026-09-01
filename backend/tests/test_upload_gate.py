"""Unit tests for the upload settings gate (``_upload_blocked_reason``).

The gate reads the per-user ``UserSetting.file_upload_enabled`` row and
returns a reason string when uploads are disabled. It lives in
``app.routers.integrations`` as a pure function so these tests run without
bootstrapping FastAPI, a DB session, or the whole app.

Contract under test:
  - no setting row            -> allowed (default True)
  - file_upload_enabled True  -> allowed
  - file_upload_enabled False -> blocked with a clear 403 reason
  - None user / None db       -> allowed (dependency handles auth)
  - settings read raises      -> allowed (fail-open, logged)
"""

from app.routers.integrations import _upload_blocked_reason


class FakeUser:
    def __init__(self, uid="u1"):
        self.id = uid


class FakeSetting:
    def __init__(self, file_upload_enabled):
        self.file_upload_enabled = file_upload_enabled


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._raised = None

    def filter(self, *a, **k):
        return self

    def first(self):
        if self._raised:
            raise self._raised
        return self._rows[0] if self._rows else None


class FakeDb:
    def __init__(self, rows=None, raised=None):
        self._q = FakeQuery(rows or [])
        self._q._raised = raised

    def query(self, *a, **k):
        return self._q


def test_no_setting_row_allows_upload():
    db = FakeDb(rows=[])
    assert _upload_blocked_reason(FakeUser(), db) is None


def test_setting_true_allows_upload():
    db = FakeDb(rows=[FakeSetting(True)])
    assert _upload_blocked_reason(FakeUser(), db) is None


def test_setting_null_allows_upload():
    # A row can exist with file_upload_enabled=None (never toggled) —
    # the model default is True, so treat null as enabled.
    db = FakeDb(rows=[FakeSetting(None)])
    assert _upload_blocked_reason(FakeUser(), db) is None


def test_setting_false_blocks_upload_with_reason():
    db = FakeDb(rows=[FakeSetting(False)])
    reason = _upload_blocked_reason(FakeUser(), db)
    assert reason is not None
    assert "disabled" in reason
    assert "Settings" in reason


def test_none_user_allows_upload():
    # The auth dependency (get_current_user_required) is responsible for
    # rejecting unauthenticated callers — the gate itself must not crash.
    assert _upload_blocked_reason(None, FakeDb()) is None


def test_none_db_allows_upload():
    assert _upload_blocked_reason(FakeUser(), None) is None


def test_settings_read_error_fails_open():
    db = FakeDb(raised=RuntimeError("db gone"))
    assert _upload_blocked_reason(FakeUser(), db) is None
