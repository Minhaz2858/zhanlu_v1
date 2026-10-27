# Query Result Contract — Design and Remediation Plan

**Date:** 2026-10-27
**Status:** APPROVED (Option C, with five adjustments) — implementation pending
**Branch:** `fix/automation-chat-artifacts-ordering`
**Owner:** —

---

## 0. Executive summary

A user asked "give me last week sales report please". The turn ran for **4.5+ minutes**,
executed **18 SQL queries** in **253 seconds**, re-ran the *same three questions six times
each*, and the agent still never answered.

The database was never slow: the bound warehouse (`aipdp_data_warehouse_prod`, KB
`74c8d56d…`) answers in **~0.9–1.4 s**. The NL2SQL pipeline was never broken: **every
observation in the live database is `success = t`** with real rows and a narrated answer.

The failure is at the **tool-result boundary**: the SQL executed successfully, the success
was lost during result extraction, and the agent was then instructed by the platform to do
the work again.

This is a **correctness** bug: execution status and tool-call status are conflated. It
affects control flow, user trust, and observability. It is prioritized above further
semantic-enrichment or table-selection work.

---

## 1. Root causes (verified in code and live data)

| # | Defect | Location | Evidence |
|---|---|---|---|
| **RC-1** | `ExecutionResult` carries no `observations` and no `execution` attribute; the extractor therefore always sees `[]` and reports "no query executed" | `backend/app/services/tool_handlers/query_database_tool.py:309-313` | Live `observation_records`: all nl2sql observations `success = t`, 10 rows each; `result_data` contains `data` |
| **RC-2** | Timing key mismatch: reader expects `duration_ms`, executor returns `elapsed_ms` | reader `backend/app/services/db/nl_answer_service.py:1293,1345`; writer `backend/app/services/db/query_service.py:250` | `nl2sql_query_logs.duration_ms` is NULL on 100% of rows |
| **RC-3** | Unknown errors default to **retryable** → a permanent result-contract failure triggers a full NL2SQL re-execution | `backend/app/services/agent_tools.py:322-352` (`return True` fallthrough) | 3× `attempt 1/2 failed (The NL2SQL pipeline did not execute any query…)` in one turn |
| **RC-4** | Loop guard counts **only successful** calls, so it is structurally blind to repeated identical *failures* | `backend/app/services/reliability/__init__.py:115-117` | 18 executions / 253 s; same question 6× |

### 1.1 Why RC-1 is silent

`ExecutionResult` (`backend/app/services/synexia/fsm.py:246-275`) is a plain pydantic
`BaseModel` with **no** `observations` and **no** `execution` field. The extractor uses
`getattr(result, "observations", None)` with a `None` default, so the mismatch raises no
exception and logs no warning. The real observations live on the `ObservationRecord` rows
keyed by `result.execution_id` (`backend/app/models/execution.py:141-164`,
`Execution.observations` relationship at `:77`).

**RC-1 is deterministic**: `query_database` can never succeed on the FSM path, while
`ask_data_agent` (a different path) works. This explains the previously observed
"`ask_data_agent` returned real rows three times while `query_database` returned 0".

### 1.2 Provenance hazard

The failure hint tells the model: *"Do NOT tell the user there are no matching rows. Retry
with `ask_data_agent`."* When the model complies, `ask_data_agent` executes **independently**
and may return different figures for an already-successful execution whose result was
discarded. This is a provenance violation, not merely wasted time.

---

## 2. Design principles

1. **One shared boundary.** A single normalizer is the only place a tool result becomes
   `success`. No caller re-derives it.
2. **Three separate concepts.** `execution_status`, `result_status`, and the derived
   `success` are distinct. A zero-row query is a success. An extraction failure after SQL
   execution is not.
3. **`success` is computed, never assigned.** Contradictory combinations must be
   unconstructible.
4. **Never re-execute SQL to recover an existing result.** Recovery reads the durable
   observation rows by execution id only.
5. **Fail loud on contract violation.** Replace silent `getattr(..., None)` fallbacks with
   explicit typed failures — silence is how this bug survived.
6. **Retry safety is independent of error wording.** Typed classification takes precedence
   over message-string matching and over the unknown-error default.

### 2.1 Approved adjustments (from review)

