"""Tests for the Whisper-based audio extractor (``extract_audio``)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.config import settings
from app.services.document_ingestion.extractors import extract_audio


def test_extract_audio_calls_whisper(tmp_path, monkeypatch):
    audio = tmp_path / "fake.mp3"
    audio.write_bytes(b"\x00\x01\x02")

    monkeypatch.setattr(settings, "AUDIO_TRANSCRIBE_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://api.openai.com/v1")

    with patch("app.services.document_ingestion.extractors.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"text": "hello world"}
        mock_post.return_value = mock_resp

        result = extract_audio(str(audio))

    assert result == "hello world"
    mock_post.assert_called_once()
    args, _ = mock_post.call_args
    assert "audio/transcriptions" in args[0]


def test_extract_audio_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "AUDIO_TRANSCRIBE_ENABLED", False)

    with patch("app.services.document_ingestion.extractors.httpx.post") as mock_post:
        result = extract_audio("/tmp/fake.mp3")

    assert result == ""
    mock_post.assert_not_called()


def test_extract_audio_no_api_key_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "AUDIO_TRANSCRIBE_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")

    with patch("app.services.document_ingestion.extractors.httpx.post") as mock_post:
        result = extract_audio("/tmp/fake.mp3")

    assert result == ""
    mock_post.assert_not_called()


def test_extract_audio_httpx_error_returns_empty(tmp_path, monkeypatch):
    audio = tmp_path / "fake.mp3"
    audio.write_bytes(b"\x00\x01\x02")

    monkeypatch.setattr(settings, "AUDIO_TRANSCRIBE_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://api.openai.com/v1")

    with patch(
        "app.services.document_ingestion.extractors.httpx.post",
        side_effect=Exception("boom"),
    ):
        result = extract_audio(str(audio))

    assert result == ""
