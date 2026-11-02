"""Phase 7C T14 — audit event registry ↔ writer reconciliation.

The registry documents the written vocabulary (legacy entries kept).
The contract test asserts every STATICALLY-KNOWN writer event_type /
event_source / outcome is registered, so future writers that introduce a
new value must register it (or consciously extend this test's dynamic
families).
"""

import re
from pathlib import Path

from app.models.audit_event import (
    AUDIT_EVENT_SOURCES, AUDIT_EVENT_TYPES, AUDIT_OUTCOMES)

BACKEND = Path(__file__).resolve().parents[1]


def _literal_values(pattern: str) -> set[str]:
    values = set()
    for py in BACKEND.glob("app/**/*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(pattern, text):
            values.add(m.group(1))
    return values


def test_all_written_event_types_are_registered():
    written = _literal_values(r'event_type="([a-z0-9_.\-]+)"')
    unknown = written - set(AUDIT_EVENT_TYPES)
    assert not unknown, f"unregistered event_type(s): {sorted(unknown)}"


def test_all_written_event_sources_are_registered():
    written = _literal_values(r'event_source="([a-z0-9_.\-]+)"')
    unknown = written - set(AUDIT_EVENT_SOURCES)
    assert not unknown, f"unregistered event_source(s): {sorted(unknown)}"


def test_all_written_outcomes_are_registered():
    written = _literal_values(r'\boutcome="([a-z0-9_.\-]+)"')
    unknown = written - set(AUDIT_OUTCOMES)
    assert not unknown, f"unregistered outcome(s): {sorted(unknown)}"


def test_dynamic_families_are_documented():
    src = (BACKEND / "app/models/audit_event.py").read_text(
        encoding="utf-8")
    assert "{resource_type}_{decision}" in src  # _record_review family
    assert "{confirmed, rejected, enabled, disabled, resubmitted}" in src


def test_legacy_registered_types_remain():
    # legacy entries are kept so historical audit rows stay interpretable
    for legacy in ("datasource_connect", "skill_create", "template_upload",
                   "learning_apply", "system_config"):
        assert legacy in AUDIT_EVENT_TYPES
