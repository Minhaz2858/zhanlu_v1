"""Tests for the _perception_health_check scheduled task.

Verifies it logs a warning when the perception snapshot returns fewer than 3
products with prices — catching the silent-empty-snapshot regression at runtime.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

from app.services import scheduled_tasks


def test_health_check_warns_when_snapshot_has_few_products():
    empty_snapshot = {"products": {}}  # no prices -> regressed
    with patch(
        "app.services.perception.service.build_perception_snapshot",
        return_value=empty_snapshot,
    ):
        with patch.object(
            logging.getLogger("app.services.scheduled_tasks"), "warning"
        ) as warn:
            scheduled_tasks._perception_health_check()

    warn.assert_called()


def test_health_check_no_warning_when_snapshot_has_enough_products():
    good_snapshot = {
        "products": {
            "cracked_c5": {"latest_price": 130.0},
            "dcpd": {"latest_price": 95.0},
            "c5_resin": {"latest_price": 110.0},
        }
    }
    with patch(
        "app.services.perception.service.build_perception_snapshot",
        return_value=good_snapshot,
    ):
        with patch.object(
            logging.getLogger("app.services.scheduled_tasks"), "warning"
        ) as warn:
            scheduled_tasks._perception_health_check()

    warn.assert_not_called()
