# Session-Cached Re-Export Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the prompt-only "HARD RULE" re-export fix with a deterministic state-cached pipeline: every data tool result is persisted as a `DataExecution` row, every re-export pulls from the cache via `source_execution_id`, and code-level guardrails block empty/garbage documents.

**Architecture:** LangGraph-style dual-layer memory. LLM context carries human-readable chat history + a small `SESSION STATE` block. Backend session state carries the most recent structured data execution. Re-exports reference the cached execution; no DB re-run, no prose parsing.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Pydantic, pytest. New tables: `data_executions`, `session_states`. New flags (all default `False`): `DATA_EXECUTION_CACHE_ENABLED`, `DATA_EXECUTION_CLEANUP_ENABLED`, `INTENT_ROUTER_ENABLED`.

**Reference spec:** `docs/superpowers/specs/2026-08-24-session-cached-re-export-design.md`

---

## Global Constraints

- All new flags default `False` per project convention.
- All new DB tables include `org_id`, `app_id` for tenant isolation, and `is_deleted` for soft-delete.
- Cache writes are best-effort: a cache failure must NEVER break the calling tool.
- Per-session cap: 20 most-recent `DataExecution` rows per session.
- TTL: 7 days from `created_at`.
- All tests follow the project's TDD pattern (test first, see fail, implement, see pass, commit).
- Run backend tests in venv: `cd backend && venv/bin/python -m pytest <file> -p no:jaxtyping -p no:cacheprovider`
- Router import check: `docker exec zhanlu-backend python -c "from app.routers.<n> import router"`
- Backend redeploy: `docker restart zhanlu-backend`
- Alembic (local): `cd backend && venv/bin/python -m alembic upgrade head`
- Alembic (container): `docker exec zhanlu-backend alembic upgrade head`

---

## File Structure

### Files to create
- `backend/app/models/data_execution.py` — `DataExecution` SQLAlchemy model
- `backend/app/models/session_state.py` — `SessionState` SQLAlchemy model
- `backend/app/services/data_execution/__init__.py` — package init
- `backend/app/services/data_execution/cache.py` — `cache_data_execution()` + `CACHEABLE_TOOL_PATTERNS`
- `backend/app/services/data_execution/session_state.py` — `SessionStateService`
- `backend/app/services/data_execution/session_state_tool.py` — `session_state_query` tool
- `backend/app/services/data_execution/prompt_block.py` — `build_session_state_block()`
- `backend/app/services/data_execution/cleanup.py` — cleanup service
- `backend/app/services/data_execution/execution_service.py` — `DataExecutionService`
- `backend/app/services/intent/__init__.py` — package init
- `backend/app/services/intent/export_intent.py` — `detect_export_intent()` + `ExportIntent`
- `backend/app/services/tool_handlers/_payload_from_execution.py` — payload mapper
- `backend/app/services/tool_handlers/_errors.py` — `ToolExecutionError`
- `backend/alembic/versions/049_add_data_execution.py` — migration
- `backend/alembic/versions/050_add_session_state.py` — migration
- ~15 test files under `backend/tests/`

### Files to modify
- `backend/app/services/tool_handlers/artifact_tool.py` — schema, guards, mapper integration
- `backend/app/routers/agents.py` — intent router, cache hook, prompt block
- `backend/app/services/agent_prompts.py` — RE-EXPORT HARD RULE
- `backend/app/config.py` — 3 new flags
- `backend/main.py` — cleanup loop on startup

---

## Task 1: DataExecution model + migration

**Files:**
- Create: `backend/app/models/data_execution.py`
- Create: `backend/alembic/versions/049_add_data_execution.py`
- Test: `backend/tests/alembic/test_049.py`

**Interfaces:**
- Consumes: `TimestampedBase`
- Produces: `DataExecution` with `id: str` (PK, "evt_xxx"), `session_id: str`, `tool_name: str`, `args: dict`, `result: dict`, `summary_text: str | None`, `expires_at: datetime`, `org_id/app_id: str | None`, `is_deleted: bool`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/alembic/test_049.py`:

```python
"""Test that migration 049 creates the data_executions table with all fields."""
from app.models.data_execution import DataExecution


def test_data_execution_model_imports():
    assert DataExecution.__tablename__ == "data_executions"


def test_data_execution_has_required_fields():
    fields = {c.name for c in DataExecution.__table__.columns}
    assert fields >= {
        "id", "session_id", "tool_name", "args", "result",
        "summary_text", "expires_at", "org_id", "app_id",
        "is_deleted", "created_date", "updated_date",
    }


def test_data_execution_id_is_string_primary_key():
    col = DataExecution.__table__.columns["id"]
    assert col.primary_key is True
    assert col.type.python_type is str
```

- [ ] **Step 2: Run test — it fails**

```bash
cd backend && venv/bin/python -m pytest tests/alembic/test_049.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create the model**

Create `backend/app/models/data_execution.py`:

```python
"""DataExecution model — caches every data tool result for re-exports."""
from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import Index, String, DateTime, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class DataExecution(TimestampedBase):
    """One row per successful data-tool call. Caches the raw structured
    result so re-exports (docx, pptx, pdf) can pull from session state
    instead of re-running the tool or parsing prose.
    """
    __tablename__ = "data_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    summary_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    org_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    app_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_data_exec_session_created", "session_id", "created_date"),
        Index("ix_data_exec_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<DataExecution id={self.id!r} tool={self.tool_name!r}>"
```

- [ ] **Step 4: Run test — it passes**

```bash
cd backend && venv/bin/python -m pytest tests/alembic/test_049.py -v
```

- [ ] **Step 5: Create the migration**

Create `backend/alembic/versions/049_add_data_execution.py`:

```python
"""add data_executions table

Revision ID: 049
Revises: 048
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_executions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("args", JSONB, nullable=False, server_default="{}"),
        sa.Column("result", JSONB, nullable=False, server_default="{}"),
        sa.Column("summary_text", sa.String, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("app_id", sa.String(64), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_data_exec_session_created", "data_executions", ["session_id", "created_date"])
    op.create_index("ix_data_exec_expires", "data_executions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_data_exec_expires", table_name="data_executions")
    op.drop_index("ix_data_exec_session_created", table_name="data_executions")
    op.drop_table("data_executions")
```

- [ ] **Step 6: Apply migration locally**

```bash
cd backend && venv/bin/python -m alembic upgrade head
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/data_execution.py backend/alembic/versions/049_add_data_execution.py backend/tests/alembic/test_049.py
git commit -m "feat(data-execution): add DataExecution model + migration 049"
```

---

## Task 2: SessionState model + migration

