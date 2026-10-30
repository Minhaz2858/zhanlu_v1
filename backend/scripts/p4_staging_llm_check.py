"""P4 A6 — LIVE staging check for real-LLM document extraction.

NOT part of the CI suite (the suite stubs `_llm_propose`). This script
runs the REAL extraction path — real file, real extractors, real
chunker, real `_llm_propose` → `chat_completion_json_sync` against the
configured production model — against a temporary in-memory database.

Usage (from backend/):
    ONTOLOGY_DOC_LLM_LIVE=1 python scripts/p4_staging_llm_check.py

Exit 0 = the live model produced verifiable, kind-valid proposals from
the document; any failure (auth, model, untraceable quotes) exits 1
with the report printed.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.environ.get("ONTOLOGY_DOC_LLM_LIVE") != "1":
    sys.exit("Refusing to run a live-model call: set ONTOLOGY_DOC_LLM_LIVE=1")

DOC = """Data Handling Policy (internal)

Customer means any organization that purchases a subscription or
professional services engagement from the company, including trial
accounts that have accepted the master agreement.

Refunds require approval by the finance department before any credit
is issued to the payment instrument used for the original charge.

Support tickets must be answered within one business day for
enterprise customers and within three business days for all others.
"""


def main() -> int:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models.base import Base
    import app.models.ontology_revision  # noqa: F401
    from app.models.knowledge_base import DocumentVersion, KnowledgeBase
    import app.config as config
    import app.services.document_ingestion.service as svc
    from app.services.ontology.document_grounding import (
        extract_document_evidence,
    )

    config.settings.ONTOLOGY_DOC_GROUNDING_ENABLED = True

    # Optional endpoint pin (staging: the production default endpoint may
    # live in the DB, not in .env). ONTOLOGY_DOC_LLM_BASE_URL / _MODEL /
    # _API_KEY override the legacy env provider.
    base_url = os.environ.get("ONTOLOGY_DOC_LLM_BASE_URL")
    model = os.environ.get("ONTOLOGY_DOC_LLM_MODEL")
    api_key = os.environ.get("ONTOLOGY_DOC_LLM_API_KEY")
    if base_url and model and api_key:
        from types import SimpleNamespace

        import app.services.llm_service as llm_service

        _orig = llm_service.chat_completion_json_sync

        def _pinned(prompt, schema=None, temperature=0.0, endpoint=None):
            return _orig(prompt, schema=schema, temperature=temperature,
                         endpoint=SimpleNamespace(
                             model_id=model, base_url=base_url,
                             api_key=api_key))

        # _llm_propose imports the symbol INSIDE the function body, so the
        # pin must land on the source module.
        llm_service.chat_completion_json_sync = _pinned

    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Only the tables the extraction pipeline touches (avoid unrelated
    # JSONB models that don't compile for SQLite).
    from app.models.ontology import DocumentProposal, OntologyDomain, \
        OntologyObject

    Base.metadata.create_all(
        eng, tables=[DocumentVersion.__table__,
                     DocumentProposal.__table__,
                     KnowledgeBase.__table__,
                     OntologyDomain.__table__,
                     OntologyObject.__table__])
    db = sessionmaker(bind=eng)()

    tmp = tempfile.mkdtemp(prefix="p4-live-")
    (Path(tmp) / "policy.txt").write_text(DOC, encoding="utf-8")
    svc._upload_root = lambda: Path(tmp)  # noqa: SLF001 — staging harness

    kb = KnowledgeBase(org_id="org-live-check", name="policy",
                       source_kind="file", file_type="txt",
                       file_url="/api/uploads/policy.txt")
    db.add(kb)
    db.commit()

    has_key = bool(
        getattr(config.settings, "DASHSCOPE_API_KEY", None)
        or getattr(config.settings, "OPENAI_API_KEY", None))
    print(f"model endpoint configured: {has_key}")
    report = extract_document_evidence(db, kb, org_id=kb.org_id)
    print("report:", report)

    from app.models.ontology import DocumentProposal as _DP

    proposals = db.query(_DP).all()
    ok = report.get("proposed", 0) > 0 and report.get(
        "dropped_untraceable", 0) == 0
    for p in proposals:
        print(f"- [{p.proposal_kind}] {p.payload_json.get('name')!r}: "
              f"quote={p.quote[:80]!r}... chunk={p.chunk_key}")
    print("VERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
