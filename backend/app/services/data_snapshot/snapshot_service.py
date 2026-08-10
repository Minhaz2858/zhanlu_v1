"""DataSnapshot service — create, retrieve, and manage immutable query snapshots.

This service is the core of the evidence layer.  It:
1. Takes a natural language question
2. Uses the LLM to generate SQL (NL2SQL)
3. Validates the SQL for read-only safety
4. Executes the query against the datasource
5. Creates an immutable DataSnapshot with checksum
6. Links snapshots to artifacts for provenance

The golden rule: data-driven artifacts cite DataSnapshot IDs, never live queries.
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.data_snapshot import DataSnapshot, SnapshotArtifactLink, SNAPSHOT_FORMATS
from app.services.data_snapshot.sql_validator import validate_sql

logger = logging.getLogger(__name__)


class DataSnapshotService:
    """Service for creating and managing immutable data snapshots."""

    def __init__(self, db: Session):
        self.db = db

    def create_snapshot(
        self,
        sql_query: str,
        result_data: Optional[list] = None,
        result_columns: Optional[list] = None,
        natural_language: Optional[str] = None,
        datasource_id: Optional[str] = None,
        knowledge_base_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        created_by_agent_id: Optional[str] = None,
        validate: bool = True,
        allowed_tables: Optional[list] = None,
        ttl_days: int = 90,
        query_duration_ms: Optional[int] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> DataSnapshot:
        """Create an immutable DataSnapshot from a query and its results.

        Args:
            sql_query: The SQL query that produced the results
            result_data: The query results as a list of row dicts
            result_columns: Column metadata (name, type) for the results
            natural_language: The original NL question that led to this query
            validate: Whether to validate the SQL for read-only safety
            allowed_tables: Optional table allowlist for validation
            ttl_days: Days until the snapshot expires (default 90)

        Returns:
            The created DataSnapshot

        Raises:
            ValidationError: If SQL validation fails and validate=True
        """
        # Validate SQL
        if validate:
            validation = validate_sql(sql_query, allowed_tables=allowed_tables)
            if not validation["valid"]:
                raise ValueError(f"SQL validation failed: {validation['errors']}")

        # Compute checksum of result data
        data_str = json.dumps(result_data or [], sort_keys=True, default=str)
        checksum = hashlib.sha256(data_str.encode("utf-8")).hexdigest()
        data_size = len(data_str.encode("utf-8"))

        # Create snapshot
        snapshot = DataSnapshot(
            id=str(uuid4()),
            sql_query=sql_query,
            sql_validated=validate,
            natural_language=natural_language,
            datasource_id=datasource_id,
            knowledge_base_id=knowledge_base_id,
            result_data=result_data,
            result_columns=result_columns,
            row_count=len(result_data) if result_data else 0,
            data_size_bytes=data_size,
            checksum=checksum,
            snapshot_format="json",
            status="active",
            conversation_id=conversation_id,
            execution_id=execution_id,
            created_by_agent_id=created_by_agent_id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days),
            query_duration_ms=query_duration_ms,
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(snapshot)
        self.db.commit()
        self.db.refresh(snapshot)

        logger.info(
            "Created DataSnapshot %s (%d rows, %d bytes, checksum=%s...)",
            snapshot.id, snapshot.row_count, data_size, checksum[:16],
        )
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> Optional[DataSnapshot]:
        """Get a DataSnapshot by ID."""
        return self.db.query(DataSnapshot).filter(DataSnapshot.id == snapshot_id).first()

    def verify_snapshot(self, snapshot_id: str) -> bool:
        """Verify a snapshot's data integrity by recomputing its checksum."""
        snapshot = self.get_snapshot(snapshot_id)
        if not snapshot:
            return False

        data_str = json.dumps(snapshot.result_data or [], sort_keys=True, default=str)
        computed_checksum = hashlib.sha256(data_str.encode("utf-8")).hexdigest()
        return computed_checksum == snapshot.checksum

    def list_snapshots(
        self,
        conversation_id: Optional[str] = None,
        datasource_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[DataSnapshot]:
        """List snapshots with optional filters."""
        query = self.db.query(DataSnapshot).filter(DataSnapshot.is_deleted == False)

        if conversation_id:
            query = query.filter(DataSnapshot.conversation_id == conversation_id)
        if datasource_id:
            query = query.filter(DataSnapshot.datasource_id == datasource_id)
        if artifact_id:
            # Join through SnapshotArtifactLink
            query = query.join(
                SnapshotArtifactLink, SnapshotArtifactLink.snapshot_id == DataSnapshot.id
            ).filter(SnapshotArtifactLink.artifact_id == artifact_id)

        return query.order_by(DataSnapshot.created_date.desc()).limit(limit).all()

    # --- Artifact lineage ---

    def link_to_artifact(
        self,
        snapshot_id: str,
        artifact_id: str,
        artifact_version_id: Optional[str] = None,
        source_part_id: Optional[str] = None,
        usage_note: Optional[str] = None,
    ) -> SnapshotArtifactLink:
        """Link a DataSnapshot to an Artifact — establishing evidence lineage."""
        link = SnapshotArtifactLink(
            id=str(uuid4()),
            snapshot_id=snapshot_id,
            artifact_id=artifact_id,
            artifact_version_id=artifact_version_id,
            source_part_id=source_part_id,
            usage_note=usage_note,
        )
        self.db.add(link)
        self.db.commit()
        self.db.refresh(link)

        logger.info("Linked snapshot %s to artifact %s", snapshot_id, artifact_id)
        return link

    def get_artifact_snapshots(self, artifact_id: str) -> list[dict]:
        """Get all DataSnapshots cited by an artifact (evidence lineage)."""
        links = (
            self.db.query(SnapshotArtifactLink)
            .filter(SnapshotArtifactLink.artifact_id == artifact_id)
            .all()
        )
        results = []
        for link in links:
            snapshot = self.get_snapshot(link.snapshot_id)
            if not snapshot:
                continue
            results.append({
                "snapshot_id": snapshot.id,
                "natural_language": snapshot.natural_language,
                "sql_query": snapshot.sql_query,
                "row_count": snapshot.row_count,
                "checksum": snapshot.checksum[:16] + "...",
                "usage_note": link.usage_note,
                "created_date": snapshot.created_date.isoformat() if snapshot.created_date else None,
            })
        return results

    # --- NL2SQL (governed) ---

    def nl2sql(
        self,
        question: str,
        schema_description: str,
        datasource_id: Optional[str] = None,
        allowed_tables: Optional[list] = None,
    ) -> dict:
        """Generate SQL from natural language using the LLM.

        This is the governed NL2SQL pipeline:
        1. LLM generates SQL from the question + schema
        2. SQL is validated for read-only safety
        3. Returns the validated SQL (caller executes it)

        Args:
            question: Natural language question
            schema_description: Text description of the database schema
            datasource_id: Optional datasource ID for tracking
            allowed_tables: Optional table allowlist

        Returns:
            {
                "sql": str,
                "valid": bool,
                "errors": [str],
                "warnings": [str],
            }
        """
        from app.services.llm_service import call_llm

        system_prompt = f"""You are a SQL query generator. Given a natural language question and a database schema, generate a safe, read-only SQL SELECT query.

Rules:
- Only generate SELECT statements
- Never use INSERT, UPDATE, DELETE, DROP, or other modifying statements
- Use standard SQL syntax compatible with PostgreSQL
- Keep queries simple and efficient
- If the question is ambiguous, make a reasonable assumption

Database Schema:
{schema_description}

Respond with ONLY the SQL query, no explanation. Start with SELECT or WITH."""

        try:
            result = call_llm(
                prompt=system_prompt,
                messages=[{"role": "user", "content": question}],
                temperature=0,
            )

            sql = result.get("response", "").strip()
            # Remove markdown code fences if present
            if sql.startswith("```"):
                sql = sql.split("\n", 1)[1] if "\n" in sql else sql[3:]
            if sql.endswith("```"):
                sql = sql[:-3]
            sql = sql.strip()

            # Validate the generated SQL
            validation = validate_sql(sql, allowed_tables=allowed_tables)

            return {
                "sql": sql,
                "valid": validation["valid"],
                "errors": validation["errors"],
                "warnings": validation["warnings"],
            }

        except Exception as e:
            logger.error("NL2SQL failed: %s", e)
            return {
                "sql": "",
                "valid": False,
                "errors": [f"NL2SQL generation failed: {str(e)}"],
                "warnings": [],
            }

    # --- Expiration ---

    def expire_old_snapshots(self) -> int:
        """Mark snapshots past their expiration as 'expired' (not deleted)."""
        now = datetime.now(timezone.utc)
        expired = (
            self.db.query(DataSnapshot)
            .filter(
                DataSnapshot.status == "active",
                DataSnapshot.expires_at < now,
            )
            .all()
        )
        for snapshot in expired:
            snapshot.status = "expired"

        if expired:
            self.db.commit()
            logger.info("Expired %d old snapshots", len(expired))

        return len(expired)