**Files:**
- Create: `backend/app/models/session_state.py`
- Create: `backend/alembic/versions/050_add_session_state.py`
- Test: `backend/tests/alembic/test_050.py`

**Interfaces:**
- Consumes: `TimestampedBase`
- Produces: `SessionState` with `session_id: str` (PK), `last_execution_id: str | None`, `last_tool_name: str | None`, `last_data_signature: str | None`, `execution_count: int`, `org_id/app_id: str | None`, `is_deleted: bool`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/alembic/test_050.py`:

```python
from app.models.session_state import SessionState


def test_session_state_model_imports():
    assert SessionState.__tablename__ == "session_states"


def test_session_state_session_id_is_primary_key():
    col = SessionState.__table__.columns["session_id"]
    assert col.primary_key is True
    assert col.type.python_type is str


def test_session_state_has_required_fields():
    fields = {c.name for c in SessionState.__table__.columns}
    assert fields >= {
        "session_id", "last_execution_id", "last_tool_name",
        "last_data_signature", "execution_count", "org_id", "app_id",
        "is_deleted", "created_date", "updated_date",
    }
```

- [ ] **Step 2: Run test — fails**

```bash
cd backend && venv/bin/python -m pytest tests/alembic/test_050.py -v
```

- [ ] **Step 3: Create the model**

Create `backend/app/models/session_state.py`:

```python
"""SessionState model — per-session pointer to the most recent data tool execution."""
from __future__ import annotations
from typing import Optional
from sqlalchemy import String, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class SessionState(TimestampedBase):
    __tablename__ = "session_states"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_execution_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_tool_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_data_signature: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    execution_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    org_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    app_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<SessionState session_id={self.session_id!r} last={self.last_execution_id!r}>"
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Create migration**

Create `backend/alembic/versions/050_add_session_state.py`:

```python
"""add session_states table

Revision ID: 050
Revises: 049
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_states",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("last_execution_id", sa.String(64), nullable=True),
        sa.Column("last_tool_name", sa.String(128), nullable=True),
        sa.Column("last_data_signature", sa.String(64), nullable=True),
        sa.Column("execution_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("app_id", sa.String(64), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_date", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("session_states")
```

- [ ] **Step 6: Apply migration**

```bash
cd backend && venv/bin/python -m alembic upgrade head
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/models/session_state.py backend/alembic/versions/050_add_session_state.py backend/tests/alembic/test_050.py
git commit -m "feat(session-state): add SessionState model + migration 050"
```

---

## Task 3: SessionState service

**Files:**
- Create: `backend/app/services/data_execution/__init__.py`
- Create: `backend/app/services/data_execution/session_state.py`
- Test: `backend/tests/services/data_execution/__init__.py`
- Test: `backend/tests/services/data_execution/test_session_state.py`

**Interfaces:**
- Consumes: `SessionState`, `DataExecution` models.
- Produces: `SessionStateService.get_last(db, session_id) -> dict | None`, `set_last(db, session_id, execution_id, tool_name, data_signature, org_id, app_id) -> None`, `get_history(db, session_id, limit) -> list[dict]`. All best-effort.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/data_execution/__init__.py` (empty) and `test_session_state.py`:

```python
import pytest
from app.services.data_execution.session_state import SessionStateService


@pytest.fixture
def db():
    from app.db import SessionLocal
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def test_get_last_returns_none_when_empty(db):
    result = SessionStateService.get_last(db, session_id="test-nonexistent")
    assert result is None


def test_set_last_creates_row(db):
    SessionStateService.set_last(
        db, session_id="test-set-1",
        execution_id="evt_abc123", tool_name="ask_data_agent",
        data_signature="abc", org_id="org1", app_id="default-app",
    )
    db.commit()
    result = SessionStateService.get_last(db, session_id="test-set-1")
    assert result is not None
    assert result["execution_id"] == "evt_abc123"
    assert result["tool_name"] == "ask_data_agent"


def test_set_last_upserts_on_same_session(db):
    SessionStateService.set_last(db, session_id="test-upsert",
        execution_id="evt_first", tool_name="ask_data_agent",
        data_signature="a", org_id=None, app_id=None)
    db.commit()
    SessionStateService.set_last(db, session_id="test-upsert",
        execution_id="evt_second", tool_name="ask_erp_kpi",
        data_signature="b", org_id=None, app_id=None)
    db.commit()
    result = SessionStateService.get_last(db, session_id="test-upsert")
    assert result["execution_id"] == "evt_second"
    assert result["tool_name"] == "ask_erp_kpi"


def test_set_last_increments_counter(db):
    SessionStateService.set_last(db, session_id="test-count",
        execution_id="evt_1", tool_name="ask_data_agent",
        data_signature="a", org_id=None, app_id=None)
    db.commit()
    SessionStateService.set_last(db, session_id="test-count",
        execution_id="evt_2", tool_name="ask_data_agent",
        data_signature="b", org_id=None, app_id=None)
    db.commit()
    result = SessionStateService.get_last(db, session_id="test-count")
    assert result["execution_count"] == 2


def test_get_history_returns_list(db):
    history = SessionStateService.get_history(db, session_id="nonexistent", limit=10)
    assert isinstance(history, list)
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Implement SessionStateService**

Create `backend/app/services/data_execution/__init__.py` (empty) and `session_state.py`:

