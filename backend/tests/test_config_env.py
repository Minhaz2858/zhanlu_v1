"""Test that config.py reads env-driven settings correctly with SQLite fallback.

These tests assert that:
1. Defaults are safe — SQLite, no sandbox, no MinIO
2. Env overrides work — DATABASE_URL, REDIS_URL, MINIO_*, SANDBOX_*
3. Existing settings are unchanged (no regression)
"""
import os
import sys

# Ensure backend is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib
import app.config as config_module


def _reload_settings(*, environ: dict[str, str] | None = None) -> config_module.Settings:
    """Create a fresh Settings object with optional env overrides.

    Because the module-level singleton `settings` is cached, we use the
    Pydantic constructor directly so each test gets its own instance.
    """
    saved = dict(os.environ)
    try:
        # Remove all Zhanlu-relevant keys so we start clean.
        for key in list(os.environ.keys()):
            if any(
                key.startswith(prefix)
                for prefix in (
                    "DATABASE_URL",
                    "REDIS_URL",
                    "MINIO_",
                    "SANDBOX_",
                    "ARTIFACT_",
                    "JWT_",
                    "BACKEND_",
                    "OPENAI_",
                )
            ):
                os.environ.pop(key, None)

        if environ:
            os.environ.update(environ)

        # Re-import from scratch to get a fresh Settings().
        importlib.reload(config_module)
        return config_module.Settings()
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ── Task 1a: DATABASE_URL / REDIS_URL defaults ──────────────────────────


def test_database_url_defaults_to_sqlite():
    """Without any env, DATABASE_URL is SQLite (so unit tests keep working)."""
    s = _reload_settings()
    assert "sqlite" in s.DATABASE_URL
    assert s.is_sqlite is True


def test_database_url_from_env():
    """DATABASE_URL is overridden by the environment."""
    s = _reload_settings(
        environ={"DATABASE_URL": "postgresql+asyncpg://u:p@host:5432/db"}
    )
    assert s.DATABASE_URL == "postgresql+asyncpg://u:p@host:5432/db"
    assert s.is_sqlite is False


def test_redis_url_defaults_to_empty():
    """Without env, REDIS_URL is empty (Redis disabled in local dev)."""
    s = _reload_settings()
    assert s.REDIS_URL == ""


def test_redis_url_from_env():
    """REDIS_URL is overridden by the environment."""
    s = _reload_settings(environ={"REDIS_URL": "redis://redis:6379/0"})
    assert s.REDIS_URL == "redis://redis:6379/0"


# ── Task 1b: MinIO / S3 settings ────────────────────────────────────────


def test_minio_defaults():
    """Without env, MinIO is not configured (postgres_bytea is the default)."""
    s = _reload_settings()
    assert s.MINIO_ENDPOINT is None or s.MINIO_ENDPOINT == ""
    assert s.MINIO_ACCESS_KEY is None or s.MINIO_ACCESS_KEY == ""
    assert s.MINIO_SECRET_KEY is None or s.MINIO_SECRET_KEY == ""
    assert s.MINIO_BUCKET == "zhanlu-artifacts"
    assert s.ARTIFACT_STORAGE_BACKEND == "postgres_bytea"


def test_minio_from_env():
    """MinIO settings are populated from environment variables."""
    s = _reload_settings(
        environ={
            "MINIO_ENDPOINT": "minio:9000",
            "MINIO_ACCESS_KEY": "minioadmin",
            "MINIO_SECRET_KEY": "minioadmin",
            "MINIO_BUCKET": "my-bucket",
            "ARTIFACT_STORAGE_BACKEND": "minio",
        }
    )
    assert s.MINIO_ENDPOINT == "minio:9000"
    assert s.MINIO_ACCESS_KEY == "minioadmin"
    assert s.MINIO_SECRET_KEY == "minioadmin"
    assert s.MINIO_BUCKET == "my-bucket"
    assert s.ARTIFACT_STORAGE_BACKEND == "minio"


