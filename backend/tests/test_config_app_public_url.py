"""Ensure APP_PUBLIC_URL is exposed on Settings and defaults to empty string."""
import os

from app.config import settings


def test_app_public_url_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    # Re-instantiate to re-read env (settings is a module-level singleton
    # created at import time; this test only verifies the field is present
    # and defaults to "" when no env var is set)
    from app.config import Settings
    s = Settings()
    assert s.APP_PUBLIC_URL == ""


def test_app_public_url_reads_env(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://zhanlu.example.com")
    from app.config import Settings
    s = Settings()
    assert s.APP_PUBLIC_URL == "https://zhanlu.example.com"