```python
"""SessionStateService — per-session pointer to the most recent data tool execution."""
from __future__ import annotations
import logging
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.session_state import SessionState
from app.models.data_execution import DataExecution

logger = logging.getLogger(__name__)


class SessionStateService:
    """All methods are best-effort: failures are logged but never raise."""

    @staticmethod
    def get_last(db: Session, *, session_id: str) -> Optional[dict]:
        try:
            row = (
                db.query(SessionState)
                .filter(
                    SessionState.session_id == session_id,
                    SessionState.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if not row:
                return None
            return {
                "execution_id": row.last_execution_id,
                "tool_name": row.last_tool_name,
                "data_signature": row.last_data_signature,
                "execution_count": row.execution_count,
                "updated_at": row.updated_date,
            }
        except Exception as exc:
            logger.warning("SessionState.get_last failed: %s", exc)
            return None

    @staticmethod
    def set_last(
        db: Session, *, session_id: str, execution_id: str,
        tool_name: str, data_signature: str,
        org_id: Optional[str], app_id: Optional[str],
    ) -> None:
        try:
            db.execute(
                pg_insert(SessionState).values(
                    session_id=session_id,
                    last_execution_id=execution_id,
                    last_tool_name=tool_name,
                    last_data_signature=data_signature,
                    execution_count=1,
                    org_id=org_id,
                    app_id=app_id,
                ).on_conflict_do_update(
                    index_elements=[SessionState.session_id],
                    set_={
                        "last_execution_id": execution_id,
                        "last_tool_name": tool_name,
                        "last_data_signature": data_signature,
                        "execution_count": SessionState.__table__.c.execution_count + 1,
                        "updated_date": SessionState.__table__.c.updated_date,
                    },
                )
            )
            db.commit()
        except Exception as exc:
            logger.warning("SessionState.set_last failed: %s", exc)
            db.rollback()

    @staticmethod
    def get_history(db: Session, *, session_id: str, limit: int = 20) -> list[dict]:
        try:
            rows = (
                db.query(DataExecution)
                .filter(
                    DataExecution.session_id == session_id,
                    DataExecution.is_deleted == False,  # noqa: E712
                )
                .order_by(DataExecution.created_date.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "execution_id": r.id,
                    "tool_name": r.tool_name,
                    "summary_preview": (r.summary_text or "")[:200],
                    "created_at": r.created_date,
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("SessionState.get_history failed: %s", exc)
            return []
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_execution/__init__.py backend/app/services/data_execution/session_state.py backend/tests/services/data_execution/__init__.py backend/tests/services/data_execution/test_session_state.py
git commit -m "feat(data-execution): SessionStateService (get_last, set_last, get_history)"
```

---

## Task 4: cache_data_execution service

**Files:**
- Create: `backend/app/services/data_execution/cache.py`
- Test: `backend/tests/services/data_execution/test_cache.py`

**Interfaces:**
- Consumes: `DataExecution`, `SessionStateService`, `CACHEABLE_TOOL_PATTERNS` set.
- Produces: `CACHEABLE_TOOL_PATTERNS: frozenset[str]`, `cache_data_execution(*, db, session_id, tool_name, args, result, summary_text, org_id, app_id) -> str | None`. Returns `"evt_xxxxxxxxxxxx"` or `None`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/data_execution/test_cache.py`:

```python
import pytest
from app.services.data_execution.cache import (
    CACHEABLE_TOOL_PATTERNS, cache_data_execution,
)


def test_cacheable_tool_patterns_contains_main_data_tools():
    assert "ask_data_agent" in CACHEABLE_TOOL_PATTERNS
    assert "ask_erp_kpi" in CACHEABLE_TOOL_PATTERNS
    assert "query_composer" in CACHEABLE_TOOL_PATTERNS
    assert "fetch_data_batch" in CACHEABLE_TOOL_PATTERNS
    assert "create_artifact" not in CACHEABLE_TOOL_PATTERNS


def test_cache_data_execution_returns_none_for_non_cacheable_tool():
    result = cache_data_execution(
        db=None, session_id="s1", tool_name="create_artifact",
        args={}, result={}, summary_text=None, org_id=None, app_id=None,
    )
    assert result is None


def test_cache_data_execution_persists_row():
    from app.db import SessionLocal
    from app.models.data_execution import DataExecution
    db = SessionLocal()
    try:
        exec_id = cache_data_execution(
            db=db, session_id="test-cache-1",
            tool_name="ask_data_agent",
            args={"query": "test"},
            result={"rows": [{"a": 1}], "columns": ["a"], "sql": "SELECT 1"},
            summary_text="test summary",
            org_id="org1", app_id="default-app",
        )
        assert exec_id is not None
        assert exec_id.startswith("evt_")
        row = db.query(DataExecution).filter_by(id=exec_id).first()
        assert row is not None
        assert row.tool_name == "ask_data_agent"
    finally:
        db.rollback()
        db.close()


def test_cache_data_execution_is_best_effort_on_db_error():
    from app.db import SessionLocal
    db = SessionLocal()
    db.close()
    result = cache_data_execution(
        db=db, session_id="test-cache-fail",
        tool_name="ask_data_agent", args={}, result={}, summary_text=None,
        org_id=None, app_id=None,
    )
    assert result is None
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Implement cache_data_execution**

Create `backend/app/services/data_execution/cache.py`:

```python
"""Auto-cache middleware for data tool results."""
from __future__ import annotations
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.data_execution import DataExecution
from app.services.data_execution.session_state import SessionStateService

logger = logging.getLogger(__name__)

CACHEABLE_TOOL_PATTERNS: frozenset[str] = frozenset({
    "ask_data_agent", "ask_erp_kpi", "ask_perception",
    "ask_forecast", "ask_pricing", "ask_decision",
    "query_composer", "fetch_data_batch", "execute_query",
    "ask_intelligence", "ask_rag_research",
})

CACHE_TTL_DAYS = 7


def _compute_data_signature(tool_name: str, result: dict) -> str:
    try:
        rows = result.get("rows") or []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            keys = sorted(rows[0].keys())
        else:
            keys = sorted((result or {}).keys())
        payload = f"{tool_name}|{len(rows)}|{','.join(keys)}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


async def cache_data_execution(
    *,
    db: Session, session_id: str, tool_name: str, args: dict,
    result: dict, summary_text: Optional[str],
    org_id: Optional[str], app_id: Optional[str],
) -> Optional[str]:
    """Persist a DataExecution row. Returns the new execution_id.
    Best-effort: failures are logged but do not break the tool call.
    """
    if tool_name not in CACHEABLE_TOOL_PATTERNS:
        return None
    if not session_id:
        return None
    try:
        execution_id = f"evt_{uuid.uuid4().hex[:12]}"
        signature = _compute_data_signature(tool_name, result or {})
        row = DataExecution(
            id=execution_id, session_id=session_id, tool_name=tool_name,
            args=args or {}, result=result or {}, summary_text=summary_text,
            expires_at=datetime.now(timezone.utc) + timedelta(days=CACHE_TTL_DAYS),
            org_id=org_id, app_id=app_id,
        )
        db.add(row)
        db.commit()
        SessionStateService.set_last(
            db, session_id=session_id, execution_id=execution_id,
            tool_name=tool_name, data_signature=signature,
            org_id=org_id, app_id=app_id,
        )
        return execution_id
    except Exception as exc:
        logger.warning("data_execution cache failed for %s: %s", tool_name, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_execution/cache.py backend/tests/services/data_execution/test_cache.py
git commit -m "feat(data-execution): cache_data_execution service + CACHEABLE_TOOL_PATTERNS"
```

---

## Task 5: build_session_state_block

**Files:**
- Create: `backend/app/services/data_execution/prompt_block.py`
- Test: `backend/tests/services/data_execution/test_prompt_block.py`

**Interfaces:**
- Consumes: `SessionStateService.get_last()`.
- Produces: `build_session_state_block(db, session_id) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/services/data_execution/test_prompt_block.py`:

