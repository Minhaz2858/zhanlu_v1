"""Tests for ``resolve_message_project_id`` (llm_router) and its wiring
into the v2/v3 chat handlers.

Background: the frontend sends the live-URL ``project_id`` on every
chat message, but ``resolve_effective_llm`` was called with only
``conv.project_id``. A conversation created without a project (legacy
rows, chats first opened from the main page) therefore ignored the
selected project's configured LLM and silently fell through to the
catalog default — the agent read the project's data sources (the
data-source runtime *does* honor the body override) while thinking
with the wrong model.

The helper gives both handlers the same precedence as the data-source
runtime: body.project_id (validated) > conv.project_id.
"""
from __future__ import annotations

import ast
import os

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.models.project import Project
from app.services.llm_router import resolve_message_project_id


# ---------------------------------------------------------------------------
# DB fixture — real Project ORM model on an isolated in-memory SQLite
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = sa.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Project.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _add_project(session, pid, name, is_deleted=False):
    session.add(
        Project(
            id=pid,
            name=name,
            org_id="org1",
            app_id="app1",
            is_deleted=is_deleted,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Helper behavior
# ---------------------------------------------------------------------------

def test_body_project_wins_when_conv_has_none(db_session):
    _add_project(db_session, "p1", "Marketing Team")
    assert (
        resolve_message_project_id(
            db_session, conv_project_id=None, body_project_id="p1"
        )
        == "p1"
    )


def test_body_project_wins_over_conv_project(db_session):
    """User opened the conv from a different project page — the live
    selection (body) takes precedence, matching the data-source runtime."""
    _add_project(db_session, "p1", "Marketing Team")
    assert (
        resolve_message_project_id(
            db_session, conv_project_id="p-old", body_project_id="p1"
        )
        == "p1"
    )


def test_conv_project_used_when_no_body(db_session):
    assert (
        resolve_message_project_id(
            db_session, conv_project_id="p-conv", body_project_id=None
        )
        == "p-conv"
    )


def test_stale_body_project_falls_back_to_conv(db_session):
    """Body project_id pointing at a deleted/unknown project must not
    500 or phantom-bind — drop it and keep the conv binding."""
    assert (
        resolve_message_project_id(
            db_session, conv_project_id="p-conv", body_project_id="p-ghost"
        )
        == "p-conv"
    )


def test_soft_deleted_body_project_falls_back_to_conv(db_session):
    _add_project(db_session, "p-del", "Deleted", is_deleted=True)
    assert (
        resolve_message_project_id(
            db_session, conv_project_id="p-conv", body_project_id="p-del"
        )
        == "p-conv"
    )


def test_stale_body_with_no_conv_returns_none(db_session):
    assert (
        resolve_message_project_id(
            db_session, conv_project_id=None, body_project_id="p-ghost"
        )
        is None
    )


def test_neither_returns_none(db_session):
    assert (
        resolve_message_project_id(
            db_session, conv_project_id=None, body_project_id=None
        )
        is None
    )


def test_same_body_and_conv_skips_validation(db_session):
    """When body == conv there is nothing to re-validate — the value is
    returned as-is (no project row needed)."""
    assert (
        resolve_message_project_id(
            db_session, conv_project_id="p1", body_project_id="p1"
        )
        == "p1"
    )


def test_body_project_name_resolves_to_id(db_session):
    """Chips selected before the id-carrying fix (or any caller with
    only the name) still reach the project's configured LLM."""
    _add_project(db_session, "p1", "Global")
    assert (
        resolve_message_project_id(
            db_session,
            conv_project_id=None,
            body_project_id=None,
            body_project_name="Global",
        )
        == "p1"
    )


def test_body_project_name_case_insensitive(db_session):
    _add_project(db_session, "p1", "Marketing Team")
    assert (
        resolve_message_project_id(
            db_session,
            conv_project_id=None,
            body_project_id=None,
            body_project_name="marketing team",
        )
        == "p1"
    )


def test_unknown_body_project_name_falls_back_to_conv(db_session):
    assert (
        resolve_message_project_id(
            db_session,
            conv_project_id="p-conv",
            body_project_id=None,
            body_project_name="No Such Project",
        )
        == "p-conv"
    )


def test_body_name_not_queried_when_body_id_valid(db_session):
    """A valid body id short-circuits — the name (possibly stale) is
    never consulted."""
    _add_project(db_session, "p1", "Marketing Team")
    assert (
        resolve_message_project_id(
            db_session,
            conv_project_id=None,
            body_project_id="p1",
            body_project_name="Some Other Project",
        )
        == "p1"
    )


# ---------------------------------------------------------------------------
# AST regression guards — both chat handlers must route LLM resolution
# through the helper (project convention: any new conditional block in
# agents.py gets an AST test).
# ---------------------------------------------------------------------------

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise RuntimeError(f"{name} not found in agents.py")


def _calls(func, fname):
    return [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == fname
    ]


@pytest.mark.parametrize("handler", ["add_message", "add_message_stream"])
def test_handler_resolves_message_project_before_llm(handler):
    with open(_AGENTS_PATH) as f:
        tree = ast.parse(f.read())
    func = _find_func(tree, handler)

    helper_calls = _calls(func, "resolve_message_project_id")
    assert helper_calls, (
        f"{handler} must call resolve_message_project_id so the "
        f"per-message body project_id reaches LLM resolution"
    )
    llm_calls = _calls(func, "resolve_effective_llm")
    assert llm_calls, f"{handler} must call resolve_effective_llm"
    assert min(c.lineno for c in helper_calls) < min(c.lineno for c in llm_calls), (
        f"{handler}: resolve_message_project_id must run before "
        f"resolve_effective_llm"
    )


@pytest.mark.parametrize("handler", ["add_message", "add_message_stream"])
def test_handler_does_not_pass_bare_conv_project_id_to_llm(handler):
    """The LLM resolver must receive the helper's result, not raw
    ``conv.project_id`` (that was the bug — body override ignored)."""
    with open(_AGENTS_PATH) as f:
        tree = ast.parse(f.read())
    func = _find_func(tree, handler)

    for call in _calls(func, "resolve_effective_llm"):
        for kw in call.keywords:
            if kw.arg != "project_id":
                continue
            is_bare_conv_attr = (
                isinstance(kw.value, ast.Attribute)
                and kw.value.attr == "project_id"
                and isinstance(kw.value.value, ast.Name)
                and kw.value.value.id == "conv"
            )
            assert not is_bare_conv_attr, (
                f"{handler}: resolve_effective_llm(project_id=conv.project_id) "
                f"ignores the per-message body project_id — pass the "
                f"resolve_message_project_id result instead"
            )
