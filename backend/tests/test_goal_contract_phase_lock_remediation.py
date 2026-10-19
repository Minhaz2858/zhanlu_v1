"""Phase-lock remediation criterion — closing the deliverable dead-end.

Measured live failure (2026-10-19, session d7125064): the model tried to
build a Word report, `deliverable_phase_lock` blocked it (data incomplete
because `weekly_compare` died on a reserved-word alias), and the goal
contract's generic pending-action forcing steered the model toward
"answer with existing data" instead of REPAIRING the failed dataset. The
turn ended with a promise instead of the report.

Fix: the phase-lock block is recorded on the contract along with the failed
datasets, and a dedicated FIRST-priority criterion forces remediation —
re-run the failed dataset queries — before the deliverable can be attempted
again. The criterion auto-resolves the moment answer-tagged data lands.
"""

from __future__ import annotations

from app.services.goal_contract import (
    GoalContract,
    build_goal_contract,
)


def _contract_with_failed_data() -> GoalContract:
    c = build_goal_contract("give me last week sales as a word document")
    assert c.requires_data and c.deliverable == "docx"
    # The batch sub-query for weekly_compare failed with a syntax error.
    c.record_failed_dataset(
        label="weekly_compare",
        error="(1064, \"You have an error in your SQL syntax; near 'lines FROM'\")",
    )
    c.record_failed_dataset(label="monthly_trend", error="Unknown column 'x'")
    return c


# ── recording ───────────────────────────────────────────────────────────────


def test_record_failed_dataset_and_block():
    c = _contract_with_failed_data()
    c.record_phase_lock_block("run_sandbox_skill")
    assert len(c.failed_datasets) == 2
    assert c.phase_lock_blocks == ["run_sandbox_skill"]


def test_record_failed_dataset_dedupes():
    c = GoalContract()
    c.record_failed_dataset(label="weekly_compare", error="boom")
    c.record_failed_dataset(label="weekly_compare", error="boom")
    assert len(c.failed_datasets) == 1


def test_phase_lock_block_dedupes():
    c = GoalContract()
    c.record_phase_lock_block("run_sandbox_skill")
    c.record_phase_lock_block("run_sandbox_skill")
    assert c.phase_lock_blocks == ["run_sandbox_skill"]


def test_error_truncated_to_300_chars():
    c = GoalContract()
    c.record_failed_dataset(label="x", error="e" * 1000)
    assert len(c.failed_datasets[0]["error"]) <= 300


# ── the criterion ───────────────────────────────────────────────────────────


def test_remediation_fires_before_other_criteria():
    """While the block is unresolved, remediation outranks pending-action
    synthesis ("answer with existing data") and the deliverable force."""
    c = _contract_with_failed_data()
    c.record_phase_lock_block("run_sandbox_skill")
    c.refresh_pending_action("Exporting now — pulling one consolidated dataset.")
    tools = {"execute_query", "run_sandbox_skill", "create_artifact"}
    crits = c.unmet(tools)
    assert crits, "criterion must fire"
    assert crits[0].code == "phase_lock_remediation"
    assert crits[0].force_tool == "execute_query"
    # The message names the failure evidence, not a generic nudge.
    assert "weekly_compare" in crits[0].message
    assert "1064" in crits[0].message
    assert "run_sandbox_skill" in crits[0].message
    # Must instruct repair, not verbatim retry.
    assert "must be corrected, not retried verbatim" in crits[0].message


def test_remediation_resolves_once_answer_data_lands():
    c = _contract_with_failed_data()
    c.record_phase_lock_block("run_sandbox_skill")
    tools = {"execute_query", "run_sandbox_skill"}
    assert c.unmet(tools)[0].code == "phase_lock_remediation"

    # The repaired query lands as an answer-tagged dataset with real rows.
    c.record_dataset(
        rows=[{"week": "2026-10-12", "amount": 100}],
        purpose="answer",
    )
    crits = c.unmet(tools)
    assert all(cr.code != "phase_lock_remediation" for cr in crits)


def test_remediation_silent_without_block():
    c = _contract_with_failed_data()  # failures but no phase-lock block yet
    tools = {"execute_query"}
    assert all(cr.code != "phase_lock_remediation" for cr in c.unmet(tools))


def test_remediation_silent_when_block_condition_cleared():
    """has_answer_data() True (pre-block data exists) → the phase-lock would
    not even fire at the gate, so the criterion must not fire either."""
    c = _contract_with_failed_data()
    c.record_phase_lock_block("run_sandbox_skill")
    c.record_dataset(rows=[{"a": 1}], purpose="answer")
    assert c.unmet({"execute_query"}) == [] or all(
        cr.code != "phase_lock_remediation" for cr in c.unmet({"execute_query"})
    )


def test_remediation_respects_force_budget():
    c = _contract_with_failed_data()
    c.record_phase_lock_block("run_sandbox_skill")
    c.max_forces = 2
    c.forces_used = 2
    assert c.unmet({"execute_query"}) == []


def test_remediation_without_query_tool_is_skipped():
    c = _contract_with_failed_data()
    c.record_phase_lock_block("run_sandbox_skill")
    # No query tool granted → cannot force remediation; criterion skipped
    # (the generic deliverable force may still fire).
    crits = c.unmet({"run_sandbox_skill", "create_artifact"})
    assert all(cr.code != "phase_lock_remediation" for cr in crits)


# ── router wiring (source-level pin, same pattern as the collapse guards) ──


def test_router_wiring_pins():
    """The v3 loop must record phase-lock blocks + failed datasets and the
    criterion must exist in the contract — source-level pins."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "routers" / "agents.py"
    ).read_text()
    assert "record_phase_lock_block(" in src
    assert "record_failed_dataset(" in src

    contract_src = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "goal_contract.py"
    ).read_text()
    # Remediation must be checked BEFORE the generic criteria in unmet().
    unmet_pos = contract_src.index("def unmet(")
    rem_pos = contract_src.index("def _unmet_phase_lock_remediation")
    for generic in (
        "def _unmet_deliverable",
        "def _unmet_zero_rows",
        "def _unmet_metadata_only",
        "def _unmet_pending_action",
    ):
        assert contract_src.index(generic) > rem_pos or unmet_pos > contract_src.index(generic)
    # Priority order inside unmet(): remediation listed first.
    priority = contract_src[unmet_pos: unmet_pos + 900]
    assert priority.index("_unmet_phase_lock_remediation") < priority.index("_unmet_deliverable")
