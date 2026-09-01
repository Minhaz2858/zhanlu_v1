"""T19 BUG B regression: the real ``_create_fullstack_dashboard`` write path.

Smoke test #5 (2026-08-21) exposed a P0: ``create_fullstack_dashboard`` ran
~20-34s of generation work, then crashed at the post-generation file
verification with ``NameError: name 'Path' is not defined``
(``dashboard_tools.py`` used ``Path("dist") / "index.html"`` without
importing ``pathlib.Path``). The tool returned a visible error and the model
retried into the same crash, then the turn fell through to the EDIA path.

Every other dashboard test mocks away the generator/manager, so no test
exercised the real write path. This integration test runs the REAL
``DashboardAppGenerator`` against a ``tmp_path`` filesystem (only the KB
access probe, DB record, and mount side-effects are stubbed) and asserts the
expected artifact files land on disk — it would have failed instantly on the
missing ``Path`` import.
"""
import json
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services.dashboard_app.generator import TEMPLATE_DIR, DashboardAppGenerator
from app.services.tool_handlers.dashboard_tools import _create_fullstack_dashboard

MIN_SPEC = {
    "name": "Sales Overview",
    "slug": "sales-overview",
    "datasource_id": "ds-test",
    "scope": "personal",
    "theme": "dark",
    "metrics": [
        {
            "id": "kpi_orders",
            "type": "kpi",
            "title": "Orders",
            "sql": "SELECT 1 AS x",
            "options": {},
        }
    ],
}


@pytest.mark.asyncio
async def test_create_fullstack_dashboard_writes_real_files(tmp_path, monkeypatch):
    """Real generator run: files land on disk and the tool reports success."""
    monkeypatch.setattr(settings, "FULLSTACK_DASHBOARD_ENABLED", True)
    apps_dir = tmp_path / "apps"

    # KB access probe → dummy (truthy, exposes .name for the result dict).
    monkeypatch.setattr(
        "app.services.tool_handlers.dashboard_tools._user_can_access_kb",
        lambda *a, **k: MagicMock(name="datasource"),
    )

    # REAL generator, but pointed at tmp_path so we never touch the prod apps
    # dir. The actual file-writing code path runs untouched.
    monkeypatch.setattr(
        "app.services.dashboard_app.generator.get_generator",
        lambda: DashboardAppGenerator(template_dir=TEMPLATE_DIR, apps_dir=apps_dir),
    )

    # Manager side-effects (DB row, mount, versioning) are out of scope here.
    manager = MagicMock()
    manager.create_app_record.return_value = MagicMock(id=1)
    monkeypatch.setattr(
        "app.services.dashboard_app.manager.dashboard_app_manager", manager,
    )

    # db is only used for the "existing record?" probe + the try/except-
    # swallowed ArtifactService call.
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    result = await _create_fullstack_dashboard(
        dict(MIN_SPEC), db, user_id="u-test", context={},
    )

    assert result["success"] is True, result

    app_dir = apps_dir / "sales_overview"
    assert app_dir.exists(), "app dir missing on disk"
    for rel in ("config.json", "api.py", "queries.py", "realtime.py"):
        assert (app_dir / rel).exists(), f"{rel} missing on disk"

    # Frontend dist copied from the real template (regression: the missing
    # Path import crashed HERE, before any of the assertions could run).
    assert (app_dir / "dist" / "index.html").exists(), "dist/index.html missing"

    cfg = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
    assert cfg["slug"] == "sales-overview"
    assert cfg["metrics"], "config.json carries no metrics"

    assert result["artifact"]["source"] == "dashboard_app"
    assert result["dashboard_app"]["app_url"] == "/api/dashboards/apps/sales-overview/"