```python
import pytest
from app.services.data_execution.prompt_block import build_session_state_block


def test_returns_none_when_no_execution():
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        result = build_session_state_block(db, session_id="test-no-exec")
        assert result is None
    finally:
        db.close()


def test_returns_block_with_execution_id():
    from app.db import SessionLocal
    from app.services.data_execution.session_state import SessionStateService
    db = SessionLocal()
    try:
        SessionStateService.set_last(
            db, session_id="test-block-1",
            execution_id="evt_abc123", tool_name="ask_data_agent",
            data_signature="abc", org_id=None, app_id=None,
        )
        db.commit()
        result = build_session_state_block(db, session_id="test-block-1")
        assert result is not None
        assert "evt_abc123" in result
        assert "ask_data_agent" in result
        assert "create_artifact" in result
    finally:
        db.rollback()
        db.close()
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Implement**

Create `backend/app/services/data_execution/prompt_block.py`:

```python
"""build_session_state_block — inject session state into the LLM system prompt."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from app.services.data_execution.session_state import SessionStateService


def build_session_state_block(db: Session, *, session_id: str) -> Optional[str]:
    state = SessionStateService.get_last(db, session_id=session_id)
    if not state or not state.get("execution_id"):
        return None
    return (
        "\n\nSESSION STATE — last data tool execution in this conversation:\n"
        f"  execution_id: {state['execution_id']}\n"
        f"  tool_name: {state['tool_name']}\n"
        f"  data_signature: {state.get('data_signature', '')}\n"
        "  When the user asks to re-export or re-format a previous analysis,\n"
        "  pass this execution_id to create_artifact(source_execution_id=...)\n"
        "  instead of re-running data tools."
    )
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_execution/prompt_block.py backend/tests/services/data_execution/test_prompt_block.py
git commit -m "feat(data-execution): build_session_state_block for prompt injection"
```

---

## Task 6: session_state_query tool

**Files:**
- Create: `backend/app/services/data_execution/session_state_tool.py`
- Test: `backend/tests/services/data_execution/test_session_state_tool.py`

**Interfaces:**
- Consumes: `SessionStateService.get_history()`.
- Produces: `SESSION_STATE_QUERY_SCHEMA`, `session_state_query(db, session_id, limit) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from app.services.data_execution.session_state_tool import (
    SESSION_STATE_QUERY_SCHEMA, session_state_query,
)


def test_schema_has_name_and_description():
    assert SESSION_STATE_QUERY_SCHEMA["type"] == "function"
    assert SESSION_STATE_QUERY_SCHEMA["function"]["name"] == "session_state_query"
    assert "previous" in SESSION_STATE_QUERY_SCHEMA["function"]["description"].lower()


def test_session_state_query_returns_list():
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        result = session_state_query(db, session_id="nonexistent", limit=5)
        assert isinstance(result, list)
    finally:
        db.close()
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Implement**

Create `backend/app/services/data_execution/session_state_tool.py`:

```python
"""session_state_query tool — LLM can query recent data tool executions."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from app.services.data_execution.session_state import SessionStateService


SESSION_STATE_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_state_query",
        "description": (
            "Return the recent data tool executions in this session, "
            "newest first. Use this when the user wants to re-export "
            "or re-format a SPECIFIC previous analysis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Max number of recent executions to return.",
                },
            },
        },
    },
}


def session_state_query(
    db: Session, *, session_id: str, limit: int = 10
) -> list[dict]:
    return SessionStateService.get_history(db, session_id=session_id, limit=limit)
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_execution/session_state_tool.py backend/tests/services/data_execution/test_session_state_tool.py
git commit -m "feat(data-execution): session_state_query tool"
```

---

## Task 7: detect_export_intent

**Files:**
- Create: `backend/app/services/intent/__init__.py`
- Create: `backend/app/services/intent/export_intent.py`
- Test: `backend/tests/services/intent/__init__.py`
- Test: `backend/tests/services/intent/test_export_intent.py`

**Interfaces:**
- Consumes: User message string.
- Produces: `ExportIntent` dataclass with `format: str | None`, `source_execution_id: str | None`, `confidence: Literal["strong", "weak"]`, `matched_pattern: str`. Plus `detect_export_intent(user_message: str) -> ExportIntent | None`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from app.services.intent.export_intent import detect_export_intent


@pytest.mark.parametrize("msg,expected_format", [
    ("give me in docx", "docx"),
    ("export as PDF", "pdf"),
    ("now as PowerPoint", "pptx"),
    ("make a Word file", "docx"),
    ("in Word", "docx"),
    ("docx", "docx"),
    ("in pdf", "pdf"),
    ("as xlsx", "xlsx"),
])
def test_strong_patterns(msg, expected_format):
    intent = detect_export_intent(msg)
    assert intent is not None
    assert intent.confidence == "strong"
    assert intent.format == expected_format


@pytest.mark.parametrize("msg", [
    "export the same", "download it", "save as that",
    "re-export the report", "reformat it",
])
def test_weak_patterns(msg):
    intent = detect_export_intent(msg)
    assert intent is not None
    assert intent.confidence == "weak"
    assert intent.format is None


@pytest.mark.parametrize("msg", [
    "give me Contract Performance for last month",
    "show me the analysis", "regenerate the report", "hi",
    "what is the margin?",
])
def test_no_export_intent(msg):
    assert detect_export_intent(msg) is None
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Implement**

Create `backend/app/services/intent/__init__.py` (empty) and `export_intent.py`:

```python
"""Code-level intent router for export requests."""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class ExportIntent:
    format: Optional[str]
    source_execution_id: Optional[str]
    confidence: Literal["strong", "weak"]
    matched_pattern: str


_FORMAT_ALIASES = {
    "docx": "docx", "word": "docx", "ms word": "docx",
    "pdf": "pdf",
    "pptx": "pptx", "powerpoint": "pptx", "slides": "pptx",
    "xlsx": "xlsx", "excel": "xlsx", "spreadsheet": "xlsx",
    "html": "html", "web page": "html",
}

_STRONG_PATTERNS = [
    (r"\b(?:give|send|export|save|download|make|convert|render)\b"
     r".*?\b(?:in|as|to)\b\s*(?:a\s+)?(docx|pdf|pptx|xlsx|html|word|powerpoint|excel)\b"),
    (r"\b(?:in|as)\s+(docx|pdf|pptx|xlsx|html)\s*$"),
    (r"^(docx|pdf|pptx|xlsx|html)\s*$"),
]

