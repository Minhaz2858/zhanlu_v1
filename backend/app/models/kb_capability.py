"""Source-scoped capability profile for a database KnowledgeBase.

One row per connected database source. It answers the question every
downstream stage currently answers by guessing:

    "what can this source actually do, and how do I talk to it?"

Why this is a table rather than a ``db_type`` string comparison
--------------------------------------------------------------
Before this model the platform inferred dialect behavior from ``kb.db_type``
in at least five places, inconsistently:

* quoting — ``db/base.quote_ident`` (mysql/postgres/mssql/oracle/sqlite) vs
  ``db/schema_graph._quote_ident`` (mysql, else double quotes → wrong brackets
  for SQL Server) vs ``forecasting.datasource_registry.quote_identifier``;
* SQL generation — ``nl2sql/dialect_rules.quote_rule`` had no Oracle or SQL
  Server entry and fell back to the PostgreSQL text;
* row caps — ``LIMIT`` hardcoded in ``db/schema_graph``,
  ``knowledge_graph/catalog_indexer`` and ``db/nl_answer_service``;
* row-count statistics — an ``if mysql / elif postgres / else None`` chain;
* temporal coverage — ``knowledge_graph/freshness`` probing MAX(date) with no
  record of which date column or timezone it meant.

Nothing here is specific to a customer, a table, a schema or a vendor module:
the profile is *discovered from the source itself* (dialect + live probes) and
is looked up by ``kb_id``. A newly connected database gets a profile without
any code change, and no profile means "unknown", never a default.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class KBCapabilityProfile(TimestampedBase):
    """Discovered capability profile for one authorized database source."""

    __tablename__ = "kb_capability_profiles"

    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )

    # ── identity / dialect ────────────────────────────────────────────────
    # Canonical dialect name resolved by app.services.db.dialect (mysql,
    # postgres, sqlite, mssql, oracle). NOT the raw db_type string.
    dialect: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    db_type_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    server_version: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── identifier quoting (mirrors DialectProfile) ───────────────────────
    quote_style: Mapped[str | None] = mapped_column(String(16), nullable=True)
    quote_open: Mapped[str | None] = mapped_column(String(4), nullable=True)
    quote_close: Mapped[str | None] = mapped_column(String(4), nullable=True)

    # ── SQL feature capabilities ──────────────────────────────────────────
    pagination: Mapped[str | None] = mapped_column(String(16), nullable=True)
    null_function: Mapped[str | None] = mapped_column(String(32), nullable=True)
    concat_template: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supports_views: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_window_functions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    supports_explain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # None = not probed (honest "unknown"); False = the account has write perms.
    read_only: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # ── namespaces visible to this account ────────────────────────────────
    catalogs: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    schemas: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # ── locale / clock ────────────────────────────────────────────────────
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── limits ────────────────────────────────────────────────────────────
    query_timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    connect_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── machine-readable surface (what the dialect layer exposes) ─────────
    # date/timestamp function map, quoting rules, pagination — the
    # DialectProfile.capability_dict() payload.
    capabilities: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Probe-by-probe evidence: which statement produced which value, and which
    # probes were refused/not supported. Never invented — absent means untested.
    probe_evidence: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # ── catalog version ───────────────────────────────────────────────────
    # Digest of the catalog this profile was validated against (same shape
    # digest used by db/schema_shape), so a profile can be tied to a schema
    # revision and invalidated when the schema moves.
    catalog_version: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    table_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.utcnow()
    )
    detection_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("kb_id", name="uq_kb_capability_profile_kb"),
    )

    def as_dict(self) -> dict:
        """Serializable payload for prompts, API responses and tests."""
        return {
            "source_id": self.kb_id,
            "dialect": self.dialect,
            "server_version": self.server_version,
            "identifier_quoting": {
                "style": self.quote_style,
                "open": self.quote_open,
                "close": self.quote_close,
            },
            "pagination": self.pagination,
            "null_function": self.null_function,
            "concat_template": self.concat_template,
            "supports_views": self.supports_views,
            "supports_window_functions": self.supports_window_functions,
            "supports_explain": self.supports_explain,
            "read_only": self.read_only,
            "catalogs": self.catalogs or [],
            "schemas": self.schemas or [],
            "timezone": self.timezone,
            "locale": self.locale,
            "query_timeout_s": self.query_timeout_s,
            "max_rows": self.max_rows,
            "connect_latency_ms": self.connect_latency_ms,
            "catalog_version": self.catalog_version,
            "table_count": self.table_count,
            "capabilities": self.capabilities or {},
            "probe_evidence": self.probe_evidence or {},
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "detection_error": self.detection_error,
        }