- **A1 — `success` is a computed property**, derived from `execution_status` + `result_status`.
- **A2 — Recovery never re-executes.** If recovery fails → typed extraction failure with
  preserved `execution_id` and provenance.
- **A3 — Retry safety separated from wording.** Typed failures take precedence over the
  unknown-error default; a successful execution is never retried because a downstream
  representation is missing.
- **A4 — Deduplicate *before* expensive work.** Initial in-flight dedup uses a request
  fingerprint (tenant + source + tool + normalized args) because the SQL fingerprint does
  not exist until after generation. The SQL fingerprint is then used to *verify*
  equivalence. No unsafe sharing of a DB session across concurrent tasks.
- **A5 — Phased delivery.** P0 (correctness) before P1 (orchestration + observability).

---

## 3. Result contract

New module: `backend/app/services/db/result_contract.py`

```python
class FailureKind(str, Enum):
    NONE = "none"
    TRANSIENT_EXECUTION_FAILURE = "transient_execution_failure"
    PERMANENT_BINDING_FAILURE = "permanent_binding_failure"
    RESULT_EXTRACTION_FAILURE = "result_extraction_failure"


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_EXECUTED = "not_executed"


class ResultStatus(str, Enum):
    ASSEMBLED = "assembled"
    EMPTY = "empty"
    EXTRACTION_FAILED = "extraction_failed"


@dataclass(frozen=True)
class NormalizedQueryResult:
    execution_status: ExecutionStatus
    result_status: ResultStatus
    failure_kind: FailureKind
    rows: list[dict]
    sql: str | None
    source_id: str | None
    source_name: str | None
    complete: bool
    warnings: list[str]
    provenance: dict
    diagnostic: str | None

    @property
    def success(self) -> bool:
        """COMPUTED (A1) — the single authoritative success decision."""
        return (
            self.execution_status is ExecutionStatus.SUCCEEDED
            and self.result_status in (ResultStatus.ASSEMBLED, ResultStatus.EMPTY)
        )

    @property
    def row_count(self) -> int:
        return len(self.rows)
```

### 3.1 Success truth table

| execution_status | result_status | success | failure_kind | retry |
|---|---|---|---|---|
| SUCCEEDED | ASSEMBLED | **True** | NONE | never |
| SUCCEEDED | EMPTY (0 rows) | **True** | NONE | never |
| SUCCEEDED | EXTRACTION_FAILED | **False** | RESULT_EXTRACTION_FAILURE | recover only, **no re-exec** |
| FAILED | — | False | TRANSIENT_EXECUTION_FAILURE | bounded (2) |
| NOT_EXECUTED (bad binding) | — | False | PERMANENT_BINDING_FAILURE | **never** |

`complete` is orthogonal to `success`: a truncated-but-real result is a success with
`complete = False`.

### 3.2 Extraction — the RC-1 fix

```python
def extract_observations(result, db) -> tuple[list, str | None]:
    """Return (observations, execution_id). Never fabricates an empty list
    as if it were a valid result."""
    obs = list(getattr(result, "observations", None) or [])
    if obs:
        return obs, getattr(result, "execution_id", None)

    execution_id = getattr(result, "execution_id", None)
    if not execution_id:
        return [], None            # caller → RESULT_EXTRACTION_FAILURE

    rows = (
        db.query(ObservationRecord)
        .filter(ObservationRecord.execution_id == execution_id)
        .order_by(ObservationRecord.seq.asc())
        .all()
    )
    return list(rows), execution_id
```

- Uses the durable source of truth (`ObservationRecord` by `execution_id`).
- Returns an explicit `None` execution id instead of silently returning `[]`.
- **No SQL re-execution** on any path (A2).

### 3.3 Normalizer

`normalize_query_result(result, db) -> NormalizedQueryResult` maps observations →
statuses. It is the **only** producer of `success` in the tool layer.

---

## 4. Retry classification (A3)

`query_database` emits `failure_kind` (already an established convention —
`agent_tools.py:288`, `tool_retry.py:128`), and `_is_retryable` consults it **before** any
message-string matching:

```python
if result.get("failure_kind") in _NON_RETRYABLE_KINDS:   # permanent, extraction
    return False
# … existing marker checks …
return True   # unknown-default unchanged, but unreachable for typed DB failures
```

The unknown-error default is preserved for genuinely unknown conditions; typed database
failures simply never reach it.