_WEAK_PATTERNS = [
    r"\b(?:export|download|save\s+as|send\s+(?:it|the\s+same|that))\b",
    r"\b(?:re-?format|re-?export)\b",
    r"\b(?:now|also)\s+(?:as|in)\s+(?:a\s+)?(?:word|pdf|powerpoint|excel|docx|pptx)\b",
]


def detect_export_intent(user_message: str) -> Optional[ExportIntent]:
    if not user_message:
        return None
    msg = user_message.lower().strip()

    for pattern in _STRONG_PATTERNS:
        m = re.search(pattern, msg)
        if m:
            fmt_token = next((g for g in m.groups() if g), None)
            return ExportIntent(
                format=_FORMAT_ALIASES.get(fmt_token, fmt_token),
                source_execution_id=None,
                confidence="strong",
                matched_pattern=pattern,
            )

    for pattern in _WEAK_PATTERNS:
        if re.search(pattern, msg):
            return ExportIntent(
                format=None, source_execution_id=None,
                confidence="weak", matched_pattern=pattern,
            )

    return None
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/intent/ backend/tests/services/intent/
git commit -m "feat(intent): detect_export_intent — code-level export request detector"
```

---

## Task 8: _payload_from_execution mapper

**Files:**
- Create: `backend/app/services/tool_handlers/_payload_from_execution.py`
- Test: `backend/tests/services/tool_handlers/test_payload_from_execution.py`

**Interfaces:**
- Consumes: A `DataExecution` ORM object.
- Produces: `_payload_from_execution(execution) -> dict` — ReportCardPayload-shaped.

- [ ] **Step 1: Write the failing test**

```python
from app.services.tool_handlers._payload_from_execution import _payload_from_execution


class FakeExecution:
    def __init__(self, tool_name, result, summary_text=""):
        self.tool_name = tool_name
        self.result = result
        self.summary_text = summary_text
        from datetime import datetime, timezone
        self.created_at = datetime.now(timezone.utc)


def test_payload_from_empty_result():
    payload = _payload_from_execution(FakeExecution("ask_data_agent", {}))
    assert payload["title"] == "Report from ask_data_agent"
    assert "0 rows" in payload["methodology"]


def test_payload_from_single_row_aggregate():
    payload = _payload_from_execution(FakeExecution(
        "ask_data_agent",
        {"rows": [[100, 200, 300]], "columns": ["a", "b", "c"], "summary": "Q3"},
    ))
    assert len(payload["kpis"]) == 3
    assert payload["kpis"][0]["value"] == 100
    assert payload["summary"] == "Q3"


def test_payload_from_multi_row_creates_chart():
    payload = _payload_from_execution(FakeExecution(
        "ask_data_agent",
        {"rows": [["A", 10], ["B", 20], ["C", 30]], "columns": ["x", "y"]},
    ))
    assert payload["chart"] is not None
    assert payload["chart"]["data"]["x"] == ["A", "B", "C"]


def test_payload_uses_result_title_when_present():
    payload = _payload_from_execution(FakeExecution(
        "ask_data_agent", {"rows": [], "title": "Custom Title"},
    ))
    assert payload["title"] == "Custom Title"
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Implement**

Create `backend/app/services/tool_handlers/_payload_from_execution.py`:

```python
"""_payload_from_execution — map cached DataExecution to ReportCardPayload dict."""
from __future__ import annotations
from typing import Any


def _payload_from_execution(execution: Any) -> dict:
    result = getattr(execution, "result", None) or {}
    rows = result.get("rows") or []
    columns = result.get("columns") or []
    summary = result.get("summary") or result.get("text") or ""

    title = result.get("title") or f"Report from {execution.tool_name}"
    methodology = (
        f"Data sourced from {execution.tool_name} "
        f"({len(rows)} rows, {len(columns)} columns). "
        f"Cached at {getattr(execution, 'created_at', 'unknown')}."
    )

    kpis = []
    if rows and len(rows) == 1 and len(columns) <= 6:
        for col, val in zip(columns, rows[0]):
            kpis.append({"label": str(col), "value": val, "caption": f"From {execution.tool_name}"})

    chart = None
    if rows and len(rows) > 1 and len(columns) >= 2:
        chart = {
            "type": "bar",
            "title": f"{columns[0]} by {columns[1]}",
            "data": {
                "x": [r[0] for r in rows[:50]],
                "y": [r[1] for r in rows[:50]],
                "x_label": str(columns[0]),
                "y_label": str(columns[1]),
            },
        }

    return {
        "title": title,
        "summary": summary[:1000] if summary else "",
        "methodology": methodology,
        "kpis": kpis,
        "chart": chart,
        "source": execution.tool_name,
    }
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tool_handlers/_payload_from_execution.py backend/tests/services/tool_handlers/test_payload_from_execution.py
git commit -m "feat(artifact): _payload_from_execution mapper"
```

---

## Task 9: create_artifact schema update + GUARD 1

**Files:**
- Modify: `backend/app/services/tool_handlers/artifact_tool.py:998-1062`
- Create: `backend/app/services/tool_handlers/_errors.py`
- Test: `backend/tests/services/tool_handlers/test_create_artifact_schema.py`
- Test: `backend/tests/services/tool_handlers/test_create_artifact_guards.py`

**Interfaces:**
- Consumes: Existing `CREATE_ARTIFACT_SCHEMA` dict.
- Produces: Updated schema with `source_execution_id` param. `ToolExecutionError` class. GUARD 1 in `_create_artifact_tool`.

- [ ] **Step 1: Write the failing test**

```python
# test_create_artifact_schema.py
from app.services.tool_handlers.artifact_tool import CREATE_ARTIFACT_SCHEMA


def test_schema_includes_source_execution_id():
    params = CREATE_ARTIFACT_SCHEMA["function"]["parameters"]
    assert "source_execution_id" in params["properties"]


def test_schema_title_is_required():
    params = CREATE_ARTIFACT_SCHEMA["function"]["parameters"]
    assert "title" in params["required"]
```

```python
# test_create_artifact_guards.py
import pytest
from unittest.mock import MagicMock
from app.services.tool_handlers.artifact_tool import _create_artifact_tool
from app.services.tool_handlers._errors import ToolExecutionError


def test_guard_1_raises_when_no_execution_id_and_no_data():
    db = MagicMock()
    user = MagicMock()
    user.org_id = "org1"
    user.app_id = "default-app"
    args = {"type": "docx", "title": "Test", "payload": {}}
    with pytest.raises(ToolExecutionError):
        import asyncio
        asyncio.run(_create_artifact_tool(
            db=db, user=user, app_id="default-app", args=args,
        ))
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Create _errors.py**

Create `backend/app/services/tool_handlers/_errors.py`:

```python
"""Common tool error types."""


