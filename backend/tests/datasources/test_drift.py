"""Tests for schema-drift detection."""

import pytest
from app.services.datasources.drift import detect


class TestSchemaDrift:
    def test_detect_no_change(self):
        stored = {"users": [{"name": "id", "dtype": "INTEGER"}, {"name": "name", "dtype": "TEXT"}]}
        live = {"users": [{"name": "id", "dtype": "INTEGER"}, {"name": "name", "dtype": "TEXT"}]}
        result = detect(stored, live)
        assert result["added_cols"] == []
        assert result["removed_cols"] == []
        assert result["type_changed"] == []

    def test_detect_added_column(self):
        stored = {"users": [{"name": "id", "dtype": "INTEGER"}]}
        live = {"users": [{"name": "id", "dtype": "INTEGER"}, {"name": "email", "dtype": "TEXT"}]}
        result = detect(stored, live)
        assert ("users", "email") in [(c["table"], c["column"]) for c in result["added_cols"]]

    def test_detect_removed_column(self):
        stored = {"users": [{"name": "id", "dtype": "INTEGER"}, {"name": "name", "dtype": "TEXT"}]}
        live = {"users": [{"name": "id", "dtype": "INTEGER"}]}
        result = detect(stored, live)
        assert ("users", "name") in [(c["table"], c["column"]) for c in result["removed_cols"]]

    def test_detect_type_changed(self):
        stored = {"users": [{"name": "id", "dtype": "INTEGER"}]}
        live = {"users": [{"name": "id", "dtype": "TEXT"}]}
        result = detect(stored, live)
        assert ("users", "id") in [(c["table"], c["column"]) for c in result["type_changed"]]

    def test_detect_new_table(self):
        stored = {"users": [{"name": "id", "dtype": "INTEGER"}]}
        live = {"users": [{"name": "id", "dtype": "INTEGER"}],
                "orders": [{"name": "id", "dtype": "INTEGER"}]}
        result = detect(stored, live)
        assert ("orders",) in [(c["table"],) for c in result["added_cols"]]

    def test_detect_empty_stored_returns_all_added(self):
        result = detect({}, {"users": [{"name": "id", "dtype": "INTEGER"}]})
        # All columns are new when there was no stored schema
        assert ("users", "id") in [(c["table"], c["column"]) for c in result["added_cols"]]
        assert result["removed_cols"] == []

    def test_empty_live_detects_removed(self):
        stored = {"users": [{"name": "id", "dtype": "INTEGER"}]}
        result = detect(stored, {})
        assert ("users", "id") in [(c["table"], c["column"]) for c in result["removed_cols"]]