**Honesty invariant:** an extraction failure is never converted into a success by a
retry, and a genuine execution failure is never converted into a success by recovery.

---

## 5. In-flight deduplication (A4)

- **Request fingerprint** — computed *before* any expensive work:
  `sha256(tool | sorted(normalized_args) | org_id | kb_ids | app_id | conversation_id)`.
- **SQL fingerprint** — computed *after* generation, used only to *verify* equivalence.
- Two concurrent requests share a result **only if** both fingerprints and the
  authorization context (tenant/source) match.
- Sharing is **in-flight and same-turn only**. Equivalent SQL is *not* an indefinite cache:
  future turns re-read the database.
- No DB session is shared across concurrent tasks — the follower awaits the leader's
  completed result object.

---

## 6. Observability (RC-2 + staged timing)

- Fix `duration_ms ← elapsed_ms` at both call sites
  (`nl_answer_service.py:1293` and `:1345`).
- Populate the existing but unused `ObservationRecord.duration_ms`.
- Emit structured stage timings: semantic planning, SQL generation/validation, DB
  execution, result extraction/assembly, retry/backoff, final tool response.
- Record `execution_status`, `result_status`, `retry_count`, `row_count`, `complete`,
  `query_fingerprint`.
- **Never log credentials or row contents** — counts, timings, and identifiers only.

---

## 7. Regression tests (all 10 required)

| # | Test case | Required outcome | Phase |
|---|---|---|---|
| 1 | SQL executes; canonical result has rows; observations empty | Success, **no re-execution** | P0 |
| 2 | SQL executes; result extraction fails | Typed extraction failure; no blind full-pipeline retry | P0 |
| 3 | Binding genuinely invalid | Permanent failure, no transient retry | P0 |
| 4 | DB connection times out before execution | Bounded retry (transient) | P0 |
| 5 | Same tool arguments repeat six times | Loop guard detects the repetition | P1 |
| 6 | Three identical concurrent calls | At most one execution per in-flight fingerprint | P1 |
| 7 | Valid execution returns zero rows | Success with zero rows, not execution failure | P0 |
| 8 | Result incomplete or truncated | Success and completeness reported separately | P0 |
| 9 | SQL succeeds but UI plan step fails | Result preserved; orchestration diagnosed separately | P1 |
| 10 | Same query via `ask_data_agent` and `query_database` | Same canonical contract; rows, completeness, source identity, provenance agree; one execution for equivalent same-turn requests; no sharing across different authorization contexts; no cross-turn cache reuse | P1 |

Test 1 is the regression that would have caught RC-1.

---

## 8. Implementation gates

### P0 — Correctness
- Add `result_contract.py` (computed `success`, typed statuses).
- Fix extraction from the actual `ExecutionResult` / `ObservationRecord` source.
- Establish one authoritative success decision.
- Correct `elapsed_ms` vs `duration_ms`.
- Make extraction and binding failures non-retryable as full SQL executions.
- **Gate: Tests 1–4 and 7–8 green.**

### P1 — Orchestration and observability
- Count failed repetitions as well as successful ones.
- Safe in-flight deduplication (request fingerprint first, SQL fingerprint verify).
- Populate execution timing and result metadata.
- Verify the UI plan state does not erase a valid query result.
- **Gate: Tests 5, 6, 9, 10 green.**

**Both gates require:** the full existing NL2SQL and agent-reliability suites remain green.
No production database mutation is needed — deterministic test doubles for retries,
concurrency, and execution outcomes.

---

## 9. Scope discipline

- Preserve `ask_data_agent` user-visible behavior; shared normalization must not redesign it.
- Avoid unnecessary schema migrations (`duration_ms` columns already exist).
- Do not expand beyond the identified root causes without new evidence.
- The file-count estimate (≈2 new modules, ≈4 existing files, 600–700 LOC) is a guide, not
  a constraint: if the real call graph demands a different, smaller and safer change, take
  it.

## 10. Rollback

Revert the P0 commit, then the P1 commit — they are independent. No migration rollback is
required. Existing flags are unaffected.

---

## 11. Status

Approved for implementation. P0 first; P1 only after P0's gate passes.

> This document records the *design and delivery direction*. It does not assert that the
> repository has been changed or that any test has passed.