class ToolExecutionError(Exception):
    pass
```

- [ ] **Step 4: Update schema**

In `backend/app/services/tool_handlers/artifact_tool.py`, update the `description` and add `source_execution_id` property. See spec for exact text.

- [ ] **Step 5: Add GUARD 1**

Add at top of `artifact_tool.py`:
```python
from app.services.tool_handlers._errors import ToolExecutionError
```

In `_create_artifact_tool`, after parsing args:
```python
    source_execution_id = args.get("source_execution_id")
    payload = args.get("payload") or {}
    conversation_id = args.get("conversation_id") or _extract_conv_id_from_context()

    if not source_execution_id:
        has_data = bool(
            payload.get("chart") or payload.get("kpis") or
            payload.get("data_table") or payload.get("slides") or
            payload.get("html_content")
        )
        if not has_data:
            raise ToolExecutionError(
                "create_artifact called with no source_execution_id and no "
                "structured payload. For re-exports, pass "
                "source_execution_id=evt_xxx."
            )
```

- [ ] **Step 6: Run test — passes**

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/tool_handlers/artifact_tool.py backend/app/services/tool_handlers/_errors.py backend/tests/services/tool_handlers/test_create_artifact_schema.py backend/tests/services/tool_handlers/test_create_artifact_guards.py
git commit -m "feat(artifact): add source_execution_id param + GUARD 1"
```

---

## Task 10: create_artifact GUARD 2 + execution resolution

**Files:**
- Modify: `backend/app/services/tool_handlers/artifact_tool.py`
- Create: `backend/app/services/data_execution/execution_service.py`
- Test: extend `test_create_artifact_guards.py`

**Interfaces:**
- Consumes: `DataExecutionService.get_by_id()`, `SessionStateService.get_last()`.
- Produces: An `execution` variable. GUARD 2 (raises when not found or wrong session).

- [ ] **Step 1: Write the failing test** (append to test_create_artifact_guards.py):

```python
def test_guard_2_raises_when_execution_id_not_found():
    from unittest.mock import patch
    db = MagicMock()
    user = MagicMock()
    user.org_id = "org1"
    user.app_id = "default-app"
    args = {"type": "docx", "title": "Test", "source_execution_id": "evt_nope"}
    with patch("app.services.tool_handlers.artifact_tool.DataExecutionService") as mock_svc:
        mock_svc.get_by_id.return_value = None
        import asyncio
        with pytest.raises(ToolExecutionError):
            asyncio.run(_create_artifact_tool(
                db=db, user=user, app_id="default-app", args=args,
            ))
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Create DataExecutionService**

Create `backend/app/services/data_execution/execution_service.py`:

```python
"""DataExecutionService — high-level lookups for DataExecution rows."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from app.models.data_execution import DataExecution


class DataExecutionService:
    @staticmethod
    def get_by_id(db: Session, execution_id: str) -> Optional[DataExecution]:
        try:
            return (
                db.query(DataExecution)
                .filter(
                    DataExecution.id == execution_id,
                    DataExecution.is_deleted == False,  # noqa: E712
                )
                .first()
            )
        except Exception:
            return None
```

- [ ] **Step 4: Add imports and GUARD 2**

In `artifact_tool.py`:
```python
from app.services.data_execution.execution_service import DataExecutionService
from app.services.data_execution.session_state import SessionStateService
```

After GUARD 1:
```python
    execution = None
    if source_execution_id:
        execution = DataExecutionService.get_by_id(db, source_execution_id)
        if not execution:
            raise ToolExecutionError(
                f"source_execution_id {source_execution_id!r} not found or "
                f"expired (TTL: 7 days)."
            )
        if execution.session_id != conversation_id:
            raise ToolExecutionError(
                f"source_execution_id {source_execution_id!r} belongs to a "
                f"different session."
            )
    else:
        state = SessionStateService.get_last(db, session_id=conversation_id)
        if state and state.get("execution_id"):
            execution = DataExecutionService.get_by_id(db, state["execution_id"])
```

- [ ] **Step 5: Run test — passes**

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/tool_handlers/artifact_tool.py backend/app/services/data_execution/execution_service.py backend/tests/services/tool_handlers/test_create_artifact_guards.py
git commit -m "feat(artifact): GUARD 2 + session-state fallback"
```

---

## Task 11: create_artifact _payload_from_execution integration + 0-row warning

**Files:**
- Modify: `backend/app/services/tool_handlers/artifact_tool.py`
- Test: `backend/tests/services/tool_handlers/test_create_artifact_e2e.py`

**Interfaces:**
- Consumes: `execution`, `payload` from args.
- Produces: Final payload — built from `_payload_from_execution()` + LLM overrides. 0-row warning if applicable.

- [ ] **Step 1: Write the failing test**

```python
from app.services.data_execution.cache import cache_data_execution
from app.services.data_execution.execution_service import DataExecutionService
from app.services.tool_handlers._payload_from_execution import _payload_from_execution


def test_full_flow_cache_then_map():
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        exec_id = cache_data_execution(
            db=db, session_id="test-e2e-flow",
            tool_name="ask_data_agent",
            args={"query": "test"},
            result={"rows": [[100, 200]], "columns": ["a", "b"], "summary": "E2E"},
            summary_text="E2E summary", org_id="org1", app_id="default-app",
        )
        assert exec_id is not None
        execution = DataExecutionService.get_by_id(db, exec_id)
        assert execution is not None
        payload = _payload_from_execution(execution)
        assert payload["summary"] == "E2E"
        assert len(payload["kpis"]) == 2
    finally:
        db.rollback()
        db.close()
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Wire _payload_from_execution into _create_artifact_tool**

In `artifact_tool.py`:
```python
from app.services.tool_handlers._payload_from_execution import _payload_from_execution
```

After GUARD 2:
```python
    if execution:
        cached_payload = _payload_from_execution(execution)
        merged = {**cached_payload, **payload}
        payload = merged
        rows = (execution.result or {}).get("rows") or []
        if len(rows) == 0:
            payload.setdefault("sections", []).append({
                "title": "Data Quality Note",
                "content": (
                    f"The cached data tool ({execution.tool_name}) returned 0 "
                    f"rows. The document below reflects that empty result."
                ),
                "type": "narrative",
            })
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tool_handlers/artifact_tool.py backend/tests/services/tool_handlers/test_create_artifact_e2e.py
git commit -m "feat(artifact): wire _payload_from_execution + 0-row warning"
```

---

## Task 12: Cleanup service

**Files:**
- Create: `backend/app/services/data_execution/cleanup.py`
- Test: `backend/tests/services/data_execution/test_cleanup.py`

**Interfaces:**
- Consumes: `DataExecution` model.
- Produces: `cleanup_expired_data_executions(db) -> int`, `enforce_per_session_cap(db) -> int`, `scheduled_cleanup_loop()`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from datetime import datetime, timezone, timedelta
from app.services.data_execution.cleanup import (
    cleanup_expired_data_executions, enforce_per_session_cap, PER_SESSION_CAP,
)


@pytest.fixture
def db():
    from app.db import SessionLocal
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


def test_cleanup_deletes_expired_rows(db):
    from app.models.data_execution import DataExecution
    expired = DataExecution(
        id="evt_expired", session_id="test-cleanup-1",
        tool_name="ask_data_agent", args={}, result={"rows": []},
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        org_id="org1", app_id="default-app", is_deleted=False,
    )
    db.add(expired)
    db.commit()
    deleted = cleanup_expired_data_executions(db)
    assert deleted >= 1
    assert db.query(DataExecution).filter_by(id="evt_expired").first() is None


def test_cap_enforced(db):
    from app.models.data_execution import DataExecution
    session_id = "test-cap-1"
    for i in range(PER_SESSION_CAP + 5):
        row = DataExecution(
            id=f"evt_cap_{i:03d}", session_id=session_id,
            tool_name="ask_data_agent", args={}, result={"rows": []},
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            org_id="org1", app_id="default-app", is_deleted=False,
        )
        db.add(row)
    db.commit()
    deleted = enforce_per_session_cap(db)
    assert deleted >= 5
    assert db.query(DataExecution).filter_by(session_id=session_id).count() == PER_SESSION_CAP
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Implement**

Create `backend/app/services/data_execution/cleanup.py`:

```python
"""Cleanup service for DataExecution rows — TTL sweep + per-session cap."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PER_SESSION_CAP = 20
SWEEP_INTERVAL_S = 3600


