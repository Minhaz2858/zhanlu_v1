"""CadRevision model (roadmap P3) — revision store with approval gate."""
from app.models.base import TimestampedBase


def test_revision_table_registered():
    assert "cad_revisions" in TimestampedBase.metadata.tables


def test_revision_columns():
    t = TimestampedBase.metadata.tables["cad_revisions"]
    cols = {c.name: c for c in t.columns}
    for required in ("id", "org_id", "app_id", "conversation_id", "agent_id",
                     "revision_no", "parent_revision_id", "title",
                     "change_summary", "status", "before_snapshot",
                     "after_snapshot", "impact_report", "approved_by_id",
                     "created_date"):
        assert required in cols, f"missing column {required}"
    fk = t.columns["conversation_id"].foreign_keys
    assert fk and list(fk)[0].ondelete == "CASCADE"
    assert t.columns["org_id"].nullable is False
    assert t.columns["app_id"].nullable is False
