"""Regression tests for the entity list ``fields`` projection.

The Base44 SDK's ``list(sort, limit, skip, fields)`` always supported a
``fields`` argument, but the generic entity service ignored it and shipped
every column — for the Tool entity that meant the full skill body
(``skill_md`` ~14KB/row) on every catalog list call. A chat skill picker
rendering the whole catalog (900+ rows) was pulling ~4MB of mostly-body
JSON. These tests pin the list-view contract: ``fields`` trims each row to
the requested subset (plus ``id``), while omitting ``fields`` keeps the
legacy full-row behavior.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.tool import Tool
from app.services.entity_service import filter_records, list_records


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_tool(db, name, body="x" * 2000, description="d", trigger=None):
    t = Tool(name=name, description=description, skill_md=body, trigger=trigger)
    db.add(t)
    db.commit()
    return t


def test_list_without_fields_keeps_full_rows():
    db = _session()
    _seed_tool(db, "full-row-skill", body="b" * 5000)
    rows = list_records(Tool, db, fields=None)
    assert len(rows) == 1
    assert "skill_md" in rows[0]
    assert "embedding" in rows[0]
    assert "description" in rows[0]


def test_list_with_fields_trims_heavy_columns():
    db = _session()
    _seed_tool(db, "trim-skill", body="b" * 5000, description="desc here")
    rows = list_records(
        Tool, db, fields=["name", "description", "trigger", "source"],
    )
    assert len(rows) == 1
    row = rows[0]
    # Requested columns present…
    assert row["name"] == "trim-skill"
    assert row["description"] == "desc here"
    assert row["source"] is not None  # model default stamped on flush
    # …heavy body columns are gone…
    assert "skill_md" not in row
    assert "embedding" not in row
    assert "references_manifest" not in row
    # …and id is always kept so row identity survives the projection.
    assert "id" in row


def test_filter_records_honors_fields_too():
    db = _session()
    _seed_tool(db, "keep-me", description="target")
    _seed_tool(db, "other", description="noise")
    rows = filter_records(
        Tool, db, q='{"description": "target"}',
        fields=["name", "created_by_id"],
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "keep-me"
    assert "description" not in rows[0]
    assert "id" in rows[0]


def test_unknown_field_names_are_ignored():
    db = _session()
    _seed_tool(db, "x")
    rows = list_records(Tool, db, fields=["name", "no_such_column"])
    assert len(rows) == 1
    assert "name" in rows[0]
    assert "no_such_column" not in rows[0]
    assert "id" in rows[0]
