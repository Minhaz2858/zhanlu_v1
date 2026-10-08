"""ONE-DASHBOARD-PER-CONVERSATION + T10 tool-scope regression tests.

Product bug (2026-09-02): the plan is ONE full-stack dashboard per chat
session, but every follow-up ("make it white and blue", "widgets missing",
"rebuild it") re-ran create_fullstack_dashboard in the SAME conversation; the
slug auto-uniquify silently spawned clones (sales-performance-dashboard →
-2/-5/-5-2) (conv 056d8c83).

Tool-scope bug (same session): the agent tools list/update/revert skipped the
T10 scope gate that the REST router enforces — any org user could list and
overwrite another user's personal dashboard, and update_fullstack_dashboard
stole the app from the creating conversation (chat_thread_id rebind), which
would have resurrected the duplicate-dashboard bug through reopen-and-edit.

Fix under test:
  1. _create_fullstack_dashboard REFUSES (success:false + redirect fields)
     when this conversation already owns ANY dashboard (chat_thread_id scan)
     with the same name OR slug, steering the agent to
     update_fullstack_dashboard(slug=<existing>).
  2. list/update/revert enforce T10: personal → creator only; company →
     creator or admin.
  3. update never rebinds chat_thread_id away from a live owner conversation
     (rebinds only when unowned or the owner conversation was deleted).
  4. build_dashboard_state_block injects every dashboard the conversation
     owns so soft-intent follow-ups route to the update tool.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.data_execution.prompt_block import build_dashboard_state_block


# ── Fake DB for create-handler tests ────────────────────────────────────
class _FakeDb:
    """Routes DashboardApp filter queries by the compared literal.

    .filter(literal).order_by().all()   → chat-ownership scan
    .filter(literal).first()            → slug-collision probe

      conv-owns-dash      -> [OWNED]                (conversation owns a board)
      sales-performance-dashboard -> OWNED via first() (slug taken)
      anything else       -> [] / None
    """

    OWNED = SimpleNamespace(
        slug="sales-performance-dashboard",
        name="销售业绩仪表盘",
        app_url="/api/dashboards/apps/sales-performance-dashboard/",
        is_deleted=False,
        created_by_id="user-1",
        scope="personal",
    )

    def __init__(self):
        self._last_val = None

    def query(self, model):
        outer = self

        class _Q:
            def filter(self, *a, **kw):
                try:
                    outer._last_val = a[0].right.value
                except Exception:
                    outer._last_val = None
                return self

            def order_by(self, *a, **kw):
                return self

            def first(self):
                if outer._last_val == "sales-performance-dashboard":
                    return _FakeDb.OWNED
                return None

            def all(self):
                if outer._last_val == "conv-owns-dash":
                    return [_FakeDb.OWNED]
                return []

        return _Q()


def _args(slug="sales-performance-dashboard", name="销售业绩仪表盘"):
    return {
        "name": name,
        "slug": slug,
        "description": "test",
        "datasource_id": "kb-1",
        "metrics": [
            {
                "id": "m1", "type": "kpi",
                "title": "Revenue",
                "sql": "SELECT SUM(amount) AS total FROM sales",
                "options": {},
            }
        ],
        "refresh_interval_seconds": 30,
        "theme": "light",
        "style": "standard",
        "scope": "personal",
        "insights": [],
        "layout": [],
    }


def _run_create(args, db, ctx):
    from app.services.tool_handlers import dashboard_tools

    mgr = MagicMock()
    generator = MagicMock()
    generator.app_dir.return_value = _tmp_app_dir()

    async def _run():
        with (
            patch.object(dashboard_tools, "_user_can_access_kb", return_value=MagicMock(id="kb-1")),
            patch("app.services.dashboard_app.manager.dashboard_app_manager", mgr),
            patch("app.services.dashboard_app.generator.get_generator", return_value=generator),
            patch.object(dashboard_tools, "_stamp_dashboard_conversation"),
            patch(
                "app.services.dashboard_app.analytics.enrich_spec",
                side_effect=lambda db_, kb, spec: spec,
            ),
            patch("app.services.artifacts.artifact_service.ArtifactService") as _ArtSvc,
        ):
            _ArtSvc.return_value.create_artifact.return_value = None
            return await dashboard_tools._create_fullstack_dashboard(
                args, db, "user-1", context=ctx,
            )

    return asyncio.new_event_loop().run_until_complete(_run())


# ── Create: one dashboard per conversation ──────────────────────────────

def test_create_refuses_when_conversation_owns_same_name_dashboard():
    """Same conversation + same name → refusal + redirect, NO second build."""
    from app.services.tool_handlers import dashboard_tools

    db = _FakeDb()
    ctx = {"org_id": "org-1", "project_id": "proj-1",
           "conversation_id": "conv-owns-dash"}
    generator = MagicMock()

    result = _run_create(_args(slug="sales-performance-dashboard-v2"), db, ctx)

    assert result["success"] is False
    assert result["action"] == "update_fullstack_dashboard"
    assert result["existing_slug"] == "sales-performance-dashboard"
    assert "update_fullstack_dashboard" in result["error"]
    # The generator must never have been invoked — nothing was built.
    generator.app_dir.assert_not_called()


def test_create_refuses_when_conversation_owns_same_slug_dashboard():
    """Same conversation + same slug (the exact auto-uniquify trap)."""
    db = _FakeDb()
    ctx = {"org_id": "org-1", "project_id": "proj-1",
           "conversation_id": "conv-owns-dash"}

    result = _run_create(_args(), db, ctx)

    assert result["success"] is False
    assert result["existing_slug"] == "sales-performance-dashboard"
    assert result["action"] == "update_fullstack_dashboard"


def test_create_allows_a_deliberately_different_dashboard_in_same_conversation():
    """Conversation owns a dashboard, but user asks for a DIFFERENT board
    (different name + slug) → still allowed to create."""
    db = _FakeDb()
    ctx = {"org_id": "org-1", "project_id": "proj-1",
           "conversation_id": "conv-owns-dash"}

    result = _run_create(
        _args(slug="sample-financial", name="SAMPLE 财务仪表盘"), db, ctx
    )

    assert result["success"] is True, result
    assert result["dashboard_app"]["slug"] == "sample-financial"


def test_create_without_owned_dashboard_still_uniquifies_taken_slug():
    """Conversation owns nothing + requested slug taken elsewhere → legacy
    auto-uniquify behavior preserved (no regression)."""
    db = _FakeDb()
    ctx = {"org_id": "org-1", "project_id": "proj-1",
           "conversation_id": "fresh-conv-no-dash"}

    result = _run_create(_args(), db, ctx)

    assert result["success"] is True, result
    assert result["dashboard_app"]["slug"].startswith("sales-performance-dashboard-")


# ── Prompt-block dashboard state ────────────────────────────────────────

def test_build_dashboard_state_block_injects_owned_dashboard():
    block = build_dashboard_state_block(_FakeDb(), conversation_id="conv-owns-dash")
    assert block is not None
    assert "DASHBOARD STATE" in block
    assert "sales-performance-dashboard" in block
    assert "update_fullstack_dashboard" in block
    assert "NEVER call create_fullstack_dashboard" in block


def test_build_dashboard_state_block_empty_when_no_dashboard():
    assert (
        build_dashboard_state_block(_FakeDb(), conversation_id="no-owner-conv")
        is None
    )
    assert build_dashboard_state_block(_FakeDb(), conversation_id="") is None


# ── T10 scope: update/revert/list must respect ownership ────────────────

class _MapDb:
    """Deterministic DB stub for update/revert handlers.

    DashboardApp queries (app_row) and AgentConversation queries (conv_row)
    each return their configured row — enough to exercise the T10 gate and
    the chat-ownership-preservation logic.
    """

    def __init__(self, app_row=None, conv_row=None):
        self.app_row = app_row
        self.conv_row = conv_row
        self.commits = 0
        self._last_literal = None

    def query(self, model):
        from app.models.agent_conversation import AgentConversation
        from app.models.dashboard_app import DashboardApp

        outer = self
        _is_conv = model is AgentConversation

        class _Q:
            def filter(self, *a, **kw):
                try:
                    outer._last_literal = a[0].right.value
                except Exception:
                    outer._last_literal = None
                return self

            def order_by(self, *a, **kw):
                return self

            def first(self):
                if _is_conv:
                    return outer.conv_row
                return outer.app_row

            def all(self):
                if _is_conv:
                    return [outer.conv_row] if outer.conv_row is not None else []
                return [outer.app_row] if outer.app_row is not None else []

        return _Q()

    def commit(self):
        self.commits += 1


def _foreign_personal_row():
    return SimpleNamespace(
        slug="sales-performance-dashboard",
        name="销售业绩仪表盘",
        app_url="/api/dashboards/apps/sales-performance-dashboard/",
        is_deleted=False,
        created_by_id="user-999",          # NOT the acting user
        scope="personal",
        spec={"name": "销售业绩仪表盘", "slug": "sales-performance-dashboard"},
        chat_thread_id="conv-owner",
        refresh_interval_seconds=30,
        datasource_kb_id="kb-1",
    )


def _update_args(slug="sales-performance-dashboard"):
    return {"slug": slug, "theme": "light"}


def test_update_denied_for_foreign_personal_dashboard():
    """A personal dashboard owned by another user cannot be edited by agent
    tools (T10) — mirrors the REST-layer 404."""
    from app.services.tool_handlers import dashboard_tools

    db = _MapDb(app_row=_foreign_personal_row())

    async def _run():
        with patch.object(dashboard_tools, "_stamp_dashboard_conversation"):
            return await dashboard_tools._update_fullstack_dashboard(
                _update_args(), db, "user-1", context={},
            )

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result["success"] is False
    assert "permission" in result["error"]


def test_revert_denied_for_foreign_personal_dashboard():
    from app.services.tool_handlers import dashboard_tools

    db = _MapDb(app_row=_foreign_personal_row())

    async def _run():
        return await dashboard_tools._revert_fullstack_dashboard(
            {"slug": "sales-performance-dashboard"}, db, "user-1", context={},
        )

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result["success"] is False
    assert "permission" in result["error"]


def test_update_preserves_owner_conversation_binding():
    """Editing from a DIFFERENT live conversation must not steal the app from
    the creating conversation (one-dashboard-per-session invariant)."""
    from app.services.tool_handlers import dashboard_tools

    row = _foreign_personal_row()
    row.created_by_id = "user-1"  # the acting user IS the creator
    row.chat_thread_id = "conv-owner"
    # Owner conversation still exists (conv query returns a row).
    db = _MapDb(app_row=row, conv_row=MagicMock())
    mgr = MagicMock()  # regenerate / set_status mocked away

    async def _run():
        with (
            patch("app.services.dashboard_app.manager.dashboard_app_manager", mgr),
            patch.object(dashboard_tools, "_stamp_dashboard_conversation") as _stamp,
        ):
            return await dashboard_tools._update_fullstack_dashboard(
                _update_args(), db, "user-1",
                context={"conversation_id": "conv-editor"},
            ), _stamp

    (result, _stamp) = asyncio.new_event_loop().run_until_complete(_run())
    assert result["success"] is True, result
    # Ownership was NOT moved to the editing conversation…
    assert row.chat_thread_id == "conv-owner"
    # …and the foreign chat was not stamped as dashboard-dedicated.
    _stamp.assert_not_called()


def test_update_rebinds_when_owner_conversation_was_deleted():
    """Reopen-and-edit flow (owner chat deleted): rebinding to the editing
    conversation is allowed so the app regains a live owner."""
    from app.services.tool_handlers import dashboard_tools

    row = _foreign_personal_row()
    row.created_by_id = "user-1"
    row.chat_thread_id = "conv-dead-owner"  # owner row no longer exists
    db = _MapDb(app_row=row)  # conv query returns None → owner gone
    mgr = MagicMock()

    async def _run():
        with (
            patch("app.services.dashboard_app.manager.dashboard_app_manager", mgr),
            patch.object(dashboard_tools, "_stamp_dashboard_conversation"),
        ):
            return await dashboard_tools._update_fullstack_dashboard(
                _update_args(), db, "user-1",
                context={"conversation_id": "conv-editor"},
            )

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result["success"] is True, result
    assert row.chat_thread_id == "conv-editor"


def test_list_fullstack_dashboards_is_user_scoped():
    """list_fullstack_dashboards passes user_id so personal apps of other
    users never leak into an agent session."""
    from app.services.tool_handlers import dashboard_tools

    mgr = MagicMock()
    mgr.list_apps.return_value = []

    async def _run():
        with patch("app.services.dashboard_app.manager.dashboard_app_manager", mgr):
            return await dashboard_tools._list_fullstack_dashboards(
                {}, None, "user-1", context={"org_id": "org-1"},
            )

    result = asyncio.new_event_loop().run_until_complete(_run())
    assert result["success"] is True
    mgr.list_apps.assert_called_once_with(org_id="org-1", user_id="user-1")


def _tmp_app_dir():
    d = Path(tempfile.mkdtemp(prefix="one_per_conv_test_"))
    (d / "config.json").write_text("{}")
    (d / "api.py").write_text("# api")
    dist = d / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>")
    return d
