"""Pytest configuration for backend tests — enable async test support."""
import os

# Must be set BEFORE any app.* imports so that app.config.settings
# reads SQLite. Pydantic-settings env vars take priority over .env files.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest

pytest_plugins = ("pytest_asyncio",)


def pytest_configure(config):
    config.option.asyncio_mode = "auto"
    # Unit tests must never spawn LibreOffice for the P0.3 render-to-image
    # thumbnail step — soffice conversion is slow and would make the export
    # test suites environment-dependent.  Thumbnail behavior itself is
    # covered by guard tests in tests/test_quality_pipeline.py.
    os.environ.setdefault("ZHANLU_THUMBNAILS_ENABLED", "0")
