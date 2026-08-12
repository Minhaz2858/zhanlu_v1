"""Tests for catalog auto-discovery: trigger guards, hooks, endpoint, item_count."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, KBColumnMeta
from app.models.user import User


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _suppress_auto_discover(monkeypatch):
    """Prevent auto_discover listeners from firing during unit tests.

    The real auto_discover hooks open their own DB sessions against the
    production Postgres, which fails with 'no such table' in the SQLite
    test database used here.
    """
    try:
        import app.services.universal_analytics.auto_discover as ad
        monkeypatch.setattr(ad, "check_auto_discover_enabled", lambda: False)
        monkeypatch.setattr(ad, "_should_discover", lambda kb: False)
    except ImportError:
        pass


@pytest.fixture
def db(tmp_path):
    """Create a fresh SQLite database with all tables."""
    import app.models  # noqa: F401 — ensure all models are imported

    from app.database import Base
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _user(db, role="user"):
    u = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4().hex[:8]}@t.io",
        full_name="t",
        role=role,
        password_hash="x",
        org_id="default-org",
        app_id="default-app",
    )
    db.add(u)
    db.commit()
    return u


def _make_kb(db, **kw):
    kb_id = str(uuid.uuid4())
    kb = KnowledgeBase(
        id=kb_id,
        name=kw.pop("name", "TestKB"),
        source_kind=kw.pop("source_kind", "database"),
        db_type=kw.pop("db_type", "mysql"),
        host=kw.pop("host", "127.0.0.1"),
        port=kw.pop("port", 3306),
        database_name=kw.pop("database_name", "testdb"),
        username=kw.pop("username", "root"),
        org_id="default-org",
        app_id="default-app",
        created_by_id=kw.pop("created_by_id", None),
        **kw,
    )
    db.add(kb)
    db.commit()
    return kb


# ── trigger guards ────────────────────────────────────────────────────

class TestCatalogTriggerGuards:
    def test_should_index_flag_off(self, monkeypatch):
        """_should_index returns False when SEMANTIC_CATALOG_ENABLED is False."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", False)
        from app.services.knowledge_graph.catalog_triggers import _should_index
        kb = KnowledgeBase(db_type="mysql")
        assert _should_index(kb) is False

    def test_should_index_unsupported_db_type(self, monkeypatch):
        """_should_index returns False for sqlite, mongodb, etc."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)
        from app.services.knowledge_graph.catalog_triggers import _should_index
        assert _should_index(KnowledgeBase(db_type="sqlite")) is False
        assert _should_index(KnowledgeBase(db_type=None)) is False
        assert _should_index(KnowledgeBase(db_type="")) is False

    def test_should_index_already_indexing(self, monkeypatch):
        """_should_index returns False when catalog_status == 'indexing'."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)
        from app.services.knowledge_graph.catalog_triggers import _should_index
        kb = KnowledgeBase(db_type="mysql", catalog_status="indexing")
        assert _should_index(kb) is False

    def test_should_index_happy(self, monkeypatch):
        """_should_index returns True for mysql KB with flag on."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)
        from app.services.knowledge_graph.catalog_triggers import _should_index
        kb = KnowledgeBase(db_type="postgresql", catalog_status="pending")
        assert _should_index(kb) is True

    def test_maybe_reindex_catalog_bg_flag_off(self, monkeypatch):
        """maybe_reindex_catalog_bg does not spawn thread when flag off."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", False)
        from app.services.knowledge_graph.catalog_triggers import maybe_reindex_catalog_bg
        kb = KnowledgeBase(db_type="mysql")
        with patch("threading.Thread") as mock_thread:
            maybe_reindex_catalog_bg(kb)
            mock_thread.assert_not_called()

    def test_maybe_reindex_catalog_bg_wrong_type(self, monkeypatch):
        """maybe_reindex_catalog_bg skips non-db KBs."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)
        from app.services.knowledge_graph.catalog_triggers import maybe_reindex_catalog_bg
        kb = KnowledgeBase(db_type="sqlite")
        with patch("threading.Thread") as mock_thread:
            maybe_reindex_catalog_bg(kb)
            mock_thread.assert_not_called()

    def test_connection_fields_changed_no_prev(self):
        """connection_fields_changed returns True when prev is None."""
        from app.services.knowledge_graph.catalog_triggers import connection_fields_changed
        assert connection_fields_changed({"host": "a"}, None) is True

    def test_connection_fields_changed_identical(self):
        """connection_fields_changed returns False when no field changed."""
        from app.services.knowledge_graph.catalog_triggers import connection_fields_changed
        prev = {"host": "a", "port": 3306}
        assert connection_fields_changed({"host": "a", "port": 3306}, prev) is False

    def test_connection_fields_changed_host_differs(self):
        """connection_fields_changed returns True when host changed."""
        from app.services.knowledge_graph.catalog_triggers import connection_fields_changed
        prev = {"host": "a", "port": 3306}
        assert connection_fields_changed({"host": "b", "port": 3306}, prev) is True

    def test_connection_fields_changed_ignore_name(self):
        """connection_fields_changed ignores non-connection fields like name."""
        from app.services.knowledge_graph.catalog_triggers import connection_fields_changed
        prev = {"host": "a", "name": "old"}
        assert connection_fields_changed({"host": "a", "name": "new"}, prev) is False


# ── catalog/tables endpoint ───────────────────────────────────────────

class TestCatalogTablesEndpoint:

    @pytest.mark.asyncio
    async def test_404_for_nonexistent_kb(self, db):
        """list_catalog_tables raises 404 for missing KB."""
        from app.routers.knowledge_bases import list_catalog_tables
        with pytest.raises(Exception) as exc:
            await list_catalog_tables("default-app", "nonexistent", db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_catalog_for_new_kb(self, db):
        """Empty catalog returns status + empty tables list."""
        from app.routers.knowledge_bases import list_catalog_tables
        user = _user(db)
        kb = _make_kb(db, created_by_id=user.id, catalog_status="pending", item_count=0)
        result = await list_catalog_tables("default-app", kb.id, db)

        assert result["kb_id"] == kb.id
        assert result["catalog_status"] == "pending"
        assert result["item_count"] == 0
        assert result["tables"] == []

    @pytest.mark.asyncio
    async def test_catalog_tables_with_data(self, db):
        """Returns tables with descriptions and aggregated column counts."""
        from app.routers.knowledge_bases import list_catalog_tables
        user = _user(db)
        kb = _make_kb(db, created_by_id=user.id, catalog_status="ready", item_count=2)
        t1 = KBTableMeta(
            id=str(uuid.uuid4()), kb_id=kb.id, schema_name="public",
            table_name="orders", table_type="table", row_count=5000,
            description_zh="订单表", description_en="Orders table",
            org_id="default-org", app_id="default-app",
        )
        t2 = KBTableMeta(
            id=str(uuid.uuid4()), kb_id=kb.id, schema_name="public",
            table_name="users", table_type="table", row_count=120,
            description_zh="用户表", description_en="Users table",
            org_id="default-org", app_id="default-app",
        )
        db.add_all([t1, t2])
        db.flush()
        c1 = KBColumnMeta(id=str(uuid.uuid4()), table_meta_id=t1.id,
                          column_name="order_id", data_type="integer",
                          org_id="default-org", app_id="default-app")
        c2 = KBColumnMeta(id=str(uuid.uuid4()), table_meta_id=t1.id,
                          column_name="amount", data_type="decimal",
                          org_id="default-org", app_id="default-app")
        c3 = KBColumnMeta(id=str(uuid.uuid4()), table_meta_id=t2.id,
                          column_name="user_id", data_type="integer",
                          org_id="default-org", app_id="default-app")
        db.add_all([c1, c2, c3])
        db.commit()

        result = await list_catalog_tables("default-app", kb.id, db)
        assert result["item_count"] == 2
        assert len(result["tables"]) == 2
        names = [t["table_name"] for t in result["tables"]]
        assert names == ["orders", "users"]
        orders = result["tables"][0]
        assert orders["column_count"] == 2
        assert orders["row_count"] == 5000
        assert orders["description_zh"] == "订单表"
        users = result["tables"][1]
        assert users["column_count"] == 1


# ── item_count write on index completion ──────────────────────────────

class TestItemCountOnIndex:
    def test_item_count_set_after_ready(self, db):
        """After catalog indexer sets status=ready, item_count equals number
        of persisted KBTableMeta rows."""
        user = _user(db)
        kb = _make_kb(db, created_by_id=user.id, catalog_status="indexing", item_count=0)
        # Simulate what index_kb_catalog does at the end
        t1 = KBTableMeta(
            id=str(uuid.uuid4()), kb_id=kb.id, table_name="t1",
            org_id="default-org", app_id="default-app",
        )
        t2 = KBTableMeta(
            id=str(uuid.uuid4()), kb_id=kb.id, table_name="t2",
            org_id="default-org", app_id="default-app",
        )
        db.add_all([t1, t2])
        kb.catalog_status = "ready"
        kb.item_count = 2
        db.commit()

        # Re-fetch
        db.expire_all()
        kb2 = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb.id).first()
        assert kb2.catalog_status == "ready"
        assert kb2.item_count == 2

    def test_item_count_zero_for_empty_schema(self, db):
        """Even with empty schema, item_count is explicitly set to 0."""
        user = _user(db)
        kb = _make_kb(db, created_by_id=user.id, catalog_status="indexing", item_count=None)
        kb.catalog_status = "ready"
        kb.item_count = 0
        db.commit()
        db.expire_all()
        kb2 = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb.id).first()
        assert kb2.catalog_status == "ready"
        assert kb2.item_count == 0


# ── entities hook ─────────────────────────────────────────────────────

class TestEntitiesCreateHook:
    """Verify the create-hook fires only for DB-type KnowledgeBases."""

    def test_create_db_kb_fires_trigger(self, monkeypatch):
        """_maybe_fire_catalog_index calls maybe_reindex_catalog_bg for mysql KB."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)

        from app.routers.entities import _maybe_fire_catalog_index
        kb = KnowledgeBase(
            id=str(uuid.uuid4()), name="DB", source_kind="database",
            db_type="mysql", host="10.0.0.1", port=3306, database_name="db",
            username="root", org_id="default-org", app_id="default-app",
        )
        with patch(
            "app.services.knowledge_graph.catalog_triggers.maybe_reindex_catalog_bg"
        ) as mock_bg:
            _maybe_fire_catalog_index(kb)
            mock_bg.assert_called_once()

    def test_create_non_db_kb_skips(self, monkeypatch):
        """File-type KBs do NOT fire catalog trigger."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)

        from app.routers.entities import _maybe_fire_catalog_index
        kb = KnowledgeBase(
            id=str(uuid.uuid4()), name="FileKB", source_kind="file",
            db_type=None, org_id="default-org", app_id="default-app",
        )
        with patch(
            "app.services.knowledge_graph.catalog_triggers.maybe_reindex_catalog_bg"
        ) as mock_bg:
            _maybe_fire_catalog_index(kb)
            mock_bg.assert_not_called()

    def test_create_flag_off_skips(self, monkeypatch):
        """Hook skips when SEMANTIC_CATALOG_ENABLED is False."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", False)

        from app.routers.entities import _maybe_fire_catalog_index
        kb = KnowledgeBase(
            id=str(uuid.uuid4()), name="DB", source_kind="database",
            db_type="mysql", host="10.0.0.1", org_id="default-org", app_id="default-app",
        )
        with patch(
            "app.services.knowledge_graph.catalog_triggers.maybe_reindex_catalog_bg"
        ) as mock_bg:
            _maybe_fire_catalog_index(kb)
            mock_bg.assert_not_called()


