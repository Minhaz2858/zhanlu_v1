"""Tests for multimodal handler modules (Phase 0.2).

Verifies that each handler registers correctly, produces the expected
schema, and returns structured error responses when MiniMax is not configured.
Uses mock patches to avoid real API calls.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


# ─── Registry tests (no API calls) ─────────────────────────────────────

class TestMultimodalRegistry:
    """Verify all 4 multimodal handlers are registered in ToolRegistry."""

    @pytest.fixture(autouse=True)
    def _ensure_imported(self):
        """Import all handler modules so registry.register() runs."""
        from app.services.tool_handlers.multimodal import image_gen  # noqa: F401
        from app.services.tool_handlers.multimodal import video_gen  # noqa: F401
        from app.services.tool_handlers.multimodal import threed_gen  # noqa: F401
        from app.services.tool_handlers.multimodal import effects  # noqa: F401

    def test_all_four_registered(self):
        """All 4 multimodal tools should be in the registry."""
        from app.services.tool_registry import registry
        names = registry.list_available()
        for expected in ["mm_image_gen", "mm_video_gen", "mm_3d_gen", "mm_effects"]:
            assert expected in names, f"{expected} not found in registry"

    def test_schemas_have_correct_names(self):
        """Each schema's function.name must match its registry name."""
        from app.services.tool_registry import registry

        expected = {
            "mm_image_gen": ["prompt"],
            "mm_video_gen": ["prompt"],
            "mm_3d_gen": [],
            "mm_effects": ["prompt", "image_url"],
        }

        for name, required_fields in expected.items():
            entry = registry.get_entry(name)
            assert entry is not None, f"{name} not registered"
            func = entry.schema.get("function", {})
            assert func["name"] == name, f"{name} schema has wrong function name"
            params = func.get("parameters", {})
            assert isinstance(params, dict)
            assert params["type"] == "object"
            req = params.get("required", [])
            assert set(req) == set(required_fields), (
                f"{name} required={req}, expected={required_fields}"
            )

    def test_schemas_are_narrow_not_polymorphic(self):
        """Each multimodal schema is narrow — no polymorphic media_type field."""
        from app.services.tool_registry import registry

        for name in ["mm_image_gen", "mm_video_gen", "mm_3d_gen", "mm_effects"]:
            entry = registry.get_entry(name)
            assert entry is not None
            params = entry.schema.get("function", {}).get("parameters", {})
            props = params.get("properties", {})
            # No polymorphic catch-all
            assert "media_type" not in props, f"{name} has media_type (polymorphic)"
            assert "kind" not in props, f"{name} has kind field (polymorphic)"
            # Each has at most 5 parameters (narrow)
            assert len(props) <= 5, f"{name} has {len(props)} params (too wide)"


# ─── Handler tests without API key ──────────────────────────────────────

class TestMultimodalHandlersNoKey:
    """Test handler returns structured error when MINIMAX_API_KEY is missing."""

    async def test_image_gen_no_key(self):
        from app.services.tool_handlers.multimodal.image_gen import _mm_image_gen
        result = await _mm_image_gen({"prompt": "test"})
        assert result["success"] is False
        assert "MINIMAX_API_KEY" in result["error"] or "not configured" in result["error"]

    async def test_video_gen_no_key(self):
        from app.services.tool_handlers.multimodal.video_gen import _mm_video_gen
        result = await _mm_video_gen({"prompt": "test"})
        assert result["success"] is False
        assert "MINIMAX_API_KEY" in result["error"] or "not configured" in result["error"]

    async def test_3d_gen_no_key(self):
        from app.services.tool_handlers.multimodal.threed_gen import _mm_3d_gen
        result = await _mm_3d_gen({"description": "test"})
        assert result["success"] is False  # Not available yet

    async def test_effects_no_key(self):
        from app.services.tool_handlers.multimodal.effects import _mm_effects
        result = await _mm_effects({
            "prompt": "watercolor",
            "image_url": "https://example.com/img.jpg",
        })
        assert result["success"] is False
        assert "MINIMAX_API_KEY" in result["error"] or "not configured" in result["error"]

    async def test_image_gen_missing_prompt(self):
        from app.services.tool_handlers.multimodal.image_gen import _mm_image_gen
        result = await _mm_image_gen({})
        assert result["success"] is False
        assert "prompt" in result.get("error", "").lower()

    async def test_video_gen_missing_prompt(self):
        from app.services.tool_handlers.multimodal.video_gen import _mm_video_gen
        result = await _mm_video_gen({})
        assert result["success"] is False
        assert "prompt" in result.get("error", "").lower()

    async def test_effects_missing_required(self):
        from app.services.tool_handlers.multimodal.effects import _mm_effects
        result = await _mm_effects({"prompt": "test"})
        assert result["success"] is False
        assert "image_url" in result.get("error", "").lower()


# ─── Mocked API tests ───────────────────────────────────────────────────

class TestMultimodalHandlersMocked:
    """Test handlers with mocked MiniMax API responses."""

    @patch("app.services.tool_handlers.multimodal.image_gen._check_config", return_value=None)
    @patch("app.services.tool_handlers.multimodal.image_gen._minimax_post")
    async def test_image_gen_success(self, mock_post, mock_check):
        mock_post.return_value = {
            "data": [{"url": "https://cdn.example.com/img1.png"}],
        }
        from app.services.tool_handlers.multimodal.image_gen import _mm_image_gen
        result = await _mm_image_gen({"prompt": "A cat"})
        assert result["success"] is True
        assert len(result["images"]) == 1
        assert result["images"][0] == "https://cdn.example.com/img1.png"

    @patch("app.services.tool_handlers.multimodal.video_gen._check_config", return_value=None)
    @patch("app.services.tool_handlers.multimodal.video_gen._minimax_post")
    async def test_video_gen_returns_task_id(self, mock_post, mock_check):
        mock_post.return_value = {
            "task_id": "vid_abc123",
            "status": "processing",
        }
        from app.services.tool_handlers.multimodal.video_gen import _mm_video_gen
        result = await _mm_video_gen({"prompt": "Ocean waves", "wait": False})
        assert result["success"] is True
        assert result["task_id"] == "vid_abc123"
        assert result["status"] == "processing"

    @patch("app.services.tool_handlers.multimodal.effects._check_config", return_value=None)
    @patch("app.services.tool_handlers.multimodal.effects._minimax_post")
    async def test_effects_success(self, mock_post, mock_check):
        mock_post.return_value = {
            "data": [{"url": "https://cdn.example.com/styled.png"}],
        }
        from app.services.tool_handlers.multimodal.effects import _mm_effects
        result = await _mm_effects({
            "effect": "style_transfer",
            "prompt": "Monet style",
            "image_url": "https://example.com/photo.jpg",
        })
        assert result["success"] is True
        assert len(result["images"]) == 1
        assert result["effect"] == "style_transfer"

    @patch("app.services.tool_handlers.multimodal.image_gen._check_config", return_value=None)
    @patch("app.services.tool_handlers.multimodal.image_gen._minimax_post")
    async def test_image_gen_api_error(self, mock_post, mock_check):
        mock_post.side_effect = Exception("Internal Server Error")
        from app.services.tool_handlers.multimodal.image_gen import _mm_image_gen
        result = await _mm_image_gen({"prompt": "A cat"})
        assert result["success"] is False
        assert "Internal Server Error" in result["error"]
