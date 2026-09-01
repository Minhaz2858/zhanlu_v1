"""Regression: AgentConversation.filter({project_id: X}) returns only X's
conversations, and the legacy {project: name} filter is correctly
treated as "no filter" (the model has no project column).

Why this exists
---------------
A user reported that the "Recent Chats" section on the Project Detail
page showed every conversation in the DB instead of just the ones
bound to the project. The cause was on the frontend
(frontend/src/pages/ProjectDetail.jsx): the loadAll() function
attempted a legacy ``{project: legacyName}`` fallback for
AgentConversation, but the AgentConversation model only has a
``project_id`` FK column — no ``project`` string column. The
backend's parse_query silently drops filters for non-existent
model fields, so the fallback returned ALL conversations, and
mergeFbk unioned them into the project-scoped list.

These tests pin both halves of the contract:

  1. The filter ``{project_id: X}`` returns exactly X's conversations
     (and not conversations with project_id=None or other ids).
  2. The filter ``{project: 'something'}`` (legacy fallback path) is
     a no-op — the query parser drops the unknown column, so the
     result is "no additional filter applied" which means ALL
     non-deleted rows. This is the bug the frontend was triggering
     accidentally. The test makes this behavior explicit so any
     future change to parse_query (e.g. raising on unknown columns)
     is caught here.
"""

import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.agent_conversation import AgentConversation
from app.services.entity_service import filter_records


@pytest.fixture
def db():
    """Fresh in-memory SQLite with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _make(db, *, agent_name="general_assistant", title="t",
          project_id=None, metadata=None):
    row = AgentConversation(
        agent_name=agent_name,
        title=title,
        messages=[],
        status="active",
        project_id=project_id,
    )
    if metadata is not None:
        row.metadata_ = metadata
    db.add(row)
    db.commit()
    return row


def test_filter_by_project_id_returns_only_matching_conversations(db):
    """The contract the Project Detail page relies on."""
    project_a = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    project_b = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    in_a_1 = _make(db, title="a-1", project_id=project_a)
    in_a_2 = _make(db, title="a-2", project_id=project_a)
    in_b = _make(db, title="b-1", project_id=project_b)
    no_proj = _make(db, title="none", project_id=None)

    rows = filter_records(
        AgentConversation, db,
        json.dumps({"project_id": project_a}),
        "-updated_date", 100, None,
    )
    ids = {r["id"] for r in rows}

    assert ids == {in_a_1.id, in_a_2.id}, (
        f"project_id={project_a} should return only its two conversations; "
        f"got {ids}"
    )
    # Defense: the unfiltered / other-project rows must not leak through.
    assert in_b.id not in ids
    assert no_proj.id not in ids


def test_filter_by_project_id_unbound_returns_only_null(db):
    """Conversations with project_id=None are returned by the
    {project_id: null} filter, NOT by the {project_id: <uuid>} filter.
    This is the "Ungrouped" bucket — they belong to no project and
    must never leak into a project's Recent Chats list."""
    project_a = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _make(db, title="in-a", project_id=project_a)
    _make(db, title="unbound-1", project_id=None)
    _make(db, title="unbound-2", project_id=None)

    rows = filter_records(
        AgentConversation, db,
        json.dumps({"project_id": project_a}),
        "-updated_date", 100, None,
    )
    titles = {r["title"] for r in rows}
    assert titles == {"in-a"}, f"unbound conversations leaked into project {project_a}: {titles}"

    # And the inverse: project_id=null returns only the unbound ones.
    rows = filter_records(
        AgentConversation, db,
        json.dumps({"project_id": None}),
        "-updated_date", 100, None,
    )
    titles = {r["title"] for r in rows}
    assert titles == {"unbound-1", "unbound-2"}


def test_legacy_project_string_filter_does_not_match_any_column(db):
    """The legacy frontend fallback used ``{project: legacyName}`` to
    find conversations tagged with the project name as a string. But
    the AgentConversation model has NO ``project`` column — only
    ``project_id`` (FK). The query parser MUST drop the unknown field
    rather than crash or 500.

    This is the silent-failure mode that caused the original bug: the
    fallback request went through, parse_query quietly dropped the
    ``project`` key (because ``getattr(model, 'project', None) is None``),
    and the resulting query had no WHERE clause — so it returned
    every conversation. The frontend's mergeFbk then unioned those
    into the project-scoped list.

    This test pins that contract: unknown columns are silently dropped
    (no error), which is the correct behavior — but it means the
    FRONTEND must not use that filter as a fallback for
    AgentConversation, because it degenerates to "no filter".
    """
    _make(db, title="a", project_id="x" * 36)
    _make(db, title="b", project_id="y" * 36)
    _make(db, title="c", project_id=None)

    # Filter on a column that doesn't exist on this model.
    rows = filter_records(
        AgentConversation, db,
        json.dumps({"project": "ecisco"}),  # not a model column
        "-updated_date", 100, None,
    )
    # parse_query drops the unknown key, so the result is "no WHERE
    # filter" — i.e. all 3 rows. Pin that behavior.
    assert len(rows) == 3, (
        f"Expected 3 rows (filter dropped because 'project' is not a "
        f"model column), got {len(rows)}. If this number changes, "
        f"parse_query has changed — update the frontend to not rely "
        f"on the legacy {project!r} fallback for AgentConversation."
    )

    # And a slightly more interesting case: a real filter combined
    # with a non-existent one. The real filter must still apply, the
    # non-existent one is dropped.
    rows = filter_records(
        AgentConversation, db,
        json.dumps({"project_id": "x" * 36, "project": "ecisco"}),
        "-updated_date", 100, None,
    )
    titles = {r["title"] for r in rows}
    assert titles == {"a"}, (
        f"project_id filter should win even when the bogus 'project' "
        f"key is present; got titles={titles}"
    )


def test_filter_respects_limit_and_sort(db):
    """Pagination + sort must work on the project_id filter, otherwise
    a project with >100 conversations will silently truncate. The
    Project Detail page asks for ``-updated_date, 100``."""
    pid = "p" * 36
    for i in range(5):
        _make(db, title=f"r{i}", project_id=pid)

    rows = filter_records(
        AgentConversation, db,
        json.dumps({"project_id": pid}),
        "-updated_date", 3, None,  # limit=3
    )
    assert len(rows) == 3
    # Most recent first (id was auto-incremented in insertion order).
    titles = [r["title"] for r in rows]
    assert titles[0] == "r4", f"expected most recent first, got {titles}"
