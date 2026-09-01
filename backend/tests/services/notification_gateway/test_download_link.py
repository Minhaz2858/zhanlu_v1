"""Tests for HMAC-signed download links."""

from app.services.notification_gateway.download_link import (
    build_download_url,
    generate_download_token,
    verify_download_token,
)


def test_generate_and_verify_roundtrip():
    token = generate_download_token("file-123")
    assert verify_download_token(token, "file-123") is True


def test_wrong_file_id_fails():
    token = generate_download_token("file-123")
    assert verify_download_token(token, "file-999") is False


def test_tampered_signature_fails():
    token = generate_download_token("file-123")
    payload_b64, sig = token.rsplit(".", 1)
    tampered = f"{payload_b64}.{'0' * len(sig)}"
    assert verify_download_token(tampered, "file-123") is False


def test_expired_token_fails():
    token = generate_download_token("file-123", ttl_days=-1)
    assert verify_download_token(token, "file-123") is False


def test_malformed_token_fails():
    assert verify_download_token("garbage", "file-123") is False
    assert verify_download_token("", "file-123") is False
    assert verify_download_token("abc.def.ghi", "file-123") is False


def test_build_download_url_contains_route():
    url = build_download_url("file-123")
    assert "/api/automations/email-download/file-123?token=" in url
