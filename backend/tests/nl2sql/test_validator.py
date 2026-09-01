"""Tests for the sqlglot-based SQL validator."""

import pytest
from app.services.nl2sql.validator import validate, ValidationResult


class TestValidateBasicRules:
    def test_empty_sql_rejected(self):
        result = validate("")
        assert result.is_valid is False
        assert any("Empty" in e for e in result.errors)

    def test_valid_select_passes(self):
        result = validate("SELECT * FROM users")
        assert result.is_valid is True
        assert result.tables_referenced == ["users"]

    def test_insert_rejected(self):
        result = validate("INSERT INTO users VALUES (1)")
        assert result.is_valid is False
        assert any("SELECT" in e for e in result.errors)

    def test_delete_rejected(self):
        result = validate("DELETE FROM users WHERE id=1")
        assert result.is_valid is False

    def test_drop_rejected(self):
        result = validate("DROP TABLE users")
        assert result.is_valid is False

    def test_multi_statement_blocked(self):
        result = validate("SELECT * FROM users; DROP TABLE users;")
        assert result.is_valid is False

    def test_generates_sql_hash(self):
        result = validate("SELECT 1")
        assert len(result.sql_hash) == 16
        # Same SQL → same hash
        r2 = validate("SELECT 1")
        assert result.sql_hash == r2.sql_hash


class TestAllowList:
    def test_allow_list_blocks_foreign_table(self):
        result = validate(
            "SELECT * FROM secret_data",
            allowed_tables=["users", "orders"],
        )
        assert result.is_valid is False
        assert any("secret_data" in e for e in result.errors)

    def test_allow_list_allows_known_table(self):
        result = validate(
            "SELECT * FROM users WHERE id=1",
            allowed_tables=["users", "orders"],
        )
        assert result.is_valid is True

    def test_allow_list_case_insensitive(self):
        result = validate(
            "SELECT * FROM Users",
            allowed_tables=["users"],
        )
        assert result.is_valid is True

    def test_column_warning_on_unknown(self):
        result = validate(
            "SELECT super_secret FROM users",
            allowed_columns=["id", "name", "email"],
        )
        # Column warnings should not block (just warn)
        assert len(result.warnings) > 0


class TestBlockList:
    def test_block_list_rejects_table(self):
        result = validate(
            "SELECT * FROM passwords",
            block_tables=["passwords", "secrets"],
        )
        assert result.is_valid is False
        assert any("blocked" in e.lower() for e in result.errors)

    def test_block_list_allows_other_tables(self):
        result = validate(
            "SELECT * FROM users",
            block_tables=["passwords"],
        )
        assert result.is_valid is True


class TestExtractReferenced:
    def test_extracts_single_table(self):
        result = validate("SELECT * FROM orders")
        assert "orders" in result.tables_referenced

    def test_extracts_joined_tables(self):
        result = validate("SELECT * FROM users u JOIN orders o ON u.id = o.user_id")
        assert "users" in result.tables_referenced
        assert "orders" in result.tables_referenced

    def test_extracts_columns(self):
        result = validate("SELECT id, name, email FROM users")
        assert "id" in result.columns_referenced
        assert "name" in result.columns_referenced

    def test_qualified_columns(self):
        result = validate("SELECT u.id, u.name FROM users u")
        assert "u.id" in [c.lower() for c in result.columns_referenced]


class TestDangerousFunctions:
    @pytest.mark.parametrize("sql", [
        "SELECT pg_sleep(10)",
        "SELECT * FROM pg_tables WHERE pg_terminate_backend(1) = 1",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM dblink('dbname=foo','SELECT 1') AS t(x int)",
        "SELECT lo_import('/etc/passwd')",
        "SELECT lo_export(12345, '/tmp/out')",
    ])
    def test_validator_rejects_dangerous_functions(self, sql):
        result = validate(sql)
        assert not result.is_valid, f"Expected {sql!r} to be rejected"
        assert any("Forbidden" in e or "dangerous" in e.lower() for e in result.errors), (
            f"Errors: {result.errors}"
        )

    def test_valid_select_without_dangerous_functions_passes(self):
        result = validate("SELECT count(*), avg(amount) FROM orders")
        assert result.is_valid is True
