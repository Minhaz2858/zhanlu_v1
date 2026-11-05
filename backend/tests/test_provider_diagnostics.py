"""Phase 3.5 P5 — provider configuration diagnostics (TDD, offline only).

ZERO network calls; ZERO secret values in the report.

Run: pytest tests/test_provider_diagnostics.py -q
"""
import json

from app.services.provider_diagnostics import (
    provider_diagnostics,
    record_probe_error,
)


class _FakeSettings:
    LLM_MODEL = "qwen-flash"
    OPENAI_API_KEY = "sk-TEST-NOT-REAL"
    OPENAI_BASE_URL = "https://compat.example.invalid/v1"
    LLM_FALLBACKS = []


def test_report_shape_and_no_secrets():
    d = provider_diagnostics(_FakeSettings(), llm_rows=[], sidecar_path="/nonexistent")
    assert d["provider_configured"] is True
    assert d["credential_present"] is True
    assert d["primary_model"] == "qwen-flash"
    assert d["network_calls_made"] == 0
    # No secret values anywhere in the serialized report.
    blob = json.dumps(d)
    assert "sk-TEST-NOT-REAL" not in blob


def test_model_enabled_check_against_registry():
    rows = [
        {"name": "qwen-flash", "model": "qwen-flash", "provider": "dashscope",
         "enabled": True, "is_deleted": False},
        {"name": "old-model", "model": "old-model", "provider": "x",
         "enabled": True, "is_deleted": True},  # soft-deleted → excluded
    ]
    d = provider_diagnostics(_FakeSettings(), llm_rows=rows, sidecar_path="/nonexistent")
    assert d["model_enabled"] is True
    assert "old-model" not in d["registered_active_models"]


def test_primary_model_missing_from_registry_flags_inconsistent():
    rows = [{"name": "other", "model": "other", "enabled": True, "is_deleted": False}]
    d = provider_diagnostics(_FakeSettings(), llm_rows=rows, sidecar_path="/nonexistent")
    assert d["model_enabled"] is False
    assert d["consistent"] is False
    assert any("NOT among enabled" in f for f in d["consistency_findings"])


def test_fallback_referencing_disabled_model_flags_inconsistent():
    s = _FakeSettings()
    s.LLM_FALLBACKS = [{"name": "backup", "model": "retired", "api_key": "x"}]
    rows = [{"name": "retired", "model": "retired", "enabled": False, "is_deleted": False}]
    d = provider_diagnostics(s, llm_rows=rows, sidecar_path="/nonexistent")
    assert any("retired" in f and "disabled" in f for f in d["consistency_findings"])


def test_no_credential_flags_inconsistent():
    s = _FakeSettings()
    s.OPENAI_API_KEY = ""
    d = provider_diagnostics(s, llm_rows=[], sidecar_path="/nonexistent")
    assert d["credential_present"] is False
    assert any("no credential" in f for f in d["consistency_findings"])


def test_last_error_category_from_sidecar(tmp_path):
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text(json.dumps({"category": "invalid_credentials"}))
    d = provider_diagnostics(_FakeSettings(), llm_rows=[], sidecar_path=sidecar)
    assert d["last_error_category"] == "invalid_credentials"


def test_record_probe_error_writes_category_only(tmp_path):
    sidecar = tmp_path / "sidecar.json"
    record_probe_error("quota_exhausted", sidecar_path=sidecar)
    data = json.loads(sidecar.read_text())
    assert data == {"category": "quota_exhausted"}


def test_probe_error_categorization(tmp_path):
    # The probe path maps 401→invalid_credentials (Phase 3 evidence: HTTP 401).
    from app.services.reliability_metrics import provider_status

    class _Resp:
        status_code = 401
        text = '{"error":{"code":"invalid_api_key"}}'

    import app.services.reliability_metrics as rm

    sidecar = tmp_path / "sidecar.json"
    orig_post = rm.httpx.post
    rm.httpx.post = lambda *a, **k: _Resp()
    try:
        status = provider_status(probe=True, sidecar_path=sidecar)
        assert status["available"] is False
        assert json.loads(sidecar.read_text())["category"] == "invalid_credentials"
        # The probe payload itself never carries secrets.
        assert "sk-" not in json.dumps(status)
    finally:
        rm.httpx.post = orig_post
