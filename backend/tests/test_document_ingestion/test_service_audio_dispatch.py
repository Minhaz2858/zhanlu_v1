"""Tests for audio/video dispatch in ``prepare_for_context``."""
from __future__ import annotations

from unittest.mock import patch

from app.services.document_ingestion.service import prepare_for_context


def test_prepare_dispatches_audio_to_whisper(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"\x00\x00\x00\x00")

    with patch(
        "app.services.document_ingestion.service._resolve_local_path",
        return_value=audio,
    ), patch(
        "app.services.document_ingestion.extractors.extract_audio",
        return_value="transcribed text",
    ) as m:
        result = prepare_for_context("/api/uploads/song.mp3")

    m.assert_called_once()
    assert result["text"] == "transcribed text"
    assert result["file_type"] == "mp3"
    assert result["is_image"] is False
    assert result["error"] is None


def test_prepare_audio_falls_back_to_marker_when_transcription_empty(tmp_path):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"\x00\x00\x00\x00")

    with patch(
        "app.services.document_ingestion.service._resolve_local_path",
        return_value=audio,
    ), patch(
        "app.services.document_ingestion.extractors.extract_audio",
        return_value="",
    ):
        result = prepare_for_context("/api/uploads/song.mp3")

    assert "[Audio attached: song.mp3]" in result["text"]


def test_prepare_dispatches_video_to_stub(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x00")

    with patch(
        "app.services.document_ingestion.service._resolve_local_path",
        return_value=video,
    ):
        result = prepare_for_context("/api/uploads/clip.mp4")

    assert "[Video attached: clip.mp4]" in result["text"]
    assert result["file_type"] == "mp4"
    assert result["is_image"] is False
