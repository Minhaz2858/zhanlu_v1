"""Ontology sensors (P3-2) — scheduled data watches that trigger fan-out.

A sensor watches a real tenant database (any connector) on a schedule:
it runs a read-only watch SQL (via QueryService's read-only gate),
compares the numeric result against a threshold, and on breach-state
CHANGE notifies the agents bound to the watched object's LINKED domains
(ontology fan-out). State-change detection prevents alert spam: a breach
notifies once, recovery notifies once.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampedBase


class OntologySensor(TimestampedBase, Base):
    __tablename__ = "ontology_sensors"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # which ontology object's linked domains get notified on breach
    object_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # which tenant data source to watch
    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    # read-only SQL returning ONE numeric value (validated by the
    # execution gate every run — write statements are rejected)
    watch_sql: Mapped[str] = mapped_column(Text, nullable=False)

    # comparison: value <op> threshold
    op: Mapped[str] = mapped_column(String(10), nullable=False, default="lt")
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0)

    # min poll granularity is the loop tick (60s); typical 3600
    schedule_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3600)

    # optional swarm team mailbox delivery target
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # {"breached": bool, "value": float} — state-change detection
    last_state_json: Mapped[dict | None] = mapped_column(Text, nullable=True)


class OntologySensorRun(TimestampedBase, Base):
    __tablename__ = "ontology_sensor_runs"

    sensor_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # state transition that fired: breach | recovery | none | error
    transition: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
