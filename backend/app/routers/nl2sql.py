"""NL2SQL router — expose governed NL2SQL as a FastAPI endpoint.

Endpoints:
- ``POST /api/nl2sql/ask`` — ask a question and get data back.
- ``GET /api/nl2sql/datasources`` — list available datasources.
- ``GET /api/nl2sql/datasources/{id}/schema`` — describe a datasource's schema.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user_required
from app.services.nl2sql import ask as nl2sql_ask, NL2SQLResult
from app.services.datasources.sqlite_adapter import SQLiteAdapter
from app.services.datasources.postgres_adapter import PostgresAdapter

router = APIRouter(tags=["nl2sql"], dependencies=[Depends(get_current_user_required)])


# ── Schemas ───────────────────────────────────────────────────────


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="Natural-language question")
    binding_id: str = Field(..., description="AgentDataBinding ID")
    datasource_id: str | None = Field(None, description="Override datasource")


class ColumnSchema(BaseModel):
    name: str
    dtype: str
    nullable: bool = True
    is_pk: bool = False


class TableSchema(BaseModel):
    name: str
    columns: list[ColumnSchema]


class DatasourceInfo(BaseModel):
    id: str
    name: str
    dialect: str
    connected: bool = False


class AskResponse(BaseModel):
    success: bool
    question: str
    sql: str = ""
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    duration_ms: float = 0.0
    intent: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    error: str = ""


# ── Endpoints ─────────────────────────────────────────────────────


@router.post("/nl2sql/ask", response_model=AskResponse)
def ask_endpoint(req: AskRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ask a natural-language question against a governed datasource."""
    # Load datasource config
    ds_config: dict[str, Any] | None = None
    if req.datasource_id:
        try:
            from app.models.datasource import Datasource
            ds = db.query(Datasource).filter(
                Datasource.id == req.datasource_id,
                Datasource.is_deleted == False,
            ).first()
            if ds:
                ds_config = ds.connection_config or {}
        except Exception:
            pass

    result: NL2SQLResult = nl2sql_ask(
        req.question,
        binding_id=req.binding_id,
        db=db,
        datasource_config=ds_config,
    )

    resp: dict[str, Any] = {
        "success": result.success,
        "question": result.question,
        "sql": result.sql,
        "row_count": result.data.row_count if result.data else 0,
        "duration_ms": result.duration_ms,
        "error": result.error,
    }

    if result.data:
        resp["columns"] = result.data.columns
        resp["rows"] = [list(r) for r in result.data.rows]

    if result.intent:
        resp["intent"] = {
            "metric_name": result.intent.metric_name,
            "table_name": result.intent.table_name,
            "columns": result.intent.columns,
            "confidence": result.intent.confidence,
        }

    if result.validation:
        resp["validation"] = {
            "is_valid": result.validation.is_valid,
            "errors": result.validation.errors,
            "warnings": result.validation.warnings,
            "tables_referenced": result.validation.tables_referenced,
        }

    if result.policy:
        resp["policy"] = {
            "allowed": result.policy.allowed,
            "reason": result.policy.reason,
        }

    if not result.success:
        raise HTTPException(status_code=422, detail=result.error)

    return resp


@router.get("/nl2sql/datasources", response_model=list[DatasourceInfo])
def list_datasources(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """List all registered datasources."""
    try:
        from app.models.datasource import Datasource
        rows = db.query(Datasource).filter(Datasource.is_deleted == False).all()
        results: list[dict[str, Any]] = []
        for r in rows:
            config = r.connection_config or {}
            results.append({
                "id": r.id,
                "name": r.name,
                "dialect": config.get("dialect", r.dialect or "unknown"),
                "connected": False,  # Lazy — test on-demand
            })
        return results
    except Exception:
        return []


@router.get("/nl2sql/datasources/{ds_id}/schema", response_model=list[TableSchema])
def describe_schema(ds_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Return the full schema of a datasource."""
    try:
        from app.models.datasource import Datasource
        ds = db.query(Datasource).filter(
            Datasource.id == ds_id,
            Datasource.is_deleted == False,
        ).first()
        if ds is None:
            raise HTTPException(status_code=404, detail="Datasource not found")

        config = ds.connection_config or {}
        dialect = config.get("dialect", ds.dialect or "sqlite").lower()
        if dialect in ("postgres", "postgresql"):
            adapter = PostgresAdapter(
                host=config.get("host", "localhost"),
                port=int(config.get("port", 5432)),
                dbname=config.get("database", config.get("dbname", "zhanlu")),
                user=config.get("username", config.get("user", "zhanlu")),
                password=config.get("password", ""),
            )
        else:
            adapter = SQLiteAdapter(db_path=config.get("path", config.get("db_path", ":memory:")))

        schema = adapter.refresh_schema()
        return [
            {
                "name": table,
                "columns": [
                    {"name": c.name, "dtype": c.dtype, "nullable": c.nullable, "is_pk": c.is_pk}
                    for c in cols
                ],
            }
            for table, cols in schema.items()
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Schema discovery failed: {e}")
