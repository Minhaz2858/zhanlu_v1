"""Tool model — skills and custom tools."""

from sqlalchemy import String, Text, Integer, Boolean, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Tool(TimestampedBase):
    __tablename__ = "tools"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False, default="custom_tool")
    trigger: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Operational state
    enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    call_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="active")

    # Source info
    source: Mapped[str | None] = mapped_column(String(50), nullable=True, default="builtin")
    github_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    skill_file_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Metadata
    version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    license: Mapped[str | None] = mapped_column(String(100), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    skill_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Default-skill flag — True for the 6 built-in default skills that are
    # always available to every agent (docx, pptx, pdf, html, dashboard, md).
    # Nullable for backward compat; rows without this flag are treated as False.
    is_default: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    # Content (can be large)
    skill_md: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Progressive disclosure — short summary for prompt injection (≤500 chars).
    # When progressive_disclosure is enabled on the agent, only this summary
    # is injected into the system prompt instead of the full skill_md body.
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Tags for progressive-disclosure filtering (JSON array of strings).
    # Example: ["image", "3d", "style-transfer"].
    tags_progressive: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # References and sources (JSON arrays)
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    references: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Kimi-style folder anatomy. references_manifest maps reference filename
    # (e.g. "output-formats.md") -> one-line summary; assets_manifest maps
    # asset relative path (e.g. "templates/report.docx") -> one-line
    # description. The actual file bytes live on the filesystem under
    # ~/.zhanlu/skills/<category>/<name>/references|assets/.
    references_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    assets_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Semantic embedding of the skill's description+body as a JSON float list.
    # Stored as JSON (not pgvector) so it works on both SQLite and Postgres
    # without requiring an extension, matching the convention used by
    # agent_memory.embedding and response_cache_entry.question_embedding.
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
