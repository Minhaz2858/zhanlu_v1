"""Vision bridge tests — pasted-image Q&A must work with ANY main model.

Live 2026-10-13: deepseek-chat (text-only) answered "the image didn't come
through in a readable form" for a legible pasted screenshot. The bridge
runs attached images through a configurable VISION_* endpoint and injects
the analysis into the system prompt.
"""

import types

from app.services import image_understanding as iu


# ── vision-capable model detection ────────────────────────────────────────


def test_vision_capable_models_detected():
    assert iu.model_looks_vision_capable("gpt-4o")
    assert iu.model_looks_vision_capable("claude-sonnet-4-5")
    assert iu.model_looks_vision_capable("qwen2.5-vl-72b")
    assert iu.model_looks_vision_capable("gemini-2.5-flash")
    assert iu.model_looks_vision_capable("glm-4v-plus")


def test_text_only_models_detected():
    assert not iu.model_looks_vision_capable("deepseek-chat")
    assert not iu.model_looks_vision_capable("deepseek-reasoner")
    assert not iu.model_looks_vision_capable("")
    assert not iu.model_looks_vision_capable(None)


# ── bridge decision ───────────────────────────────────────────────────────


def test_bridge_auto_mode_skips_vision_models(monkeypatch):
    monkeypatch.setattr(iu.settings, "VISION_BRIDGE_MODE", "auto")
    assert iu.should_bridge("gpt-4o") is False
    assert iu.should_bridge("deepseek-chat") is True


def test_bridge_always_and_off_modes(monkeypatch):
    monkeypatch.setattr(iu.settings, "VISION_BRIDGE_MODE", "always")
    assert iu.should_bridge("gpt-4o") is True
    monkeypatch.setattr(iu.settings, "VISION_BRIDGE_MODE", "off")
    assert iu.should_bridge("deepseek-chat") is False


# ── prompt construction ───────────────────────────────────────────────────


def test_bridge_prompt_unconfigured_is_honest(monkeypatch):
    """No VISION_MODEL configured → the block must tell the agent it
    CANNOT see the images (relay the limitation), never invite guessing."""
    monkeypatch.setattr(iu.settings, "VISION_MODEL", "")
    monkeypatch.setattr(iu.settings, "OPENAI_BASE_URL", "")
    monkeypatch.setattr(iu.settings, "OPENAI_API_KEY", "")
    out = iu.build_bridge_prompt(["/api/uploads/abc.png"], "what is this?")
    assert out is not None
    assert "cannot see" in out.lower() or "no vision channel" in out.lower()
    assert "not guess" in out.lower().replace("do not", "not")


def test_bridge_prompt_ignores_non_image_attachments(monkeypatch):
    monkeypatch.setattr(iu.settings, "VISION_MODEL", "x")
    out = iu.build_bridge_prompt(
        ["/api/uploads/doc.pdf", "/api/uploads/doc.docx"], "q"
    )
    assert out is None


def test_bridge_prompt_injects_analysis(monkeypatch):
    """Happy path: the analysis text lands in the block verbatim."""
    monkeypatch.setattr(
        iu, "analyze_images",
        lambda urls, q: {
            "ran": True, "configured": True,
            "results": [{
                "file_url": urls[0], "name": "shot.png", "ok": True,
                "analysis": "CODE ZEBRA-7743, revenue 12.5M USD",
            }],
        },
    )
    out = iu.build_bridge_prompt(["/api/uploads/shot.png"], "read the code")
    assert out is not None
    assert "ZEBRA-7743" in out
    assert "shot.png" in out


def test_bridge_prompt_reports_failed_image_honestly(monkeypatch):
    monkeypatch.setattr(
        iu, "analyze_images",
        lambda urls, q: {
            "ran": True, "configured": True,
            "results": [{"file_url": urls[0], "name": "shot.png",
                         "ok": False, "error": "vision call failed: timeout"}],
        },
    )
    out = iu.build_bridge_prompt(["/api/uploads/shot.png"], "q")
    assert "FAILED" in out and "could not be analyzed" in out


# ── endpoint derivation ───────────────────────────────────────────────────


def test_endpoint_falls_back_to_openai_settings(monkeypatch):
    monkeypatch.setattr(iu.settings, "VISION_MODEL", "glm-4v")
    monkeypatch.setattr(iu.settings, "VISION_BASE_URL", "")
    monkeypatch.setattr(iu.settings, "VISION_API_KEY", "")
    monkeypatch.setattr(iu.settings, "OPENAI_BASE_URL", "https://x.example/v1")
    monkeypatch.setattr(iu.settings, "OPENAI_API_KEY", "sk-x")
    ep = iu._vision_endpoint()
    assert ep == ("https://x.example/v1", "sk-x", "glm-4v")


def test_endpoint_none_when_no_model(monkeypatch):
    monkeypatch.setattr(iu.settings, "VISION_MODEL", "")
    monkeypatch.setattr(iu.settings, "OPENAI_BASE_URL", "https://x.example/v1")
    monkeypatch.setattr(iu.settings, "OPENAI_API_KEY", "sk-x")
    assert iu._vision_endpoint() is None
