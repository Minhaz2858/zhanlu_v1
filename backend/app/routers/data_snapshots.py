"""DataSnapshot router — CRUD, NL2SQL, and lineage APIs.

Provides:
* POST /api/data-snapshots — create a snapshot from SQL + results
* GET /api/data-snapshots/{id} — get snapshot detail (with data)
* GET /api/data-snapshots/{id}/verify — verify checksum integrity
* GET /api/data-snapshots — list snapshots (with filters)
* POST /api/data-snapshots/nl2sql — generate SQL from natural language
* POST /api/data-snapshots/{id}/link — link snapshot to artifact
* GET /api/artifacts/{artifact_id}/snapshots — get evidence lineage for an artifact
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.services.data_snapshot.snapshot_service import DataSnapshotService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data-snapshots"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CreateSnapshotRequest(BaseModel):
    sql_query: str
    result_data: Optional[list] = None
    result_columns: Optional[list] = None
    natural_language: Optional[str] = None
    datasource_id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    conversation_id: Optional[str] = None
    execution_id: Optional[str] = None
    created_by_agent_id: Optional[str] = None
    validate_sql: bool = Field(default=True, alias="validate")
    allowed_tables: Optional[list[str]] = None
    ttl_days: int = 90
    query_duration_ms: Optional[int] = None

    model_config = {"populate_by_name": True}


class NL2SQLRequest(BaseModel):
    question: str
    schema_description: str
    datasource_id: Optional[str] = None
    allowed_tables: Optional[list[str]] = None


class LinkToArtifactRequest(BaseModel):
    artifact_id: str
    artifact_version_id: Optional[str] = None
    source_part_id: Optional[str] = None
    usage_note: Optional[str] = None


@router.post("/data-snapshots")
def create_snapshot(req: CreateSnapshotRequest, db: Session = Depends(get_db)):
    """Create an immutable DataSnapshot from a query and its results."""
    service = DataSnapshotService(db)
    try:
        snapshot = service.create_snapshot(
            sql_query=req.sql_query,
            result_data=req.result_data,
            result_columns=req.result_columns,
            natural_language=req.natural_language,
            datasource_id=req.datasource_id,
            knowledge_base_id=req.knowledge_base_id,
            conversation_id=req.conversation_id,
            execution_id=req.execution_id,
            created_by_agent_id=req.created_by_agent_id,
            validate=req.validate_sql,
            allowed_tables=req.allowed_tables,
            ttl_days=req.ttl_days,
            query_duration_ms=req.query_duration_ms,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return snapshot.to_dict()


@router.get("/data-snapshots")
def list_snapshots(
    conversation_id: Optional[str] = Query(None),
    datasource_id: Optional[str] = Query(None),
    artifact_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """List DataSnapshots with optional filters."""
    service = DataSnapshotService(db)
    snapshots = service.list_snapshots(
        conversation_id=conversation_id,
        datasource_id=datasource_id,
        artifact_id=artifact_id,
        limit=limit,
    )
    # Exclude full result_data from list view for performance
    results = []
    for s in snapshots:
        d = s.to_dict()
        d.pop("result_data", None)
        results.append(d)
    return results


@router.get("/data-snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str, db: Session = Depends(get_db)):
    """Get DataSnapshot detail (includes full result data)."""
    service = DataSnapshotService(db)
    snapshot = service.get_snapshot(snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return snapshot.to_dict()


@router.get("/data-snapshots/{snapshot_id}/verify")
def verify_snapshot(snapshot_id: str, db: Session = Depends(get_db)):
    """Verify a snapshot's data integrity by recomputing its checksum."""
    service = DataSnapshotService(db)
    is_valid = service.verify_snapshot(snapshot_id)
    return {"snapshot_id": snapshot_id, "verified": is_valid}


@router.post("/data-snapshots/nl2sql")
def nl2sql(req: NL2SQLRequest, db: Session = Depends(get_db)):
    """Generate SQL from natural language (governed NL2SQL).

    Returns the generated SQL and validation results.  The caller is
    responsible for executing the SQL and creating a DataSnapshot with
    the results.
    """
    service = DataSnapshotService(db)
    result = service.nl2sql(
        question=req.question,
        schema_description=req.schema_description,
        datasource_id=req.datasource_id,
        allowed_tables=req.allowed_tables,
    )
    return result


@router.post("/data-snapshots/{snapshot_id}/link")
def link_to_artifact(snapshot_id: str, req: LinkToArtifactRequest, db: Session = Depends(get_db)):
    """Link a DataSnapshot to an Artifact — establishing evidence lineage."""
    service = DataSnapshotService(db)
    link = service.link_to_artifact(
        snapshot_id=snapshot_id,
        artifact_id=req.artifact_id,
        artifact_version_id=req.artifact_version_id,
        source_part_id=req.source_part_id,
        usage_note=req.usage_note,
    )
    return link.to_dict()


@router.get("/artifacts/{artifact_id}/snapshots")
def get_artifact_snapshots(artifact_id: str, db: Session = Depends(get_db)):
    """Get all DataSnapshots cited by an artifact (evidence lineage)."""
    service = DataSnapshotService(db)
    return service.get_artifact_snapshots(artifact_id)
