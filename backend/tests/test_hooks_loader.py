"""SP2-WS-A: hooks hybrid loader — builtins + DB rules register correctly."""
import pytest

from app.services.hooks import get_hook_executor, HookEvent
from app.services.hooks.registry import BUILTIN_HOOKS


def test_builtin_hooks_defined():
    """The code registry ships at least one active safety hook."""
    assert len(BUILTIN_HOOKS) >= 2
    events = {h.event for h in BUILTIN_HOOKS}
    assert HookEvent.PRE_TOOL_USE.value in events
    assert HookEvent.POST_TOOL_USE.value in events


def test_clear_hooks_empties_executor():
    """clear_hooks() must remove all registered hooks (for reload)."""
    executor = get_hook_executor()
    # Register something
    from app.services.hooks import HookConfig
    executor.add_hook(HookConfig(id="tmp", event="pre_tool_use", type="prompt", prompt="x"))
    assert len(executor.list_hooks()) >= 1
    executor.clear_hooks()
    assert len(executor.list_hooks()) == 0


def test_load_hooks_registers_builtins():
    """load_hooks(db) must register all builtin hooks even with no DB rows."""
    from app.services.hooks.loader import load_hooks

    # A fake DB session whose query returns no rows.
    class _EmptyQuery:
        def filter(self, *a, **k):
            return self
        def all(self):
            return []
    class _FakeDB:
        def query(self, model):
            return _EmptyQuery()

    executor = get_hook_executor()
    executor.clear_hooks()
    count = load_hooks(_FakeDB())
    assert count >= len(BUILTIN_HOOKS)
    # Builtins should be present
    registered_ids = {h.id for h in executor.list_hooks()}
    for b in BUILTIN_HOOKS:
        assert b.id in registered_ids


def test_load_hooks_degrades_without_table():
    """load_hooks must still register builtins if the DB query raises
    (e.g. table not yet created on first run)."""
    from app.services.hooks.loader import load_hooks

    class _ExplodingDB:
        def query(self, model):
            raise RuntimeError("no such table")

    executor = get_hook_executor()
    executor.clear_hooks()
    count = load_hooks(_ExplodingDB())
    assert count >= len(BUILTIN_HOOKS)  # builtins still loaded
