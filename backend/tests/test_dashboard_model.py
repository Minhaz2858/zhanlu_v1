import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
from app.models.dashboard import Dashboard
from app.models.base import TimestampedBase


def test_dashboard_inherits_timestamped_base():
    assert issubclass(Dashboard, TimestampedBase)


def test_dashboard_has_required_columns():
    cols = {c.name for c in Dashboard.__table__.columns}
    required = {
        "id", "created_date", "updated_date", "created_by_id", "is_deleted",
        "org_id", "app_id",  # from TimestampedBase
        "project_id", "project", "datasource_kb_id", "name", "description",
        "definition", "refresh_interval_seconds",
    }
    missing = required - cols
    assert not missing, f"missing columns: {missing}"


def test_dashboard_tablename():
    assert Dashboard.__tablename__ == "dashboards"


def test_dashboard_defaults():
    assert Dashboard.__table__.c.refresh_interval_seconds.default.arg == 30
    assert Dashboard.__table__.c.project.default.arg == "global"
    assert Dashboard.__table__.c.is_deleted.default.arg is False