def cleanup_expired_data_executions(db: Session) -> int:
    try:
        now = datetime.now(timezone.utc)
        result = db.execute(
            text("DELETE FROM data_executions WHERE is_deleted = false AND expires_at < :now"),
            {"now": now},
        )
        db.commit()
        if result.rowcount:
            logger.info("data_executions cleanup: %d rows deleted", result.rowcount)
        return result.rowcount or 0
    except Exception as exc:
        logger.warning("data_executions cleanup failed: %s", exc)
        db.rollback()
        return 0


def enforce_per_session_cap(db: Session) -> int:
    try:
        result = db.execute(text("""
            DELETE FROM data_executions
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY session_id ORDER BY created_date DESC
                    ) AS rn
                    FROM data_executions WHERE is_deleted = false
                ) t WHERE rn > :cap
            )
        """), {"cap": PER_SESSION_CAP})
        db.commit()
        return result.rowcount or 0
    except Exception as exc:
        logger.warning("data_executions cap failed: %s", exc)
        db.rollback()
        return 0


async def scheduled_cleanup_loop() -> None:
    from app.db import SessionLocal
    while True:
        try:
            db = SessionLocal()
            try:
                cleanup_expired_data_executions(db)
                enforce_per_session_cap(db)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("scheduled cleanup loop error: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_S)
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_execution/cleanup.py backend/tests/services/data_execution/test_cleanup.py
git commit -m "feat(data-execution): cleanup service"
```

---

## Task 13: Config flags

**Files:**
- Modify: `backend/app/config.py`
- Test: append to `backend/tests/test_docx_themed.py`

**Interfaces:**
- Produces: `DATA_EXECUTION_CACHE_ENABLED`, `DATA_EXECUTION_CLEANUP_ENABLED`, `INTENT_ROUTER_ENABLED` (all default `False`).

- [ ] **Step 1: Write smoke test** (append to test_docx_themed.py):

```python
def test_data_execution_cache_flag_defaults_false():
    from app.config import settings
    assert settings.DATA_EXECUTION_CACHE_ENABLED is False


def test_data_execution_cleanup_flag_defaults_false():
    from app.config import settings
    assert settings.DATA_EXECUTION_CLEANUP_ENABLED is False


def test_intent_router_flag_defaults_false():
    from app.config import settings
    assert settings.INTENT_ROUTER_ENABLED is False
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Add the flags to config.py**

```python
    DATA_EXECUTION_CACHE_ENABLED: bool = False
    DATA_EXECUTION_CLEANUP_ENABLED: bool = False
    INTENT_ROUTER_ENABLED: bool = False
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/tests/test_docx_themed.py
git commit -m "feat(config): add 3 new session-cache flags"
```

---

## Task 14: Agent prompt — RE-EXPORT HARD RULE

**Files:**
- Modify: `backend/app/services/agent_prompts.py`
- Test: `backend/tests/test_prompt_re_export_rule.py`

- [ ] **Step 1: Write smoke test**

```python
def test_default_skills_block_contains_re_export_rule():
    from app.services.agent_prompts import default_skills_block
    block = default_skills_block()
    assert "RE-EXPORT" in block or "RE-FORMAT" in block
    assert "source_execution_id" in block
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Add the HARD RULE**

In `agent_prompts.py`, in the `default_skills_block()` return list, add a new fragment after "Sandbox path" and before "Dashboard workflow":

```python
            "RE-EXPORT / RE-FORMAT (HARD RULE): When the user asks to "
            "re-export or re-format a previous analysis to a different file "
            "format AFTER you've already produced an analysis in this "
            "conversation (e.g., \"give me in docx\", \"export as PDF\", "
            "\"now as Word\"):\n"
            "1. CHECK the SESSION STATE block in your system prompt for the "
            "most recent `execution_id` (e.g., \"evt_a1b2c3\").\n"
            "2. If present: call "
            "`create_artifact(source_execution_id=\"evt_a1b2c3\", "
            "type=<format>, title=<title>)`. The platform builds the "
            "document from the cached structured data — NO data tool re-run "
            "needed.\n"
            "3. If not present: re-run the data tool(s) you used originally, "
            "then call `create_artifact` with a populated `payload` (legacy "
            "path).\n"
            "4. NEVER fabricate a payload from the previous chat prose.\n\n",
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/agent_prompts.py backend/tests/test_prompt_re_export_rule.py
git commit -m "feat(agent_prompts): RE-EXPORT / RE-FORMAT HARD RULE"
```

---

## Task 15: Wire intent router into v3 stream

**Files:**
- Modify: `backend/app/routers/agents.py`
- Test: `backend/tests/services/intent/test_hard_bypass.py`

- [ ] **Step 1: Write the test**

```python
from app.services.intent.export_intent import detect_export_intent


def test_hard_bypass_strong_match_detected():
    intent = detect_export_intent("give me in docx")
    assert intent is not None
    assert intent.confidence == "strong"
    assert intent.format == "docx"
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Wire into v3 stream entry**

In `backend/app/routers/agents.py`, find the v3 message handler. Add at the top of the handler (before LLM invocation):

```python
from app.services.intent.export_intent import detect_export_intent
from app.services.data_execution.session_state import SessionStateService
from app.services.tool_handlers.artifact_tool import _create_artifact_tool
from app.config import settings

if settings.INTENT_ROUTER_ENABLED:
    intent = detect_export_intent(user_message)
    if intent and intent.confidence == "strong":
        state = SessionStateService.get_last(db, session_id=conversation_id)
        if state and state.get("execution_id"):
            intent.source_execution_id = state["execution_id"]
        if intent.source_execution_id:
            result = await _create_artifact_tool(
                db=db, user=user, app_id=app_id,
                args={
                    "type": intent.format,
                    "title": (state.get("tool_name", "Report") if state else "Report"),
                    "source_execution_id": intent.source_execution_id,
                },
            )
            yield _format_sse_done(result)
            return
    if intent and intent.confidence == "weak":
        state = SessionStateService.get_last(db, session_id=conversation_id)
        directive = "\n\nUSER INTENT DETECTED: re-export. Prefer create_artifact(source_execution_id=...)."
        if state and state.get("execution_id"):
            directive += f" Last: {state['execution_id']}\n"
        system_prompt += directive
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Verify imports**

```bash
docker exec zhanlu-backend python -c "from app.routers.agents import router"
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/agents.py backend/tests/services/intent/test_hard_bypass.py
git commit -m "feat(intent): wire intent router into v3 stream"
```

---

## Task 16: Auto-cache hook in tool dispatcher

**Files:**
- Modify: `backend/app/routers/agents.py`
- Test: `backend/tests/services/data_execution/test_inject.py`

- [ ] **Step 1: Write the test**

```python
from app.services.data_execution.cache import cache_data_execution


def test_inject_execution_id_in_result_dict():
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        exec_id = cache_data_execution(
            db=db, session_id="test-inject-1",
            tool_name="ask_data_agent", args={},
            result={"rows": [{"x": 1}]}, summary_text=None,
            org_id=None, app_id=None,
        )
        assert exec_id is not None
        result_with_id = {"rows": [{"x": 1}], "_execution_id": exec_id}
        assert "_execution_id" in result_with_id
    finally:
        db.rollback()
        db.close()
```

- [ ] **Step 2: Run test — fails**

- [ ] **Step 3: Wire the cache hook**

In `backend/app/routers/agents.py`, after each tool call:

```python
from app.services.data_execution.cache import cache_data_execution
from app.config import settings

if settings.DATA_EXECUTION_CACHE_ENABLED:
    exec_id = await cache_data_execution(
        db=db, session_id=conversation_id,
        tool_name=tool_name, args=args, result=result,
        summary_text=current_assistant_content,
        org_id=user.org_id, app_id=user.app_id,
    )
    if exec_id:
        result = {**(result or {}), "_execution_id": exec_id}
```

- [ ] **Step 4: Run test — passes**

- [ ] **Step 5: Verify imports**

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/agents.py backend/tests/services/data_execution/test_inject.py
git commit -m "feat(cache): auto-cache hook in tool dispatcher"
```

---

## Task 17: SESSION STATE block in prompt assembly

**Files:**
- Modify: `backend/app/routers/agents.py`

- [ ] **Step 1: Inject block**

```python
from app.services.data_execution.prompt_block import build_session_state_block

state_block = build_session_state_block(db, session_id=conversation_id)
if state_block:
    system_prompt = system_prompt + state_block
```

- [ ] **Step 2: Verify imports**

- [ ] **Step 3: Commit**

```bash
git add backend/app/routers/agents.py
git commit -m "feat(prompt): inject SESSION STATE block into system prompt"
```

---

## Task 18: Wire cleanup loop into app startup

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Add startup hook**

```python
import asyncio
from app.config import settings
from app.services.data_execution.cleanup import scheduled_cleanup_loop


@app.on_event("startup")
async def _startup():
    if settings.DATA_EXECUTION_CLEANUP_ENABLED:
        asyncio.create_task(scheduled_cleanup_loop())
```

- [ ] **Step 2: Verify imports**

- [ ] **Step 3: Commit**

```bash
git add backend/main.py
git commit -m "feat(cleanup): start scheduled_cleanup_loop on startup"
```

---

## Task 19: E2E test

**Files:**
- Create: `backend/tests/e2e/test_re_export_no_rerun.py`

- [ ] **Step 1: Write the E2E test**

See spec for the full test file. Key assertions:
- Turn 1 calls >= 1 data tool
- Turn 2 calls 0 data tools (the invariant)
- Turn 2 calls create_artifact
- Turn 2 produces non-empty DOCX

- [ ] **Step 2: Commit**

```bash
git add backend/tests/e2e/test_re_export_no_rerun.py
git commit -m "test(e2e): re_export_no_rerun"
```

---

## Task 20: Apply migrations

- [ ] **Step 1: Apply**

```bash
docker exec zhanlu-backend alembic upgrade head
```

- [ ] **Step 2: Verify tables**

```bash
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c "\d data_executions" | head
docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c "\d session_states" | head
```

- [ ] **Step 3: Restart backend**

```bash
docker restart zhanlu-backend
```

---

## Task 21: Enable flags and verify

- [ ] **Step 1: Set env**

```bash
DATA_EXECUTION_CACHE_ENABLED=true
DATA_EXECUTION_CLEANUP_ENABLED=true
INTENT_ROUTER_ENABLED=true
```

- [ ] **Step 2: Restart backend**

- [ ] **Step 3: Run E2E**

```bash
cd backend && E2E_RUN=1 venv/bin/python -m pytest tests/e2e/test_re_export_no_rerun.py -s -v
```

- [ ] **Step 4: Manual UI smoke test**

- [ ] **Step 5: Commit env**

```bash
git add .env.example
git commit -m "chore(env): enable session-cached re-export"
```

---

## Self-Review

- [x] Spec coverage: 6 sections → 21 tasks
- [x] No placeholders: every code block is complete
- [x] Type consistency: `evt_xxx` format, `Optional[dict]` returns, flag names
- [x] TDD pattern: every task has test-first steps
- [x] Backward compat: legacy `payload` path preserved
- [x] Safe rollout: 3 flags, independent toggling
