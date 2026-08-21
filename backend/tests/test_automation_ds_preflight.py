"""data_sync preflight: zero bound data sources -> fail before the LLM run."""
import os, sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

# Ensure the shared in-memory SQLite schema exists before the DB-backed
# tests below run (the DB tests are order-independent this way — the
# shared engine keeps tables created here for the whole session).
from app.database import engine  # noqa: E402
from app.models.base import Base  # noqa: E402
import app.models.knowledge_base  # noqa: E402,F401  (registers tables)
import app.models.project  # noqa: E402,F401
Base.metadata.create_all(engine)


def test_resolve_composes_all_three_binding_sources(monkeypatch):
    from app.services import automation_executor as ax
    calls = []

    def _own(agent):
        calls.append("own")
        return {"kb-own"}

    def _ws(db, agent, bound):
        calls.append("ws")
        return bound | {"kb-ws"}

    def _proj(db, agent, bound, project_id, project_name=None):
        calls.append("proj")
        assert project_id == "proj-1"
        return bound | {"kb-proj"}

    _MOD = "app.services.data_source_runtime.data_source_runtime"
    monkeypatch.setattr(f"{_MOD}.get_bound_data_source_ids", _own)
    monkeypatch.setattr(f"{_MOD}._maybe_extend_with_workspace_auto_bind", _ws)
    monkeypatch.setattr(f"{_MOD}._extend_with_project_kbs", _proj)

    out = ax._resolve_bound_data_source_ids(db=None, agent=object(), project_id="proj-1")
    assert calls == ["own", "ws", "proj"], "must use the same resolution chain as the tool runtime"
    assert set(out) == {"kb-own", "kb-ws", "kb-proj"}


def test_resolve_can_skip_workspace_auto_bind_for_scoped_automation(monkeypatch):
    from app.services import automation_executor as ax
    calls = []

    def _own(agent):
        calls.append("own")
        return set()

    def _ws(db, agent, bound):
        calls.append("ws")
        return bound | {"kb-global"}

    def _proj(db, agent, bound, project_id, project_name=None):
        calls.append("proj")
        return bound | {"kb-project"}

    _MOD = "app.services.data_source_runtime.data_source_runtime"
    monkeypatch.setattr(f"{_MOD}.get_bound_data_source_ids", _own)
    monkeypatch.setattr(f"{_MOD}._maybe_extend_with_workspace_auto_bind", _ws)
    monkeypatch.setattr(f"{_MOD}._extend_with_project_kbs", _proj)

    out = ax._resolve_bound_data_source_ids(
        db=None,
        agent=object(),
        project_id="proj-1",
        include_workspace_auto_bind=False,
    )
    assert calls == ["own", "proj"]
    assert set(out) == {"kb-project"}


def test_gate_blocks_data_sync_without_sources_and_allows_others():
    """Source-level: the preflight gate sits before the agent invocation,
    applies only to data_sync, and the no-binding path fails retryably."""
    import inspect
    from app.services import automation_executor as ax
    src = inspect.getsource(ax.execute_automation)
    gate = src.index("_resolve_bound_data_source_ids")
    invoke = src.index("pool.submit(_run_agent_in_conversation")
    assert gate < invoke, "preflight must run before the LLM call"
    assert 'data_sync' in src
    # No-binding failure stays retryable (a binding may be added later).
    no_binding_region = src[gate:src.index("_check_bound_source_connectivity(")]
    assert "_mark_failed(" in no_binding_region
    assert "_mark_failed_no_retry(" not in no_binding_region
    # Connectivity gate (Phase 1): unreachable sources retry (transient
    # outage); misconfigured ones (deleted/driver missing) fail fast.
    conn_region = src[src.index("_check_bound_source_connectivity("):invoke]
    assert "_mark_failed_no_retry(" in conn_region
    assert "_mark_failed(" in conn_region


# ---------------------------------------------------------------------------
# Legacy-name (dual-column) project binding parity with the UI
# ---------------------------------------------------------------------------
# The Resources panel (ProjectDetail.jsx) shows KBs bound via EITHER
# ``project_id`` (FK) OR the legacy ``project`` name string. The backend
# resolution chain must recognize the same bindings — otherwise the
# preflight fails with "No data source bound" while the UI clearly shows
# one connected (production bug, 2026-07-29).


def _make_kb(db, **kw):
    from app.models.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(**kw)
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return kb


def test_extend_with_project_kbs_matches_legacy_name():
    """(a) A KB bound ONLY via the legacy name column (project='test',
    project_id NULL) resolves when the caller supplies project_name."""
    from app.database import SessionLocal
    from app.services.data_source_runtime import data_source_runtime as dsrt
    db = SessionLocal()
    try:
        kb = _make_kb(db, name="erp-legacy", project="test", project_id=None,
                      source_kind="database")
        out = dsrt._extend_with_project_kbs(
            db, None, [], None, project_name="test",
        )
        assert kb.id in out
        db.delete(kb)
        db.commit()
    finally:
        db.close()