# ── Task 1c: Sandbox settings (runbook §6) ──────────────────────────────


def test_sandbox_defaults():
    """Without env, sandbox is disabled with safe defaults."""
    s = _reload_settings()
    assert s.SANDBOX_ENABLED is False
    assert s.SANDBOX_IMAGE_PYTHON == "zhanlu-sandbox-python:latest"
    assert s.SANDBOX_IMAGE_OFFICE == "zhanlu-sandbox-office:latest"
    assert s.SANDBOX_IMAGE_WEBAPP == "zhanlu-sandbox-webapp:latest"
    assert s.SANDBOX_DEFAULT_NETWORK == "none"
    assert s.SANDBOX_DEFAULT_TIMEOUT_SECONDS == 120
    assert s.SANDBOX_DEFAULT_MEMORY_MB == 1024
    assert s.SANDBOX_DEFAULT_CPUS == 1.0
    assert s.SANDBOX_DEFAULT_PIDS_LIMIT == 128
    assert s.SANDBOX_ALLOW_DOCKER_SOCKET_ONLY_IN_WORKER is True
    assert s.SANDBOX_TMP_ROOT == "/tmp/zhanlu_sandbox"


def test_sandbox_from_env():
    """All sandbox settings can be overridden via environment variables."""
    s = _reload_settings(
        environ={
            "SANDBOX_ENABLED": "true",
            "SANDBOX_IMAGE_PYTHON": "my-py:1.0",
            "SANDBOX_IMAGE_OFFICE": "my-office:1.0",
            "SANDBOX_IMAGE_WEBAPP": "my-web:1.0",
            "SANDBOX_DEFAULT_NETWORK": "bridge",
            "SANDBOX_DEFAULT_TIMEOUT_SECONDS": "300",
            "SANDBOX_DEFAULT_MEMORY_MB": "2048",
            "SANDBOX_DEFAULT_CPUS": "2.5",
            "SANDBOX_DEFAULT_PIDS_LIMIT": "256",
            "SANDBOX_ALLOW_DOCKER_SOCKET_ONLY_IN_WORKER": "false",
            "SANDBOX_TMP_ROOT": "/custom/tmp",
        }
    )
    assert s.SANDBOX_ENABLED is True
    assert s.SANDBOX_IMAGE_PYTHON == "my-py:1.0"
    assert s.SANDBOX_IMAGE_OFFICE == "my-office:1.0"
    assert s.SANDBOX_IMAGE_WEBAPP == "my-web:1.0"
    assert s.SANDBOX_DEFAULT_NETWORK == "bridge"
    assert s.SANDBOX_DEFAULT_TIMEOUT_SECONDS == 300
    assert s.SANDBOX_DEFAULT_MEMORY_MB == 2048
    assert s.SANDBOX_DEFAULT_CPUS == 2.5
    assert s.SANDBOX_DEFAULT_PIDS_LIMIT == 256
    assert s.SANDBOX_ALLOW_DOCKER_SOCKET_ONLY_IN_WORKER is False
    assert s.SANDBOX_TMP_ROOT == "/custom/tmp"


# ── Task 1d: Existing settings are unchanged (regression check) ─────────


def test_existing_settings_jwt_still_works():
    """JWT settings still work with defaults and env overrides."""
    s = _reload_settings(environ={"JWT_SECRET": "super-secret-test"})
    assert s.JWT_SECRET == "super-secret-test"


def test_existing_settings_backend_port_still_works():
    """BACKEND_PORT still defaults to 5002."""
    s = _reload_settings()
    assert s.BACKEND_PORT == 5002


def test_existing_settings_openai_still_works():
    """OPENAI_API_KEY still fields the env."""
    s = _reload_settings(environ={"OPENAI_API_KEY": "sk-test"})
    assert s.OPENAI_API_KEY == "sk-test"
