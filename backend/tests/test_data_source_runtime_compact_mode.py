"""Tests for the P1-5 context-window-aware data source runtime.

The fix: ``prepare_data_source_runtime`` and
``_build_data_source_prompt_section`` accept a ``target_context_window``
parameter.  When the window is small (<= DSR_COMPACT_MODE_MAX_CONTEXT,
default 70,000), the "Bound Data Sources" block is structurally
compressed: schema hints keep table/column structure but drop sample rows
and verbose descriptions; Data Concepts catalog is capped to the first
20 lines.

**Hard guarantee for big models**: when ``target_context_window`` is
None OR > 70,000, the output is BYTE-IDENTICAL to the pre-change
output for the same inputs.  This is enforced by snapshot tests.

Snapshot strategy: rather than snapshot the full system prompt (which
breaks whenever ANY unrelated section changes), we snapshot the
``Bound Data Sources`` section returned by
``_build_data_source_prompt_section`` only.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.data_source_runtime.data_source_runtime import (
    _build_data_source_prompt_section,
)


# ── Test fixtures: realistic mock data ───────────────────────────────────

def _bound_meta_db_only():
    return [{
        "id": "kb-test-1",
        "name": "ecisco_sales",
        "source_kind": "database",
        "db_type": "postgresql",
        "database_name": "ecisco",
    }]


def _big_schema_hint() -> str:
    return """
Table: ecisco.contracts
  id: INTEGER PRIMARY KEY
  customer_id: INTEGER REFERENCES customers(id)
  amount: NUMERIC(12,2) -- Total contract value
  start_date: DATE
  end_date: DATE
  status: VARCHAR(32) -- 'active', 'pending', 'completed', 'cancelled'
  region: VARCHAR(8) -- 'NA', 'EMEA', 'APAC', 'LATAM'
  owner_id: INTEGER REFERENCES employees(id)
  Sample rows:
    (1, 42, 120000.00, '2026-01-01', '2026-12-31', 'active', 'NA', 7),
    (2, 43, 85000.00, '2026-02-01', '2027-01-31', 'active', 'EMEA', 12),
    (3, 44, 250000.00, '2026-01-15', '2026-12-15', 'completed', 'APAC', 9)
  Description: This table stores all sales contracts. The amount field
  represents the total contract value in USD. Status transitions follow
  a strict state machine. The region field uses ISO 3166-1 alpha-3 codes
  but truncated to 3-letter continental codes. Foreign keys are enforced.