def test_extend_with_project_kbs_unions_id_and_name_dedup():
    """(b) id-bound and name-bound KBs union; a KB matching BOTH is not
    duplicated; name-asc ordering is preserved."""
    from app.database import SessionLocal
    from app.services.data_source_runtime import data_source_runtime as dsrt
    from app.models.project import Project
    db = SessionLocal()
    try:
        proj = Project(name="test")
        db.add(proj)
        db.commit()
        db.refresh(proj)
        by_id = _make_kb(db, name="aaa-by-id", project="global",
                         project_id=proj.id, source_kind="database")
        by_name = _make_kb(db, name="bbb-by-name", project="test",
                           project_id=None, source_kind="database")
        both = _make_kb(db, name="ccc-both", project="test",
                        project_id=proj.id, source_kind="database")
        out = dsrt._extend_with_project_kbs(
            db, None, [], proj.id, project_name="test",
        )
        assert by_id.id in out
        assert by_name.id in out
        assert out.count(both.id) == 1, "dual-matched KB must be deduped"
        name_of = {by_id.id: "aaa-by-id", by_name.id: "bbb-by-name",
                   both.id: "ccc-both"}
        returned_names = [name_of[i] for i in out]
        assert returned_names == sorted(returned_names), (
            "extra ids must preserve KnowledgeBase.name.asc() ordering"
        )
        for kb in (by_id, by_name, both):
            db.delete(kb)
        db.delete(proj)
        db.commit()
    finally:
        db.close()


def test_extend_with_project_kbs_global_and_none_never_match():
    """(d) The literal name 'global' is the default label, not a binding —
    it must never match; (None, None) early-returns empty."""
    from app.database import SessionLocal
    from app.services.data_source_runtime import data_source_runtime as dsrt
    db = SessionLocal()
    try:
        kb = _make_kb(db, name="plain-global", project="global",
                      project_id=None, source_kind="database")
        assert dsrt._extend_with_project_kbs(
            db, None, [], None, project_name="global") == []
        assert dsrt._extend_with_project_kbs(
            db, None, [], None, project_name=None) == []
        assert dsrt._extend_with_project_kbs(db, None, [], None) == []
        db.delete(kb)
        db.commit()
    finally:
        db.close()


def test_resolve_threads_project_name_to_project_kbs(monkeypatch):
    """_resolve_bound_data_source_ids forwards project_name into the
    project-KB extension step (the chain must not drop it)."""
    from app.services import automation_executor as ax
    seen = {}

    def _own(agent):
        return set()

    def _ws(db, agent, bound):
        return bound

    def _proj(db, agent, bound, project_id, project_name=None):
        seen["project_id"] = project_id
        seen["project_name"] = project_name
        return bound

    _MOD = "app.services.data_source_runtime.data_source_runtime"
    monkeypatch.setattr(f"{_MOD}.get_bound_data_source_ids", _own)
    monkeypatch.setattr(f"{_MOD}._maybe_extend_with_workspace_auto_bind", _ws)
    monkeypatch.setattr(f"{_MOD}._extend_with_project_kbs", _proj)

    ax._resolve_bound_data_source_ids(
        db=None, agent=object(), project_id=None, project_name="test",
    )
    assert seen == {"project_id": None, "project_name": "test"}


def test_resolve_includes_pinned_data_source(monkeypatch):
    """(c) A task's pinned data_source_id counts as a bound source even
    when no other binding resolves."""
    from app.database import SessionLocal
    from app.services import automation_executor as ax

    def _empty(*a, **kw):
        return set()

    _MOD = "app.services.data_source_runtime.data_source_runtime"
    monkeypatch.setattr(f"{_MOD}.get_bound_data_source_ids", _empty)
    monkeypatch.setattr(f"{_MOD}._maybe_extend_with_workspace_auto_bind",
                        lambda db, agent, bound: bound)
    monkeypatch.setattr(f"{_MOD}._extend_with_project_kbs",
                        lambda db, agent, bound, project_id, project_name=None: bound)

    db = SessionLocal()
    try:
        pinned = _make_kb(db, name="pinned-src", project="global",
                          project_id=None, source_kind="database")
        out = ax._resolve_bound_data_source_ids(
            db=db, agent=object(), project_id=None,
            pinned_data_source_id=pinned.id,
        )
        assert pinned.id in out
        # A soft-deleted pinned source must NOT satisfy the resolution.
        pinned.is_deleted = True
        db.commit()
        out2 = ax._resolve_bound_data_source_ids(
            db=db, agent=object(), project_id=None,
            pinned_data_source_id=pinned.id,
        )
        assert pinned.id not in out2
        db.delete(pinned)
        db.commit()
    finally:
        db.close()


def test_prepare_runtime_includes_pinned_data_source(monkeypatch):
    """The executor's data_source_id pin must reach the actual tool context,
    not only the preflight gate; otherwise list_data_sources/ask_data_agent
    see no source while preflight succeeds."""
    from types import SimpleNamespace
    from app.services.data_source_runtime import data_source_runtime as dsrt

    pinned = SimpleNamespace(
        id="kb-pinned",
        name="warehouse",
        db_type="mysql",
        database_name="aipdp_data_warehouse_prod",
        source_kind="database",
        file_type="",
        indexing_status=None,
        chunk_count=0,
        is_deleted=False,
    )

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [pinned]

    class _DB:
        def get(self, model, row_id):
            return pinned if row_id == pinned.id else None

        def query(self, model):
            return _Query()

    monkeypatch.setattr(dsrt, "get_bound_data_source_ids", lambda agent: [])
    monkeypatch.setattr(dsrt, "_maybe_extend_with_workspace_auto_bind", lambda db, agent, bound: bound)
    monkeypatch.setattr(dsrt, "_extend_with_project_kbs", lambda db, agent, bound, project_id, project_name=None: bound)

    _, prompt, extras = dsrt.prepare_data_source_runtime(
        _DB(),
        SimpleNamespace(name="automation_runtime_agent", org_id="default-org", app_id="default-app"),
        [],
        "base prompt",
        pinned_data_source_id=pinned.id,
    )

    assert extras["bound_kb_ids"] == [pinned.id]
    assert "warehouse" in prompt
