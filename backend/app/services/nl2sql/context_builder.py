"""ContextBuilder — assembles the full LLM prompt from M-Schema, terminology,
Q→SQL examples, and dialect rules.

Each data source contributes a tagged section (``<dialect-rules>``, ``<m-schema>``,
``<terminology>``, ``<q-sql-examples>``) so the LLM can clearly distinguish
schema from domain knowledge from examples.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.datasources import DatasourceAdapter
from app.services.nl2sql import _build_adapter

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Assemble the NL2SQL LLM prompt context from all metadata sources.

    Call ``build(question, binding, ds_config)`` to get a composite string
    ready for injection into the system prompt / user message.
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    # ── public API ────────────────────────────────────────────────────────

    def build(
        self,
        question: str,
        binding: dict[str, Any] | None,
        ds_config: dict[str, Any],
    ) -> str:
        """Build the complete context string for the given question and datasource.

        Returns a multi-section string with tagged blocks the LLM can parse.
        """
        sections: list[str] = []
        datasource_id = binding.get("datasource_id") if binding else None
        allowed_tables = _safe_list(binding.get("allowed_tables")) if binding else None

        # 1. Dialect quoting rules
        dialect_section = self._build_dialect_section(ds_config)
        if dialect_section:
            sections.append(dialect_section)

        # 2. M-Schema
        schema_section = self._build_schema_section(ds_config, allowed_tables)
        if schema_section:
            sections.append(schema_section)

        # 3. Terminology (business glossary)
        terminology_section = self._build_terminology_section(
            question, datasource_id
        )
        if terminology_section:
            sections.append(terminology_section)

        # 4. Q→SQL examples (few-shot)
        examples_section = self._build_examples_section(question, datasource_id)
        if examples_section:
            sections.append(examples_section)

        return "\n\n".join(sections)

    # ── private section builders ──────────────────────────────────────────

    def _build_dialect_section(self, ds_config: dict[str, Any]) -> str:
        try:
            from app.services.nl2sql.dialect_rules import quote_rule

            dialect = ds_config.get("dialect", "postgresql")
            return f"<dialect-rules>{quote_rule(dialect)}</dialect-rules>"
        except Exception:
            return ""

    def _build_schema_section(
        self,
        ds_config: dict[str, Any],
        allowed_tables: list[str] | None,
    ) -> str:
        try:
            adapter = _build_adapter(ds_config)
            from app.services.datasources.m_schema import render_m_schema

            schema_text = render_m_schema(
                adapter, allowed_tables=allowed_tables, sample_rows=3
            )
            return f"<m-schema>\n{schema_text}\n</m-schema>"
        except Exception as exc:
            logger.warning("Failed to render M-Schema: %s", exc)
            return ""

    def _build_terminology_section(
        self,
        question: str,
        datasource_id: str | None,
    ) -> str:
        if not datasource_id:
            return ""
        try:
            from app.services.terminology.service import TerminologyService

            svc = TerminologyService(self._db)
            results = svc.search_by_word(question, datasource_id, top_k=3)
            if not results:
                return ""

            lines = ["<terminology>"]
            for word, desc, score in results:
                lines.append(f"  - {word}: {desc}  (relevance: {score:.2f})")
            lines.append("</terminology>")
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("Failed to load terminology: %s", exc)
            return ""

    def _build_examples_section(
        self,
        question: str,
        datasource_id: str | None,
    ) -> str:
        if not datasource_id:
            return ""
        try:
            from app.services.q_sql_examples.service import QSqlExampleService

            svc = QSqlExampleService(self._db)
            results = svc.top_k(question, datasource_id, k=3)
            if not results:
                return ""

            lines = ["<q-sql-examples>"]
            for q_text, sql, score in results:
                lines.append(f"  Question: {q_text}")
                lines.append(f"  SQL: {sql}")
                lines.append(f"  (relevance: {score:.2f})")
                lines.append("")
            lines.append("</q-sql-examples>")
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("Failed to load Q→SQL examples: %s", exc)
            return ""


def _safe_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    if val is not None:
        return [str(val)]
    return []