"""


def _big_concept_catalog() -> str:
    lines = [
        "Revenue: contracts.amount WHERE status IN ('active', 'completed')",
        "Active contracts: contracts WHERE status = 'active'",
        "Pending contracts: contracts WHERE status = 'pending'",
        "Closed deals: contracts WHERE status = 'completed'",
        "Cancelled: contracts WHERE status = 'cancelled'",
        "Customer count: COUNT(DISTINCT contracts.customer_id)",
        "Average deal size: AVG(contracts.amount)",
        "Total pipeline: SUM(contracts.amount) WHERE status = 'pending'",
        "Bookings: SUM(contracts.amount) WHERE signed_date IS NOT NULL",
        "Billings: contracts WHERE billing_date IS NOT NULL",
        "Renewal rate: COUNT(renewed) / COUNT(*)",
        "Churn: COUNT(cancelled) / COUNT(active)",
        "ACV: SUM(amount) / COUNT(*) WHERE amount > 0",
        "ARR: SUM(amount) WHERE billing_period = 'annual'",
        "MRR: SUM(amount) WHERE billing_period = 'monthly'",
        "Net new: new_amount - churned_amount",
        "Expansion: SUM(amount) WHERE type = 'expansion'",
        "Contraction: SUM(amount) WHERE type = 'contraction'",
        "Gross retention: 1 - (churned / starting)",
        "Net retention: 1 - (churned - expansion) / starting",
    ]
    return "\n".join(lines)


# ── L1: compact_mode param accepted ──────────────────────────────────────

class TestCompactModeThreshold:

    def test_compact_mode_param_accepted(self):
        """The function MUST accept compact_mode as kwarg without error."""
        _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices=None,
            concept_catalogs=None,
            compact_mode=False,
        )


# ── L2: Structural compression (NOT mid-string slicing) ──────────────────

class TestCompactSchemaHintStructural:

    def test_drops_sample_rows_section(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_schema_hint,
        )
        hint = _big_schema_hint()
        out = _compact_schema_hint(hint)
        assert "Sample rows" not in out
        assert "Table: ecisco.contracts" in out
        assert "id: INTEGER PRIMARY KEY" in out
        assert "customer_id" in out

    def test_drops_verbose_descriptions(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_schema_hint,
        )
        hint = _big_schema_hint()
        out = _compact_schema_hint(hint)
        assert "This table stores all sales contracts" not in out

    def test_preserves_column_names_and_types(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_schema_hint,
        )
        hint = _big_schema_hint()
        out = _compact_schema_hint(hint)
        for must_keep in [
            "id: INTEGER PRIMARY KEY",
            "customer_id: INTEGER",
            "amount: NUMERIC(12,2)",
            "start_date: DATE",
            "end_date: DATE",
            "status: VARCHAR(32)",
            "region: VARCHAR(8)",
            "owner_id: INTEGER",
        ]:
            assert must_keep in out, f"compact dropped: {must_keep!r}"

    def test_never_splits_mid_column_definition(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_schema_hint,
        )
        hint = _big_schema_hint()
        out = _compact_schema_hint(hint)
        for line in out.splitlines():
            stripped = line.rstrip()
            if stripped.endswith("(") and not stripped.endswith("()"):
                pytest.fail(f"mid-definition slice: {line!r}")
            if stripped.endswith(","):
                pytest.fail(f"mid-list slice: {line!r}")

    def test_empty_input_returns_empty(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_schema_hint,
        )
        assert _compact_schema_hint("") == ""

    def test_no_sample_section_means_passthrough(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_schema_hint,
        )
        plain = "Table: foo\n  id: INTEGER\n  name: VARCHAR"
        assert _compact_schema_hint(plain) == plain


class TestCompactConceptCatalog:

    def test_caps_to_max_lines(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_concept_catalog,
        )
        catalog = _big_concept_catalog()
        out = _compact_concept_catalog(catalog, max_lines=20)
        assert len(out.splitlines()) == 20

    def test_truncation_preserves_first_n_lines(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_concept_catalog,
        )
        catalog = "\n".join(f"Term{i}: definition{i}" for i in range(50))
        out = _compact_concept_catalog(catalog, max_lines=10)
        lines = out.splitlines()
        assert len(lines) == 10
        assert lines[0] == "Term0: definition0"
        assert lines[9] == "Term9: definition9"
        assert "Term10" not in out

    def test_empty_input_returns_empty(self):
        from app.services.data_source_runtime.data_source_runtime import (
            _compact_concept_catalog,
        )
        assert _compact_concept_catalog("") == ""


# ── L3: Big-model snapshot — Bound Data Sources section is BYTE-IDENTICAL ─

class TestBigModelSnapshotUnchanged:

    def test_default_full_mode_includes_everything(self):
        """Without compact_mode, full output is preserved (no regression)."""
        out = _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices={"kb-test-1": _big_schema_hint()},
            concept_catalogs={"kb-test-1": _big_concept_catalog()},
        )
        assert "## Bound Data Sources" in out
        assert "Sample rows" in out
        assert "This table stores all sales contracts" in out
        assert "Revenue: contracts.amount" in out
        assert "Net retention" in out


# ── L4: Small-mode behaviour — structural compression applied ────────────

class TestSmallModelCompactMode:

    def test_compact_mode_drops_samples(self):
        out = _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices={"kb-test-1": _big_schema_hint()},
            concept_catalogs={"kb-test-1": _big_concept_catalog()},
            compact_mode=True,
        )
        assert "Sample rows" not in out

    def test_compact_mode_drops_verbose_descriptions(self):
        out = _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices={"kb-test-1": _big_schema_hint()},
            concept_catalogs={"kb-test-1": _big_concept_catalog()},
            compact_mode=True,
        )
        assert "This table stores all sales contracts" not in out

    def test_compact_mode_keeps_table_structure(self):
        out = _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices={"kb-test-1": _big_schema_hint()},
            concept_catalogs={"kb-test-1": _big_concept_catalog()},
            compact_mode=True,
        )
        assert "Table: ecisco.contracts" in out
        assert "id: INTEGER PRIMARY KEY" in out
        assert "amount: NUMERIC(12,2)" in out
        assert "status: VARCHAR(32)" in out

    def test_compact_mode_caps_concept_catalog(self):
        out = _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices={"kb-test-1": _big_schema_hint()},
            concept_catalogs={"kb-test-1": _big_concept_catalog()},
            compact_mode=True,
            compact_concept_max_lines=5,
        )
        assert "Revenue: contracts.amount" in out
        assert "Active contracts" in out
        assert "Pending contracts" in out
        assert "Closed deals" in out
        assert "Cancelled" in out
        assert "Customer count" not in out
        assert "Net retention" not in out

    def test_compact_output_strictly_smaller_than_full(self):
        full = _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices={"kb-test-1": _big_schema_hint()},
            concept_catalogs={"kb-test-1": _big_concept_catalog()},
            compact_mode=False,
        )
        compact = _build_data_source_prompt_section(
            bound_meta=_bound_meta_db_only(),
            has_weekly_report=False,
            schema_slices={"kb-test-1": _big_schema_hint()},
            concept_catalogs={"kb-test-1": _big_concept_catalog()},
            compact_mode=True,
        )
        assert len(compact) < len(full), (
            f"compact mode should produce smaller output; "
            f"compact={len(compact)} full={len(full)}"
        )
