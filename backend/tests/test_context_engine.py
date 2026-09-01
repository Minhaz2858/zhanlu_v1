"""Tests for the pluggable context engine."""
import asyncio
import os
import sys
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.context_engine import (
    ContextEngine,
    DefaultContextEngine,
    register_engine,
    get_context_engine,
)


def test_get_default_engine():
    """get_context_engine returns a DefaultContextEngine by default."""
    engine = get_context_engine(model="deepseek-chat")
    assert isinstance(engine, DefaultContextEngine)
    assert engine.model == "deepseek-chat"


def test_get_engine_by_name():
    """get_context_engine returns the named engine."""
    engine = get_context_engine("default", model="test-model")
    assert isinstance(engine, DefaultContextEngine)


def test_register_custom_engine():
    """Custom engines can be registered and retrieved."""
    class TestEngine(ContextEngine):
        def __init__(self, **kwargs):
            pass
        def should_compress(self, messages, current_tokens=None):
            return False
        async def compress(self, messages, *, model="", force=False):
            return messages, False

    register_engine("test-custom", TestEngine)
    engine = get_context_engine("test-custom")
    assert isinstance(engine, TestEngine)


def test_default_engine_should_compress():
    """DefaultContextEngine.should_compress delegates to should_autocompact."""
    engine = DefaultContextEngine(model="deepseek-chat")
    with patch("app.services.compaction.should_autocompact", return_value=True):
        assert engine.should_compress([], current_tokens=100000) is True
    with patch("app.services.compaction.should_autocompact", return_value=False):
        assert engine.should_compress([], current_tokens=100) is False


def test_default_engine_prune_delegates():
    """DefaultContextEngine.prune_tool_results_only delegates to pre_api_prune."""
    engine = DefaultContextEngine(model="deepseek-chat")
    messages = [{"role": "user", "content": "hi"}]
    result, n = engine.prune_tool_results_only(messages, current_tokens=100)
    assert result is messages
    assert n == 0


def test_should_compress_info():
    """should_compress_info returns (bool, reason)."""
    engine = DefaultContextEngine(model="deepseek-chat")
    with patch("app.services.compaction.should_autocompact", return_value=True):
        should, reason = engine.should_compress_info([], current_tokens=100000)
        assert should is True
        assert reason is not None


def test_custom_engine_compress():
    """A custom engine's compress method is called."""
    class TestEngine(ContextEngine):
        def should_compress(self, messages, current_tokens=None):
            return True
        async def compress(self, messages, *, model="", force=False):
            return messages + [{"role": "system", "content": "compressed"}], True

    engine = TestEngine()
    result, was_compacted = asyncio.run(engine.compress([]))
    assert was_compacted is True
    assert len(result) == 1
