"""_is_transient_error: classifier-based, no bare-4xx-substring false blocks."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.automation_executor import _is_transient_error


def test_substring_400_no_longer_blocks_transient():
    # The old substring blocklist saw "400" anywhere and returned False even
    # when the real failure was a dropped connection.
    assert _is_transient_error(
        Exception("Exported 400 rows, then connection reset by peer")
    )


def test_explicit_permanent_codes_stay_non_transient():
    assert not _is_transient_error(Exception("HTTP 401 unauthorized"))
    assert not _is_transient_error(Exception("402 payment required"))
    assert not _is_transient_error(ValueError("invalid prompt: empty"))
    assert not _is_transient_error(PermissionError("403 forbidden: no access"))


def test_driver_level_db_drops_are_transient():
    assert _is_transient_error(Exception("database is locked"))
    assert _is_transient_error(Exception("OperationalError: server closed the connection"))


def test_existing_contract_unchanged():
    assert _is_transient_error(Exception("server returned 503 service unavailable"))
    assert _is_transient_error(Exception("connection timed out after 30s"))
    assert _is_transient_error(Exception("429 rate limit exceeded"))