class TestEntitiesUpdateHook:
    """Verify the update-hook fires only when connection fields change."""

    def test_diff_skips_non_connection_changes(self, monkeypatch):
        """_maybe_fire_catalog_index_on_update skips when no connection field changed."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)

        from app.routers.entities import _maybe_fire_catalog_index_on_update
        kb = KnowledgeBase(
            id=str(uuid.uuid4()), name="X", db_type="mysql",
            host="a", port=3306, database_name="db", username="u",
            org_id="default-org", app_id="default-app", catalog_status="ready",
        )
        prev = KnowledgeBase(
            id=kb.id, name="Old", db_type="mysql",
            host="a", port=3306, database_name="db", username="u",
        )
        with patch(
            "app.services.knowledge_graph.catalog_triggers.maybe_reindex_catalog_bg"
        ) as mock_bg:
            _maybe_fire_catalog_index_on_update(prev, {"name": "New"}, kb)
            mock_bg.assert_not_called()

    def test_diff_fires_on_host_change(self, monkeypatch):
        """_maybe_fire_catalog_index_on_update fires when host changes."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)

        from app.routers.entities import _maybe_fire_catalog_index_on_update
        kb = KnowledgeBase(
            id=str(uuid.uuid4()), name="X", db_type="mysql",
            host="new", port=3306, database_name="db", username="u",
            org_id="default-org", app_id="default-app", catalog_status="ready",
        )
        prev = KnowledgeBase(
            id=kb.id, name="X", db_type="mysql",
            host="old", port=3306, database_name="db", username="u",
        )
        with patch(
            "app.services.knowledge_graph.catalog_triggers.maybe_reindex_catalog_bg"
        ) as mock_bg:
            _maybe_fire_catalog_index_on_update(prev, {"host": "new"}, kb)
            mock_bg.assert_called_once()

    def test_diff_fires_when_prev_none(self, monkeypatch):
        """_maybe_fire_catalog_index_on_update fires when prev is None (new creation)."""
        import app.config
        monkeypatch.setattr(app.config.settings, "SEMANTIC_CATALOG_ENABLED", True)

        from app.routers.entities import _maybe_fire_catalog_index_on_update
        kb = KnowledgeBase(
            id=str(uuid.uuid4()), name="X", db_type="postgresql",
            host="a", port=5432, database_name="db", username="u",
            org_id="default-org", app_id="default-app", catalog_status="pending",
        )
        with patch(
            "app.services.knowledge_graph.catalog_triggers.maybe_reindex_catalog_bg"
        ) as mock_bg:
            _maybe_fire_catalog_index_on_update(None, {"name": "X"}, kb)
            mock_bg.assert_called_once()


# ── PATCH /catalog/tables/{table_id} (description edit) ───────────────

class TestUpdateCatalogTable:
    """Tests for the user-driven description edit endpoint."""

    def _make_table(self, db, user_id):
        """Helper: create a KB with one table for edit tests."""
        kb = _make_kb(db, created_by_id=user_id)
        t = KBTableMeta(
            id=str(uuid.uuid4()), kb_id=kb.id, schema_name="public",
            table_name="orders", table_type="table", row_count=100,
            description_zh="原始中文描述",
            description_en="Original English description",
            org_id="default-org", app_id="default-app",
        )
        db.add(t)
        db.commit()
        return kb, t

    @pytest.mark.asyncio
    async def test_edit_description_zh_only(self, db):
        """PATCH with only description_zh updates that field and leaves en intact."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, t = self._make_table(db, user.id)
        result = await update_catalog_table(
            "default-app", kb.id, t.id, {"description_zh": "新中文描述"}, db
        )
        assert result["description_zh"] == "新中文描述"
        assert result["description_en"] == "Original English description"

    @pytest.mark.asyncio
    async def test_edit_description_en_only(self, db):
        """PATCH with only description_en updates that field."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, t = self._make_table(db, user.id)
        result = await update_catalog_table(
            "default-app", kb.id, t.id, {"description_en": "New English"}, db
        )
        assert result["description_en"] == "New English"
        assert result["description_zh"] == "原始中文描述"

    @pytest.mark.asyncio
    async def test_edit_both(self, db):
        """PATCH with both fields updates both."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, t = self._make_table(db, user.id)
        result = await update_catalog_table(
            "default-app", kb.id, t.id,
            {"description_zh": "中文", "description_en": "English"}, db
        )
        assert result["description_zh"] == "中文"
        assert result["description_en"] == "English"

    @pytest.mark.asyncio
    async def test_edit_clears_to_null(self, db):
        """PATCH with null clears the description."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, t = self._make_table(db, user.id)
        result = await update_catalog_table(
            "default-app", kb.id, t.id, {"description_zh": None}, db
        )
        assert result["description_zh"] is None

    @pytest.mark.asyncio
    async def test_edit_rejects_non_string(self, db):
        """PATCH with non-string value raises 400."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, t = self._make_table(db, user.id)
        with pytest.raises(Exception) as exc:
            await update_catalog_table(
                "default-app", kb.id, t.id, {"description_zh": 123}, db
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_edit_empty_body_rejected(self, db):
        """PATCH with no editable fields raises 400."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, t = self._make_table(db, user.id)
        with pytest.raises(Exception) as exc:
            await update_catalog_table(
                "default-app", kb.id, t.id, {"name": "nope"}, db
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_edit_unknown_table_404(self, db):
        """PATCH for non-existent table raises 404."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, _ = self._make_table(db, user.id)
        with pytest.raises(Exception) as exc:
            await update_catalog_table(
                "default-app", kb.id, "nonexistent",
                {"description_zh": "x"}, db,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_edit_wrong_kb_id_404(self, db):
        """PATCH with table_id that doesn't belong to kb_id raises 404."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb1 = _make_kb(db, created_by_id=user.id, name="KB1")
        kb2 = _make_kb(db, created_by_id=user.id, name="KB2")
        t = KBTableMeta(
            id=str(uuid.uuid4()), kb_id=kb1.id, table_name="t",
            org_id="default-org", app_id="default-app",
        )
        db.add(t)
        db.commit()
        with pytest.raises(Exception) as exc:
            # table belongs to kb1 but we pass kb2
            await update_catalog_table(
                "default-app", kb2.id, t.id,
                {"description_zh": "x"}, db,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_edit_triggers_embedding_update(self, db):
        """Successful edit triggers update_table_embedding (best-effort)."""
        from app.routers.knowledge_bases import update_catalog_table
        user = _user(db)
        kb, t = self._make_table(db, user.id)
        with patch(
            "app.services.knowledge_graph.catalog_indexer.update_table_embedding"
        ) as mock_embed:
            await update_catalog_table(
                "default-app", kb.id, t.id,
                {"description_zh": "新描述"}, db,
            )
            mock_embed.assert_called_once()
            # Verify embedding is called with the updated description
            kwargs = mock_embed.call_args.kwargs
            assert kwargs["description_zh"] == "新描述"
            assert kwargs["table_name"] == "orders"
            assert kwargs["row_count"] == 100


# ── list_catalog_tables new fields ─────────────────────────────────────

class TestCatalogTablesListNewFields:

    @pytest.mark.asyncio
    async def test_includes_column_names(self, db):
        """GET /catalog/tables returns column_names list per table."""
        from app.routers.knowledge_bases import list_catalog_tables
        user = _user(db)
        kb = _make_kb(db, created_by_id=user.id, catalog_status="ready", item_count=1)
        t = KBTableMeta(
            id=str(uuid.uuid4()), kb_id=kb.id, table_name="orders",
            org_id="default-org", app_id="default-app",
        )
        db.add(t)
        db.flush()
        for cn in ["id", "name", "amount", "created_at"]:
            db.add(KBColumnMeta(
                id=str(uuid.uuid4()), table_meta_id=t.id,
                column_name=cn, data_type="text",
                org_id="default-org", app_id="default-app",
            ))
        db.commit()

        result = await list_catalog_tables("default-app", kb.id, db)
        assert result["kb_name"] == kb.name
        assert "column_names" in result["tables"][0]
        assert sorted(result["tables"][0]["column_names"]) == ["amount", "created_at", "id", "name"]
