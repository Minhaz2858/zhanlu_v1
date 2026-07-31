# Document RAG for Knowledge Bases — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let users upload documents (PDF, DOCX, CSV, XLSX, MD, TXT, JSON) into a `KnowledgeBase` (`source_kind='file'`) and have agents answer questions over them via chunk + embeddings + vector retrieval.

**Architecture:** Reuse the existing dormant `source_kind='file'` slot on `KnowledgeBase`. On upload/save, an ingestion service extracts text per-format, chunks it, embeds with local `sentence-transformers/all-MiniLM-L6-v2`, and stores vectors in ChromaDB (one collection per `org_id`). The existing `data_agent` subagent (reached via `ask_data_agent`) gets two new tools — `answer_from_documents` and `search_documents` — alongside the 4 DB tools. `list_data_sources` is extended to surface file KBs too, so the data_agent picks the right tool based on `source_kind`. The prompt builder stops skipping file KBs.

**Tech Stack:** ChromaDB (`chromadb`), `sentence-transformers`, `pypdf` (PDF text), `python-docx` (already installed), `openpyxl` (already installed), `markdown` (MD→HTML→text). All embedding model loading is lazy + singleton to keep import-time memory flat.

---

## Recap of agreed decisions

| Decision | Choice |
|---|---|
| RAG approach | Chunk + embeddings + vector search |
| Vector store | ChromaDB (embedded, persisted to disk) |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` (384 dims, local, no API key) |
| UI / model | Reuse existing `source_kind='file'` on `KnowledgeBase` |
| Agent path | Extend `data_agent` subagent with doc tools (no new delegation tool) |

---

## File map

**Backend — new files:**
- `backend/app/services/document_ingestion/__init__.py`
- `backend/app/services/document_ingestion/extractors.py` — per-format text extraction
- `backend/app/services/document_ingestion/chunker.py` — recursive text splitter
- `backend/app/services/document_ingestion/embedder.py` — lazy singleton sentence-transformers wrapper
- `backend/app/services/document_ingestion/store.py` — ChromaDB client + collection per org
- `backend/app/services/document_ingestion/service.py` — orchestrator (`ingest`, `delete`, `status`)
- `backend/app/services/document_ingestion/retrieval.py` — vector search + LLM synthesis (`answer_from_documents`, `search_documents`)
- `backend/app/routers/knowledge_bases.py` — custom KB endpoints (`/reindex`, `/status`)
- `backend/alembic/versions/030_knowledge_base_document_indexing.py`
- `backend/tests/test_document_ingestion.py`
- `backend/tests/test_document_retrieval_tool.py`

**Backend — modified files:**
- `backend/requirements.txt` — add `chromadb`, `sentence-transformers`, `pypdf`, `markdown`
- `backend/app/models/knowledge_base.py` — add `indexing_status`, `chunk_count`, `index_error`, `last_indexed_at`
- `backend/app/services/tool_handlers/db_tools.py` — `list_data_sources` returns file KBs too (with `source_kind`)
- `backend/app/services/tool_handlers/delegation_tools.py` — add doc tools to `_DATA_AGENT_TOOLS`
- `backend/app/services/agent_definitions/__init__.py` — extend `DATA_AGENT_PROMPT` with doc-tool guidance
- `backend/app/services/data_source_runtime/data_source_runtime.py` — stop skipping file KBs in prompt section; inject `ask_data_agent` when file KBs bound
- `backend/main.py` — register the new `knowledge_bases` router

**Frontend — modified files:**
- `frontend/src/components/kb/KbFileFields.jsx` — add `.docx,.md` to `accept`; show `indexing_status` hint
- `frontend/src/components/kb/KbCard.jsx` — show `indexing_status` badge + `chunk_count` for file KBs
- `frontend/src/components/kb/KbSetupDialog.jsx` — trigger reindex after save for file KBs
- `frontend/src/api/base44Client.js` (or wherever entities live) — add `KnowledgeBase.reindex(id)` / `.status(id)` helpers

---

## Task 1: Add dependencies

**Files:**
- Modify: `backend/requirements.txt`

**Step 1: Append the four new deps**

Add at the end of `backend/requirements.txt`:

```
# ── Document RAG (KB file source_kind) ─────────────────────────────
chromadb>=0.5.0,<0.7.0
sentence-transformers>=3.0,<5.0
pypdf>=4.0,<6.0
markdown>=3.5,<5.0
```

`python-docx` and `openpyxl` are already pinned. `chromadb` pulls in `onnxruntime` (CPU) transitively.

**Step 2: Install**

Run: `cd /root/zhanlu/backend && source venv/bin/activate && pip install -r requirements.txt`
Expected: installs succeed. Note: `sentence-transformers` first import will download the `all-MiniLM-L6-v2` model (~90 MB) into `~/.cache/huggingface`.

**Step 3: Commit**

```bash
git add backend/requirements.txt
git commit -m "deps: add chromadb, sentence-transformers, pypdf, markdown for document RAG"
```

---

## Task 2: Add KnowledgeBase index columns + migration

**Files:**
- Modify: `backend/app/models/knowledge_base.py`
- Create: `backend/alembic/versions/030_knowledge_base_document_indexing.py`

**Step 1: Add columns to the model**

Append after the `status` column in `KnowledgeBase`:

```python
    # Document indexing state (source_kind='file' only)
    indexing_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True, default=None
    )  # None | "pending" | "indexing" | "ready" | "failed"
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
```

Add `from datetime import datetime` to the imports at the top.

**Step 2: Write the migration**

Create `backend/alembic/versions/030_knowledge_base_document_indexing.py`. Follow the idempotent pattern from `029_chat_session_conversation_and_agent.py` (use `_column_exists` + `op.batch_alter_table`). Add the four columns to `knowledge_bases`. `revision = "030"`, `down_revision = "029"`. Downgrade drops them.

```python
"""030_knowledge_base_document_indexing

Revision ID: 030
Revises: 029
Create Date: 2026-07-27

Add document-indexing state columns to ``knowledge_bases`` so file-kind
KBs can track chunking/embedding progress for the RAG pipeline.
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    if op.get_context().as_sql:
        return False
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return result is not None


def upgrade() -> None:
    conn = op.get_bind()
    for col, typ in (
        ("indexing_status", sa.String(50)),
        ("chunk_count", sa.Integer()),
        ("index_error", sa.Text()),
        ("last_indexed_at", sa.DateTime()),
    ):
        if not _column_exists(conn, "knowledge_bases", col):
            with op.batch_alter_table("knowledge_bases") as batch_op:
                batch_op.add_column(sa.Column(col, typ, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_bases") as batch_op:
        batch_op.drop_column("last_indexed_at")
        batch_op.drop_column("index_error")
        batch_op.drop_column("chunk_count")
        batch_op.drop_column("indexing_status")
```

**Step 3: Run migration**

Run: `cd /root/zhanlu/backend && source venv/bin/activate && alembic upgrade head`
Expected: `Running upgrade 029 -> 030, ...`

**Step 4: Commit**

```bash
git add backend/app/models/knowledge_base.py backend/alembic/versions/030_knowledge_base_document_indexing.py
git commit -m "feat(kb): add document indexing columns + migration 030"
```

---

## Task 3: Text extractors

**Files:**
- Create: `backend/app/services/document_ingestion/__init__.py` (empty)
- Create: `backend/app/services/document_ingestion/extractors.py`
- Create: `backend/tests/test_document_ingestion.py` (start; extractor tests)

**Step 1: Write the failing test**

`backend/tests/test_document_ingestion.py`:

```python
"""Tests for document ingestion (extractors, chunker, embedder mock, store)."""

import pytest
from app.services.document_ingestion.extractors import extract_text


def test_extract_text_txt(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello world\nsecond line", encoding="utf-8")
    out = extract_text(str(p), file_type="txt")
    assert "hello world" in out
    assert "second line" in out


def test_extract_text_md(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("# Title\n\nSome **bold** text.", encoding="utf-8")
    out = extract_text(str(p), file_type="md")
    assert "Title" in out
    assert "bold" in out
    assert "#" not in out  # markdown stripped


def test_extract_text_csv(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")
    out = extract_text(str(p), file_type="csv")
    assert "name" in out and "age" in out
    assert "Alice" in out and "30" in out


def test_extract_text_unknown_type_returns_empty(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01")
    out = extract_text(str(p), file_type="bin")
    assert out == ""
```

**Step 2: Run — verify failure**

Run: `cd /root/zhanlu/backend && source venv/bin/activate && python -m pytest tests/test_document_ingestion.py -v -x`
Expected: FAIL with `ModuleNotFoundError: app.services.document_ingestion.extractors`

**Step 3: Implement extractors**

`backend/app/services/document_ingestion/extractors.py`:

```python
"""Per-format text extraction for document ingestion.

Each extractor returns a single string of plain text (newlines preserved).
Tables (CSV/XLSX) are serialised as header + rows so the chunker can split
on semantic boundaries. Returns "" for unsupported formats.
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_text(file_path: str, file_type: str) -> str:
    """Dispatch to the right extractor based on `file_type`.

    `file_type` is the normalised value stored on KnowledgeBase.file_type
    (one of: pdf, docx, csv, excel, xlsx, xls, md, txt, json). Unknown
    types return "".
    """
    ft = (file_type or "").lower().strip()
    try:
        if ft in ("txt", "text"):
            return _extract_txt(file_path)
        if ft == "md":
            return _extract_markdown(file_path)
        if ft == "csv":
            return _extract_csv(file_path)
        if ft in ("excel", "xlsx", "xls"):
            return _extract_excel(file_path)
        if ft == "pdf":
            return _extract_pdf(file_path)
        if ft == "docx":
            return _extract_docx(file_path)
        if ft == "json":
            return _extract_json(file_path)
        logger.warning("extract_text: unsupported file_type=%r", ft)
        return ""
    except Exception as e:
        logger.exception("extract_text failed for %s (type=%s): %s", file_path, ft, e)
        return ""


def _read_bytes(file_path: str) -> bytes:
    return Path(file_path).read_bytes()


def _extract_txt(file_path: str) -> str:
    return Path(file_path).read_text(encoding="utf-8", errors="replace")


def _extract_markdown(file_path: str) -> str:
    import markdown as md  # lazy

    raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
    html = md.markdown(raw)
    # strip HTML tags with a tiny regex — good enough for chunking
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+\n", "\n", text).strip()


def _extract_csv(file_path: str) -> str:
    rows: list[str] = []
    with open(file_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for r in reader:
            rows.append(", ".join(r))
    return "\n".join(rows)


def _extract_excel(file_path: str) -> str:
    from openpyxl import load_workbook  # lazy

    wb = load_workbook(file_path, read_only=True, data_only=True)
    out: list[str] = []
    for ws in wb.worksheets:
        out.append(f"## Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(cells):
                out.append(", ".join(cells))
    wb.close()
    return "\n".join(out)


def _extract_pdf(file_path: str) -> str:
    from pypdf import PdfReader  # lazy

    reader = PdfReader(file_path)
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        txt = page.extract_text() or ""
        pages.append(f"--- page {i + 1} ---\n{txt}")
    return "\n\n".join(pages)


def _extract_docx(file_path: str) -> str:
    from docx import Document  # lazy (python-docx)

    doc = Document(file_path)
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_json(file_path: str) -> str:
    import json

    raw = Path(file_path).read_text(encoding="utf-8", errors="replace")
    data = json.loads(raw)
    return json.dumps(data, indent=2, ensure_ascii=False)
```

**Step 4: Run — verify pass**

Run: `cd /root/zhanlu/backend && source venv/bin/activate && python -m pytest tests/test_document_ingestion.py -v`
Expected: 4 PASS

**Step 5: Commit**

```bash
git add backend/app/services/document_ingestion/__init__.py backend/app/services/document_ingestion/extractors.py backend/tests/test_document_ingestion.py
git commit -m "feat(rag): per-format text extractors"
```

---

## Task 4: Chunker

**Files:**
- Modify: `backend/app/services/document_ingestion/chunker.py` (create)
- Modify: `backend/tests/test_document_ingestion.py` (append chunker tests)

**Step 1: Write failing tests** (append to `test_document_ingestion.py`)

```python
from app.services.document_ingestion.chunker import chunk_text


def test_chunk_text_short_returns_one_chunk():
    out = chunk_text("hello world", max_tokens=800, overlap=100)
    assert len(out) == 1
    assert "hello world" in out[0]["text"]


def test_chunk_text_splits_long_text():
    # ~4000 chars => well over 800 tokens
    text = ("alpha beta gamma delta epsilon zeta eta theta. " * 200)
    out = chunk_text(text, max_tokens=800, overlap=100)
    assert len(out) > 1
    # every chunk has metadata
    for c in out:
        assert "text" in c and "index" in c


def test_chunk_text_respects_paragraph_boundary():
    text = "para one.\n\n" + ("word " * 1000) + "\n\npara three."
    out = chunk_text(text, max_tokens=200, overlap=20)
    assert len(out) >= 2
```

**Step 2: Run — verify failure**

Run: `python -m pytest tests/test_document_ingestion.py::test_chunk_text_short_returns_one_chunk -v -x`
Expected: FAIL (module missing)

**Step 3: Implement chunker**

`backend/app/services/document_ingestion/chunker.py`:

```python
"""Recursive text chunker — splits on paragraph / sentence boundaries.

Token budget is approximated with a word-count heuristic (1 token ≈ 0.75
words for English). Avoids pulling in a tokenizer dependency at chunk
time; the embedder does its own tokenisation.
"""

from __future__ import annotations

import re

_WORDS_PER_TOKEN = 0.75


def _approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) / _WORDS_PER_TOKEN))


def chunk_text(
    text: str,
    max_tokens: int = 800,
    overlap: int = 100,
) -> list[dict]:
    """Split `text` into chunks of <= max_tokens (approx), with overlap.

    Splits first on double-newlines (paragraphs), then on single newlines,
    then on sentence boundaries, then on words — accumulating into a
    buffer until the budget is hit. Returns list of
    ``{"text": str, "index": int, "token_count": int}``.
    """
    text = (text or "").strip()
    if not text:
        return []

    if _approx_tokens(text) <= max_tokens:
        return [{"text": text, "index": 0, "token_count": _approx_tokens(text)}]

    chunks: list[str] = []
    # Split into paragraphs, keep the separators
    paragraphs = re.split(r"(\n{2,})", text)
    buf: list[str] = []
    buf_tokens = 0

    def flush():
        nonlocal buf, buf_tokens
        if buf:
            chunks.append("".join(buf).strip())
            # keep overlap: carry last `overlap` tokens worth of text
            tail = " ".join(buf)
            tail_words = tail.split()
            keep = max(1, int(overlap / _WORDS_PER_TOKEN))
            buf = [" ".join(tail_words[-keep:]) + "\n\n"]
            buf_tokens = _approx_tokens(buf[0])
        else:
            buf = []
            buf_tokens = 0

    for para in paragraphs:
        para_tokens = _approx_tokens(para)
        if buf_tokens + para_tokens > max_tokens and buf:
            flush()
        # if a single paragraph is bigger than the budget, sub-split it
        if para_tokens > max_tokens:
            sub = _sub_split(para, max_tokens, overlap)
            for s in sub:
                if buf_tokens + _approx_tokens(s) > max_tokens and buf:
                    flush()
                buf.append(s)
                buf_tokens += _approx_tokens(s)
        else:
            buf.append(para)
            buf_tokens += para_tokens

    flush()
    return [
        {"text": c, "index": i, "token_count": _approx_tokens(c)}
        for i, c in enumerate(chunks)
        if c
    ]


def _sub_split(text: str, max_tokens: int, overlap: int) -> list[str]:
    """Split a too-large block on sentence boundaries, then words."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for s in sentences:
        st = _approx_tokens(s)
        if buf_tokens + st > max_tokens and buf:
            out.append(" ".join(buf))
            keep = max(1, int(overlap / _WORDS_PER_TOKEN))
            buf = buf[-keep:]
            buf_tokens = sum(_approx_tokens(x) for x in buf)
        if st > max_tokens:
            # single sentence still too big — hard word-split
            words = s.split()
            for i in range(0, len(words), max(1, int(max_tokens * _WORDS_PER_TOKEN))):
                out.append(" ".join(words[i:i + int(max_tokens * _WORDS_PER_TOKEN)]))
        else:
            buf.append(s)
            buf_tokens += st
    if buf:
        out.append(" ".join(buf))
    return out
```

**Step 4: Run — verify pass**

Run: `python -m pytest tests/test_document_ingestion.py -v`
Expected: 7 PASS

**Step 5: Commit**

```bash
git add backend/app/services/document_ingestion/chunker.py backend/tests/test_document_ingestion.py
git commit -m "feat(rag): recursive text chunker"
```

---

## Task 5: Embedder (lazy singleton)

**Files:**
- Create: `backend/app/services/document_ingestion/embedder.py`
- Modify: `backend/tests/test_document_ingestion.py` (append embedder mock test)

**Design note:** `sentence-transformers` import + model load is heavy (~500 MB RAM with torch). It MUST be lazy: never imported at module top-level of any file that the FastAPI app imports at startup. Only `embedder.py` imports it, and only inside `get_embedder()`. Tests mock it — they never load the real model.

**Step 1: Write failing test** (append)

```python
def test_embedder_singleton(monkeypatch):
    from app.services.document_ingestion import embedder
    calls = {"n": 0}

    class FakeModel:
        def encode(self, texts, **kw):
            calls["n"] += 1
            import numpy as np
            return np.array([[0.1, 0.2] for _ in texts])

    monkeypatch.setattr(embedder, "_load_model", lambda: FakeModel())
    embedder.reset_for_tests()
    e1 = embedder.get_embedder()
    e2 = embedder.get_embedder()
    assert e1 is e2  # singleton
    vecs = embedder.embed_texts(["a", "b"])
    assert vecs.shape == (2, 2)
    assert calls["n"] == 1  # model loaded once
    embedder.reset_for_tests()
```

**Step 2: Run — verify failure**

Run: `python -m pytest tests/test_document_ingestion.py::test_embedder_singleton -v -x`
Expected: FAIL (module missing)

**Step 3: Implement embedder**

`backend/app/services/document_ingestion/embedder.py`:

```python
"""Lazy singleton wrapper around sentence-transformers all-MiniLM-L6-v2.

NEVER import sentence-transformers at module top level — it pulls in
torch and ~500 MB of RAM. All imports happen inside ``_load_model()``,
which is called exactly once on first use and cached on the module
global ``_MODEL``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_EMBED_DIM = 384

_MODEL: Any = None
_LOCK = threading.Lock()


def _load_model() -> Any:
    """Actually import + load the sentence-transformers model.

    Patchable by tests (monkeypatch this to return a fake).
    """
    from sentence_transformers import SentenceTransformer  # lazy import

    logger.info("Loading embedding model %r ...", _MODEL_NAME)
    return SentenceTransformer(_MODEL_NAME)


def get_embedder() -> Any:
    """Return the singleton model instance, loading it on first call."""
    global _MODEL
    if _MODEL is None:
        with _LOCK:
            if _MODEL is None:
                _MODEL = _load_model()
    return _MODEL


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a batch of texts. Returns shape (n, 384)."""
    if not texts:
        return np.zeros((0, _EMBED_DIM), dtype="float32")
    model = get_embedder()
    vecs = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return vecs.astype("float32")


def embed_query(text: str) -> np.ndarray:
    """Embed a single query string. Returns shape (384,)."""
    return embed_texts([text])[0]


def embed_dim() -> int:
    return _EMBED_DIM


def reset_for_tests() -> None:
    """Test-only: clear the cached singleton."""
    global _MODEL
    _MODEL = None
```

**Step 4: Run — verify pass**

Run: `python -m pytest tests/test_document_ingestion.py -v`
Expected: 8 PASS

**Step 5: Commit**

```bash
git add backend/app/services/document_ingestion/embedder.py backend/tests/test_document_ingestion.py
git commit -m "feat(rag): lazy singleton embedder (all-MiniLM-L6-v2)"
```

---

## Task 6: Chroma store

**Files:**
- Create: `backend/app/services/document_ingestion/store.py`
- Modify: `backend/tests/test_document_ingestion.py` (append store test)

**Design:** One Chroma collection per `org_id`, named `kb_{org_id}`. Persisted under `backend/data/chroma/`. Each chunk stored with metadata `{kb_id, file_name, file_type, chunk_index, source_kind}` so retrieval can filter by `kb_id` and we can delete all chunks for a KB on reindex.

**Step 1: Write failing test** (append)

```python
def test_store_upsert_and_query(tmp_path, monkeypatch):
    from app.services.document_ingestion import store, embedder

    class FakeModel:
        def encode(self, texts, **kw):
            import numpy as np
            return np.array([[1.0, 0.0] if "cat" in t else [0.0, 1.0] for t in texts])
    monkeypatch.setattr(embedder, "_load_model", lambda: FakeModel())
    embedder.reset_for_tests()
    monkeypatch.setattr(store, "_CHROMA_DIR", str(tmp_path))

    store.upsert_chunks(
        org_id="org-1",
        kb_id="kb-1",
        chunks=[
            {"text": "the cat sat", "index": 0},
            {"text": "the dog ran", "index": 1},
        ],
        metas=[
            {"file_name": "a.txt", "file_type": "txt"},
            {"file_name": "a.txt", "file_type": "txt"},
        ],
    )
    res = store.query(org_id="org-1", kb_ids=["kb-1"], query_text="cat", top_k=1)
    assert res["chunks"]
    assert "cat" in res["chunks"][0]["text"]
    # delete and confirm empty
    store.delete_kb(org_id="org-1", kb_id="kb-1")
    res2 = store.query(org_id="org-1", kb_ids=["kb-1"], query_text="cat", top_k=1)
    assert not res2["chunks"]
    embedder.reset_for_tests()
```

**Step 2: Run — verify failure**

Run: `python -m pytest tests/test_document_ingestion.py::test_store_upsert_and_query -v -x`
Expected: FAIL

**Step 3: Implement store**

`backend/app/services/document_ingestion/store.py`:

```python
"""ChromaDB vector store — one collection per org_id.

Persistence dir is ``backend/data/chroma/`` (configurable via the
``CHROMA_DIR`` env var). Collection name is ``kb_{org_id}`` so all KBs
in one org share an index but chunks are tagged with their ``kb_id`` in
metadata, allowing per-KB delete + filtered query.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from app.services.document_ingestion import embedder

logger = logging.getLogger(__name__)

_CHROMA_DIR = os.environ.get(
    "CHROMA_DIR", str(Path(__file__).resolve().parents[3] / "data" / "chroma")
)

_CLIENT: Any = None


def _get_client() -> Any:
    global _CLIENT
    if _CLIENT is None:
        import chromadb  # lazy

        Path(_CHROMA_DIR).mkdir(parents=True, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(path=_CHROMA_DIR)
    return _CLIENT


def _collection_name(org_id: str) -> str:
    return f"kb_{org_id}"


def _get_collection(org_id: str) -> Any:
    client = _get_client()
    return client.get_or_create_collection(
        name=_collection_name(org_id),
        metadata={"hnsw:space": "cosine"},
    )


def upsert_chunks(
    org_id: str,
    kb_id: str,
    chunks: list[dict],
    metas: list[dict],
) -> int:
    """Embed + upsert chunks for one KB. Returns count stored."""
    if not chunks:
        return 0
    texts = [c["text"] for c in chunks]
    vecs = embedder.embed_texts(texts)
    coll = _get_collection(org_id)
    ids = [f"{kb_id}_{c['index']}_{uuid.uuid4().hex[:8]}" for c in chunks]
    metadatas = [
        {
            "kb_id": kb_id,
            "chunk_index": c["index"],
            "file_name": m.get("file_name", ""),
            "file_type": m.get("file_type", ""),
            **({"page": m["page"]} if m.get("page") is not None else {}),
        }
        for c, m in zip(chunks, metas)
    ]
    coll.upsert(ids=ids, embeddings=vecs.tolist(), documents=texts, metadatas=metadatas)
    return len(ids)


def delete_kb(org_id: str, kb_id: str) -> None:
    """Delete all chunks for one KB."""
    coll = _get_collection(org_id)
    coll.delete(where={"kb_id": kb_id})


def query(
    org_id: str,
    kb_ids: list[str],
    query_text: str,
    top_k: int = 5,
) -> dict:
    """Vector search across one or more KBs in the org. Returns
    ``{"chunks": [{"text","score","kb_id","file_name","chunk_index"}], }``.
    """
    if not kb_ids:
        return {"chunks": []}
    qvec = embedder.embed_query(query_text).tolist()
    coll = _get_collection(org_id)
    # Chroma `where` is AND across the where clause; for multiple kb_ids
    # we use `$in`.
    res = coll.query(
        query_embeddings=[qvec],
        n_results=top_k * len(kb_ids),
        where={"kb_id": {"$in": list(kb_ids)}},
    )
    out: list[dict] = []
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    for doc, meta, dist in zip(docs, metas, dists):
        out.append({
            "text": doc,
            "score": 1.0 - float(dist),  # cosine distance → similarity
            "kb_id": meta.get("kb_id"),
            "file_name": meta.get("file_name", ""),
            "file_type": meta.get("file_type", ""),
            "chunk_index": meta.get("chunk_index", 0),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return {"chunks": out[:top_k]}


def count(org_id: str, kb_id: str) -> int:
    """Return chunk count for one KB."""
    coll = _get_collection(org_id)
    try:
        got = coll.count(where={"kb_id": kb_id})
        return int(got)
    except Exception:
        return 0


def reset_for_tests() -> None:
    global _CLIENT
    _CLIENT = None
```

**Step 4: Run — verify pass**

Run: `python -m pytest tests/test_document_ingestion.py -v`
Expected: 9 PASS

**Step 5: Commit**

```bash
git add backend/app/services/document_ingestion/store.py backend/tests/test_document_ingestion.py
git commit -m "feat(rag): ChromaDB vector store (per-org collection)"
```

---

## Task 7: Ingestion orchestrator

**Files:**
- Create: `backend/app/services/document_ingestion/service.py`
- Modify: `backend/tests/test_document_ingestion.py` (append service test)

**Design:** `ingest_kb(db, kb_id)` is the single entry point. It: loads the KB row, resolves the local file path from `file_url` (strip `/api/uploads/` prefix → `settings.upload_path / name`), extracts text, chunks, upserts into Chroma, updates KB columns. Sets `indexing_status` to `pending` → `indexing` → `ready`/`failed`. Runs in a thread via `asyncio.to_thread` so the HTTP handler stays async.

**Step 1: Write failing test** (append)

```python
def test_ingest_kb_end_to_end(tmp_path, monkeypatch):
    from app.services.document_ingestion import service, store, embedder
    from app.models.knowledge_base import KnowledgeBase
    from app.database import SessionLocal, engine, Base

    class FakeModel:
        def encode(self, texts, **kw):
            import numpy as np
            return np.array([[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(embedder, "_load_model", lambda: FakeModel())
    embedder.reset_for_tests()
    monkeypatch.setattr(store, "_CHROMA_DIR", str(tmp_path / "chroma"))

    # write an upload file
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "note.txt").write_text("hello world from doc", encoding="utf-8")

    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        kb = KnowledgeBase(
            name="doc",
            source_kind="file",
            file_type="txt",
            file_url="/api/uploads/note.txt",
            org_id="org-x",
            app_id="app-x",
        )
        db.add(kb); db.commit(); db.refresh(kb)

        # patch upload path resolver
        monkeypatch.setattr(service, "_upload_root", lambda: upload_dir)

        ok = service.ingest_kb(db, kb.id)
        assert ok is True
        db.refresh(kb)
        assert kb.indexing_status == "ready"
        assert kb.chunk_count and kb.chunk_count >= 1
        assert kb.last_indexed_at is not None
    finally:
        db.close()
        embedder.reset_for_tests()
        store.reset_for_tests()
```

**Step 2: Run — verify failure**

Run: `python -m pytest tests/test_document_ingestion.py::test_ingest_kb_end_to_end -v -x`
Expected: FAIL

**Step 3: Implement service**

`backend/app/services/document_ingestion/service.py`:

```python
"""Ingestion orchestrator — turn a file-kind KB into embedded chunks.

Entry point ``ingest_kb(db, kb_id)`` is synchronous (run inside
``asyncio.to_thread`` from the HTTP layer). It updates
``KnowledgeBase.indexing_status`` through the lifecycle
``pending → indexing → ready | failed`` so the UI can poll.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.document_ingestion import chunker, extractors, store

logger = logging.getLogger(__name__)


def _upload_root() -> Path:
    """Patchable in tests. Returns the on-disk uploads directory."""
    from app.config import settings
    return settings.upload_path


def _resolve_local_path(file_url: str) -> Path | None:
    """Turn `/api/uploads/<name>` into an absolute filesystem path."""
    if not file_url:
        return None
    prefix = "/api/uploads/"
    if file_url.startswith(prefix):
        return _upload_root() / file_url[len(prefix):]
    # already absolute path
    p = Path(file_url)
    if p.is_absolute():
        return p
    return _upload_root() / file_url


def ingest_kb(db: Session, kb_id: str) -> bool:
    """Extract → chunk → embed → upsert for one KB. Returns True on success."""
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        logger.warning("ingest_kb: kb %s not found", kb_id)
        return False
    if kb.source_kind != "file":
        logger.info("ingest_kb: kb %s is source_kind=%s, skipping", kb_id, kb.source_kind)
        return False

    kb.indexing_status = "pending"
    kb.index_error = None
    db.commit()

    try:
        local = _resolve_local_path(kb.file_url or "")
        if local is None or not local.exists():
            raise FileNotFoundError(f"file not found on disk: {local} (url={kb.file_url})")

        text = extractors.extract_text(str(local), kb.file_type or "")
        if not text.strip():
            raise ValueError("extracted text is empty")

        chunks = chunker.chunk_text(text, max_tokens=800, overlap=100)
        if not chunks:
            raise ValueError("chunker produced 0 chunks")

        # wipe previous chunks for this KB (reindex path)
        store.delete_kb(org_id=kb.org_id, kb_id=kb.id)

        metas = [
            {"file_name": local.name, "file_type": kb.file_type or ""}
            for _ in chunks
        ]
        n = store.upsert_chunks(
            org_id=kb.org_id, kb_id=kb.id, chunks=chunks, metas=metas
        )

        kb.indexing_status = "ready"
        kb.chunk_count = n
        kb.index_error = None
        kb.last_indexed_at = datetime.utcnow()
        db.commit()
        logger.info("ingest_kb: kb %s ready, %d chunks", kb_id, n)
        return True

    except Exception as e:
        logger.exception("ingest_kb failed for kb %s: %s", kb_id, e)
        kb.indexing_status = "failed"
        kb.index_error = str(e)[:500]
        db.commit()
        return False


def delete_index(db: Session, kb_id: str) -> None:
    """Drop all vectors for a KB (call on delete)."""
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    if kb is None:
        return
    try:
        store.delete_kb(org_id=kb.org_id, kb_id=kb_id)
    except Exception as e:
        logger.warning("delete_index failed for kb %s: %s", kb_id, e)


def get_status(db: Session, kb_id: str) -> dict:
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        return {"found": False}
    return {
        "found": True,
        "kb_id": kb.id,
        "indexing_status": kb.indexing_status,
        "chunk_count": kb.chunk_count,
        "index_error": kb.index_error,
        "last_indexed_at": kb.last_indexed_at.isoformat() if kb.last_indexed_at else None,
    }
```

**Step 4: Run — verify pass**

Run: `python -m pytest tests/test_document_ingestion.py -v`
Expected: 10 PASS

**Step 5: Commit**

```bash
git add backend/app/services/document_ingestion/service.py backend/tests/test_document_ingestion.py
git commit -m "feat(rag): ingestion orchestrator with status lifecycle"
```

---

## Task 8: Retrieval tools (`answer_from_documents`, `search_documents`)

**Files:**
- Create: `backend/app/services/document_ingestion/retrieval.py`
- Modify: `backend/app/services/tool_handlers/db_tools.py` (register the two tools)
- Create: `backend/tests/test_document_retrieval_tool.py`

**Design:** Two tools, mirroring the DB pair:

- `search_documents(data_source_id, query, top_k=5)` → returns raw chunks (granular)
- `answer_from_documents(data_source_id, question)` → vector search + LLM synthesis into prose (high-level)

Both enforce `bound_kb_ids` scoping via `_require_kb_id` (already in db_tools.py).

**Step 1: Write failing test**

`backend/tests/test_document_retrieval_tool.py`:

```python
"""Tests for the document retrieval tools."""
import pytest


@pytest.mark.asyncio
async def test_search_documents_requires_kb_id(monkeypatch):
    from app.services.tool_handlers import db_tools
    res = await db_tools._search_documents({}, db=None, user_id=None, context={})
    assert res["success"] is False
    assert "data_source_id" in res["error"]


@pytest.mark.asyncio
async def test_search_documents_unbound_kb_rejected(monkeypatch):
    from app.services.tool_handlers import db_tools
    res = await db_tools._search_documents(
        {"data_source_id": "kb-x", "query": "foo"},
        db=None, user_id=None,
        context={"bound_kb_ids": ["kb-other"]},
    )
    assert res["success"] is False
    assert "not bound" in res["error"]
```

**Step 2: Run — verify failure**

Run: `python -m pytest tests/test_document_retrieval_tool.py -v -x`
Expected: FAIL (handler missing)

**Step 3: Implement retrieval module**

`backend/app/services/document_ingestion/retrieval.py`:

```python
"""Retrieval helpers for the document tools.

``search_documents`` returns raw chunks; ``answer_from_documents``
retrieves then synthesises a prose answer via a single LLM call. Both
are called by the ``data_agent`` subagent — never by the user-facing
agent directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.services.document_ingestion import store
from app.services.sub_agent_reliability import call_llm_with_reliability

logger = logging.getLogger(__name__)


def _load_kb(db: Session, kb_id: str) -> KnowledgeBase | None:
    return (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )


def search(db: Session, kb_id: str, query: str, top_k: int = 5) -> dict:
    """Vector search. Returns ``{"success", "chunks", "source_id", "source_name"}``."""
    kb = _load_kb(db, kb_id)
    if kb is None:
        return {"success": False, "error": f"KnowledgeBase {kb_id!r} not found"}
    if kb.source_kind != "file":
        return {
            "success": False,
            "error": f"KnowledgeBase {kb_id!r} is source_kind={kb.source_kind!r}, "
                     f"not 'file'. Use the database tools instead.",
        }
    if kb.indexing_status != "ready":
        return {
            "success": False,
            "error": (
                f"Document index is not ready (status={kb.indexing_status!r}). "
                f"Wait for indexing to finish or re-trigger it from My Space."
            ),
        }
    top_k = max(1, min(int(top_k), 20))
    res = store.query(
        org_id=kb.org_id, kb_ids=[kb_id], query_text=query, top_k=top_k
    )
    return {
        "success": True,
        "chunks": res["chunks"],
        "source_id": kb.id,
        "source_name": kb.name,
        "file_name": kb.file_url or "",
    }


async def answer(db: Session, kb_id: str, question: str) -> dict:
    """Vector search + LLM synthesis → prose answer with citations."""
    top_k = 6
    sr = search(db, kb_id, question, top_k=top_k)
    if not sr.get("success"):
        return sr
    chunks = sr["chunks"]
    if not chunks:
        return {
            "success": True,
            "answer": (
                f"No relevant passages found in {sr['source_name']!r} "
                f"for that question."
            ),
            "chunks": [],
            "source_id": kb_id,
            "source_name": sr["source_name"],
        }

    context_block = "\n\n".join(
        f"[{i + 1}] (score={c['score']:.3f}, file={c['file_name']}, chunk={c['chunk_index']})\n{c['text']}"
        for i, c in enumerate(chunks)
    )
    prompt = (
        "Answer the user's question using ONLY the passages below. "
        "Cite passages by their [N] index. If the passages don't contain "
        "the answer, say so explicitly — do not fabricate.\n\n"
        f"PASSAGES:\n{context_block}\n\n"
        f"QUESTION: {question}\n\nANSWER:"
    )
    messages = [{"role": "user", "content": prompt}]
    try:
        resp = await call_llm_with_reliability(messages, tools=[], temperature=0.2)
        prose = (resp.get("content") or "").strip()
    except Exception as e:
        logger.warning("answer_from_documents synthesis failed: %s", e)
        prose = (
            f"Found {len(chunks)} relevant passage(s) in {sr['source_name']!r} "
            f"but could not synthesise an answer ({e})."
        )

    return {
        "success": True,
        "answer": prose,
        "chunks": chunks,
        "source_id": kb_id,
        "source_name": sr["source_name"],
        "citations": [
            {"file_name": c["file_name"], "chunk_index": c["chunk_index"], "score": c["score"]}
            for c in chunks
        ],
    }
```

**Step 4: Register tools in db_tools.py**

Append to `backend/app/services/tool_handlers/db_tools.py` (before the registration loop, after `_answer_from_database`):

```python
# ---------------------------------------------------------------------------
# Document tools (source_kind='file')
# ---------------------------------------------------------------------------

async def _search_documents(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Vector search over a bound file-kind KB. Returns raw chunks."""
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    query = (args.get("query") or args.get("question") or "").strip()
    if not query:
        return {"success": False, "error": "query (or question) is required"}
    top_k = int(args.get("top_k", 5))
    from app.services.document_ingestion import retrieval
    return retrieval.search(db, kb_id, query, top_k=top_k)


async def _answer_from_documents(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """End-to-end: vector search + LLM synthesis → prose answer with citations."""
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    from app.services.document_ingestion import retrieval
    return await retrieval.answer(db, kb_id, question)
```

Add the two schemas + register them. Append two schema dicts and extend the registration loop:

```python
SEARCH_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Semantic search over a bound document data source "
            "(source_kind='file'). Returns the top-k matching passages "
            "with scores and file metadata. Use this for granular "
            "retrieval; use answer_from_documents for a one-shot prose answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound document data source.",
                },
                "query": {
                    "type": "string",
                    "description": "The natural-language search query.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max passages to return (default 5, max 20).",
                    "default": 5,
                },
            },
            "required": ["data_source_id", "query"],
        },
    },
}

ANSWER_FROM_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "answer_from_documents",
        "description": (
            "End-to-end document answer. Pass a question, get back a "
            "prose 'answer' grounded in the top passages of the bound "
            "document data source, plus 'chunks' and 'citations' "
            "(file_name, chunk_index, score). Use this for simple "
            "questions; for multi-step reasoning use search_documents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound document data source.",
                },
                "question": {
                    "type": "string",
                    "description": "The natural-language question to answer.",
                },
            },
            "required": ["data_source_id", "question"],
        },
    },
}
```

Extend the registration loop at the bottom of `db_tools.py`:

```python
for _name, _schema, _handler, _desc in (
    ("list_data_sources", LIST_DATA_SOURCES_SCHEMA, _list_data_sources,
     "List data sources bound to this agent."),
    ("describe_schema", DESCRIBE_SCHEMA_SCHEMA, _describe_schema,
     "Introspect the schema of a bound data source."),
    ("execute_query", EXECUTE_QUERY_SCHEMA, _execute_query,
     "Run a SQL statement against a bound data source."),
    ("answer_from_database", ANSWER_FROM_DATABASE_SCHEMA, _answer_from_database,
     "End-to-end NL2SQL answer from a bound data source."),
    ("search_documents", SEARCH_DOCUMENTS_SCHEMA, _search_documents,
     "Semantic search over a bound document data source."),
    ("answer_from_documents", ANSWER_FROM_DOCUMENTS_SCHEMA, _answer_from_documents,
     "End-to-end answer from a bound document data source."),
):
    registry.register(
        name=_name,
        schema=_schema,
        handler=_handler,
        category="database",
        enabled_by_default=False,  # subagent-only
        description=_desc,
    )
```

**Step 5: Run — verify pass**

Run: `python -m pytest tests/test_document_retrieval_tool.py -v`
Expected: 2 PASS

**Step 6: Commit**

```bash
git add backend/app/services/document_ingestion/retrieval.py backend/app/services/tool_handlers/db_tools.py backend/tests/test_document_retrieval_tool.py
git commit -m "feat(rag): search_documents + answer_from_documents tools"
```

---

## Task 9: Wire doc tools into the data_agent subagent

**Files:**
- Modify: `backend/app/services/tool_handlers/delegation_tools.py`
- Modify: `backend/app/services/agent_definitions/__init__.py` (DATA_AGENT_PROMPT)
- Modify: `backend/app/services/tool_handlers/db_tools.py` (`_list_data_sources` returns file KBs)

**Step 1: Add doc tools to the data_agent's allowed toolset**

In `backend/app/services/tool_handlers/delegation_tools.py`, extend `_DATA_AGENT_TOOLS`:

```python
_DATA_AGENT_TOOLS = [
    "list_data_sources",
    "describe_schema",
    "execute_query",
    "answer_from_database",
    "search_documents",
    "answer_from_documents",
]
```

**Step 2: Make `list_data_sources` surface file KBs too**

In `backend/app/services/tool_handlers/db_tools.py`, edit `_list_data_sources` so the returned dicts include `source_kind`, `file_type`, `indexing_status`, and `chunk_count`:

```python
    return {
        "success": True,
        "data_sources": [
            {
                "id": kb.id,
                "name": kb.name,
                "description": kb.description or "",
                "source_kind": kb.source_kind or "database",
                "db_type": kb.db_type or "",
                "database_name": kb.database_name or "",
                "file_type": kb.file_type or "",
                "indexing_status": kb.indexing_status,
                "chunk_count": kb.chunk_count or 0,
            }
            for kb in rows
        ],
    }
```

Also update the empty-list message to mention documents:

```python
            "message": "This agent has no data sources bound. "
                       "Bind a KnowledgeBase of source_kind='database' or "
                       "source_kind='file' in the agent's Data Sources section.",
```

**Step 3: Extend DATA_AGENT_PROMPT**

In `backend/app/services/agent_definitions/__init__.py`, append to the `DATA_AGENT_PROMPT` string (after the existing `TOOLS` section, before `OUTPUT CONTRACT`):

```
DOCUMENT DATA SOURCES
- Some bound data sources have ``source_kind == 'file'`` — these are
  uploaded documents (PDF, DOCX, CSV, XLSX, MD, TXT) that have been
  chunked and embedded. For those, use the DOCUMENT tools, NOT SQL:
  - ``search_documents(data_source_id, query, [top_k])``: semantic search,
    returns raw passages with scores. Use for granular / multi-step work.
  - ``answer_from_documents(data_source_id, question)``: one-shot —
    retrieves top passages and synthesises a prose answer with citations.
    Prefer this for simple questions.
- Pick the right tool family by checking each source's ``source_kind``
  from ``list_data_sources``. NEVER call ``execute_query`` /
  ``describe_schema`` on a file source, and NEVER call
  ``search_documents`` / ``answer_from_documents`` on a database source.
- If a file source's ``indexing_status`` is not ``'ready'``, tell the
  caller the document is still being indexed and they should retry shortly.
```

**Step 4: Smoke test the wiring**

Run: `cd /root/zhanlu/backend && source venv/bin/activate && python -c "from app.services.tool_handlers import db_tools, delegation_tools; print('tools:', [t for t in delegation_tools._DATA_AGENT_TOOLS]); print('schemas:', [n for n in ['search_documents','answer_from_documents'] if db_tools.registry.get_entry(n)])"`
Expected: prints the 6 tool names and confirms both doc tools are registered.

**Step 5: Commit**

```bash
git add backend/app/services/tool_handlers/delegation_tools.py backend/app/services/tool_handlers/db_tools.py backend/app/services/agent_definitions/__init__.py
git commit -m "feat(rag): wire doc tools into data_agent; surface file KBs in list_data_sources"
```

---

## Task 10: Stop skipping file KBs in the prompt builder

**Files:**
- Modify: `backend/app/services/data_source_runtime/data_source_runtime.py`

**Step 1: Update `_load_bound_kb_meta` to include file fields**

In `data_source_runtime.py`, extend the dict built in `_load_bound_kb_meta`:

```python
    return [
        {
            "id": kb.id,
            "name": kb.name,
            "db_type": kb.db_type or "",
            "database_name": kb.database_name or "",
            "source_kind": kb.source_kind or "",
            "file_type": kb.file_type or "",
            "indexing_status": kb.indexing_status,
            "chunk_count": kb.chunk_count or 0,
        }
        for kb in rows
    ]
```

**Step 2: Rewrite `_build_data_source_prompt_section` to handle both kinds**

Replace the body. Stop skipping non-database KBs; instead describe each kind with the right tool guidance:

```python
def _build_data_source_prompt_section(bound_meta: list[dict]) -> str:
    """Compose the 'Bound Data Sources' section appended to the system prompt."""
    if not bound_meta:
        return ""

    db_meta = [k for k in bound_meta if k["source_kind"] == "database"]
    file_meta = [k for k in bound_meta if k["source_kind"] == "file"]
    other_meta = [k for k in bound_meta if k["source_kind"] not in ("database", "file")]

    lines = ["## Bound Data Sources", ""]

    if db_meta:
        lines.append("### Database sources")
        for kb in db_meta:
            label = f"- **{kb['name']}** (id=`{kb['id']}`, db_type=`{kb['db_type']}`"
            if kb["database_name"]:
                label += f", database=`{kb['database_name']}`"
            label += ")"
            lines.append(label)
        lines.append("")

    if file_meta:
        lines.append("### Document sources")
        for kb in file_meta:
            status = kb.get("indexing_status") or "unknown"
            cc = kb.get("chunk_count") or 0
            label = (
                f"- **{kb['name']}** (id=`{kb['id']}`, file_type=`{kb['file_type']}`, "
                f"status=`{status}`, chunks={cc})"
            )
            lines.append(label)
        lines.append("")

    lines.extend([
        "**MANDATORY TOOL — call the function whose name is exactly `ask_data_agent`.**",
        "This is the ONLY way to reach the bound data sources (database OR document).",
        "You do not have direct SQL or vector-search access; you delegate to the Data",
        "Agent, which picks the right internal tool (SQL for databases, vector",
        "retrieval for documents) based on each source's `source_kind`.",
        "",
        "**Function signature:**",
        "```",
        "ask_data_agent(",
        "    question: str,                # required — the natural-language question",
        "    data_source_id: str = None,   # optional — id of a bound source",
        "    max_iterations: int = 6,      # optional — cap on subagent rounds (max 10)",
        ")",
        "```",
        "",
        "**When to call:**",
        "- Any time the user asks about, references, or implies data from the bound sources.",
        "- Any time a downstream step (report, chart, summary) needs real numbers or facts",
        "  — fetch them via `ask_data_agent` first.",
        "",
        "**Anti-patterns:**",
        "- Do NOT fabricate data, invent values, or generate tables without calling the tool.",
        "- Do NOT narrate a workflow ('query schema', 'run SQL', 'present results') without",
        "  an actual tool call — that is a hallucination.",
    ])
    return "\n".join(lines)
```

**Step 3: Make the anti-hallucination directive cover documents too**

Update `_CRITICAL_ANTI_HALLUCINATION_DIRECTIVE` (top of file) — change "bound database data sources" to "bound data sources (database or document)":

```python
_CRITICAL_ANTI_HALLUCINATION_DIRECTIVE = (
    "CRITICAL RULE: You have bound data sources (database and/or document). "
    "For ANY data question, you MUST call the `ask_data_agent` tool FIRST. "
    "Do NOT fabricate data, invent customer names, or generate data tables "
    "without calling `ask_data_agent`. Once you receive real data from "
    "`ask_data_agent`, summarize the key findings clearly: name the top "
    "performers, cite totals and shares, note the time period, and highlight "
    "any notable patterns. Every number you mention must come from the tool "
    "result — but you SHOULD produce a substantive narrative, not just a "
    "one-line handoff."
)
```

**Step 4: Make auto-bind + project-scope include file KBs**

In `_maybe_extend_with_workspace_auto_bind`, the query filters on `KnowledgeBase.source_kind == "database"`. Drop that filter so file KBs are auto-bound too:

```python
    rows = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.is_deleted == False,  # noqa: E712
            KnowledgeBase.org_id == org_id,
            KnowledgeBase.app_id == app_id,
        )
        .order_by(KnowledgeBase.name.asc())
        .all()
    )
```

**Step 5: Smoke test**

Run: `cd /root/zhanlu/backend && source venv/bin/activate && python -c "from app.services.data_source_runtime.data_source_runtime import _build_data_source_prompt_section; print(_build_data_source_prompt_section([{'id':'1','name':'Sales DB','db_type':'postgresql','database_name':'sales','source_kind':'database','file_type':'','indexing_status':None,'chunk_count':0},{'id':'2','name':'Manual','db_type':'','database_name':'','source_kind':'file','file_type':'pdf','indexing_status':'ready','chunk_count':42}]))"`
Expected: prints a section listing both the database source and the document source.

**Step 6: Commit**

```bash
git add backend/app/services/data_source_runtime/data_source_runtime.py
git commit -m "feat(rag): prompt builder surfaces file KBs; auto-bind includes documents"
```

---

## Task 11: KB router — `/reindex` and `/status` endpoints

**Files:**
- Create: `backend/app/routers/knowledge_bases.py`
- Modify: `backend/main.py`

**Design:** Two endpoints, both under `/api/apps/{app_id}/knowledge_bases`:
- `POST /{kb_id}/reindex` — kicks off ingestion synchronously (in a thread) and returns the new status. For small docs this is fast; for large ones the UI can poll `/status`.
- `GET /{kb_id}/status` — returns `indexing_status`, `chunk_count`, `index_error`, `last_indexed_at`.

**Step 1: Implement router**

`backend/app/routers/knowledge_bases.py`:

```python
"""Custom endpoints for KnowledgeBase — document reindex + status.

The generic entity router still owns CRUD; this router adds the two
document-RAG-specific actions.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_db
from app.models.knowledge_base import KnowledgeBase
from app.services.document_ingestion import service as ingestion_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps/{app_id}/knowledge_bases", tags=["KnowledgeBase"])


@router.post("/{kb_id}/reindex")
async def reindex_kb(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Re-extract, re-chunk, re-embed a file-kind KB. Returns new status."""
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    if kb.source_kind != "file":
        raise HTTPException(
            status_code=400,
            detail=f"Reindex is only for source_kind='file' (got {kb.source_kind!r})",
        )
    # Run ingestion in a thread so we don't block the event loop.
    ok = await asyncio.to_thread(ingestion_service.ingest_kb, db, kb_id)
    status = ingestion_service.get_status(db, kb_id)
    return {"success": ok, **status}


@router.get("/{kb_id}/status")
async def kb_status(app_id: str, kb_id: str, db: Session = Depends(get_db)):
    """Return the indexing status of a file-kind KB."""
    status = ingestion_service.get_status(db, kb_id)
    if not status.get("found"):
        raise HTTPException(status_code=404, detail="KnowledgeBase not found")
    return status
```

**Step 2: Register in main.py**

In `backend/main.py`, add the import with the other router imports:

```python
from app.routers.knowledge_bases import router as knowledge_bases_router
```

And add the include line (after the entity-router loop, near the other custom routers):

```python
    app.include_router(knowledge_bases_router, prefix="/api")
```

**Step 3: Smoke test**

Run: `cd /root/zhanlu/backend && source venv/bin/activate && python -c "from main import create_app; app = create_app(); routes = [r.path for r in app.routes if 'knowledge_bases' in getattr(r,'path','')]; print(routes)"`
Expected: includes `/api/apps/{app_id}/knowledge_bases/{kb_id}/reindex` and `/api/apps/{app_id}/knowledge_bases/{kb_id}/status`.

**Step 4: Commit**

```bash
git add backend/app/routers/knowledge_bases.py backend/main.py
git commit -m "feat(rag): KB /reindex + /status endpoints"
```

---

## Task 12: Frontend — accept DOCX/MD, show indexing status, trigger reindex

**Files:**
- Modify: `frontend/src/components/kb/KbFileFields.jsx`
- Modify: `frontend/src/components/kb/KbCard.jsx`
- Modify: `frontend/src/components/kb/KbSetupDialog.jsx`

**Step 1: Add `.docx` and `.md` to the upload accept list**

In `KbFileFields.jsx`, change the `accept` attribute:

```jsx
<input type="file" accept=".pdf,.docx,.md,.csv,.xlsx,.xls,.json,.txt" className="hidden" onChange={handleFile} disabled={uploading} />
```

Also extend `extOf` so `.md` is kept as `md` (it already is) and `.docx` → `docx` (currently it falls through to `docx` which is fine, but make it explicit):

```js
function extOf(name) {
  const m = (name || '').toLowerCase().match(/\.([a-z0-9]+)$/);
  if (!m) return '';
  const e = m[1];
  if (e === 'xlsx' || e === 'xls') return 'excel';
  return e;
}
```

**Step 2: Add a reindex call after saving a file KB**

In `KbSetupDialog.jsx`, after `onSaved?.(saved)`, if the saved KB is `source_kind === 'file'`, fire a reindex (fire-and-forget; the UI will poll status via `KbCard`):

```jsx
      const saved = editItem
        ? await base44.entities.KnowledgeBase.update(editItem.id, payload)
        : await base44.entities.KnowledgeBase.create(payload);
      // Kick off document indexing for file KBs (fire-and-forget).
      if (saved?.source_kind === 'file' && saved?.file_url) {
        try {
          await fetch(`/api/apps/${saved.app_id || 'default-app'}/knowledge_bases/${saved.id}/reindex`, { method: 'POST' });
        } catch { /* non-fatal — user can reindex from the card */ }
      }
      onSaved?.(saved);
      onOpenChange(false);
```

**Step 3: Show indexing status + chunk count on `KbCard`**

In `KbCard.jsx`, for file KBs, add a status badge under the subtitle:

```jsx
import { Database, FileText, Pause, Play, Pencil, Trash2, RefreshCw } from 'lucide-react';

export default function KbCard({ item, t, translate, onClick, onEdit, onTogglePause, onDelete, onReindex }) {
  const isFile = item.source_kind === 'file';
  const Icon = isFile ? FileText : Database;
  const paused = item.status === 'paused';
  const sub = isFile
    ? (t.kb.fileTypes[item.file_type] || item.file_type || t.kb.sourceKinds.file)
    : (t.kb.dbTypes[item.db_type] || item.db_type || t.kb.sourceCinds.database);
  const idxStatus = item.indexing_status;
  const idxLabel = {
    pending: 'Queued', indexing: 'Indexing…', ready: 'Indexed', failed: 'Index failed',
  }[idxStatus] || (isFile ? 'Not indexed' : '');

  return (
    <div onClick={onClick} className="group flex cursor-pointer flex-col rounded-xl border border-border bg-card p-5 transition-shadow hover:shadow-sm">
      <div className="mb-2 flex items-start gap-2">
        <Icon className="mt-0.5 h-4 w-4 text-primary" />
        <h3 className="flex-1 font-display text-base text-foreground group-hover:text-primary">{translate(item.name)}</h3>
        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${paused ? 'bg-secondary text-muted-foreground' : 'bg-primary/10 text-primary'}`}>{t.detail.kbStatuses[item.status] || item.status}</span>
      </div>
      <p className="mb-1 flex-1 text-xs text-muted-foreground">{item.description ? translate(item.description) : sub}</p>
      {isFile && idxLabel && (
        <div className="mb-2 flex items-center gap-2 text-[10px] text-muted-foreground">
          <span className={`rounded px-1.5 py-0.5 ${idxStatus === 'ready' ? 'bg-primary/10 text-primary' : idxStatus === 'failed' ? 'bg-destructive/10 text-destructive' : 'bg-secondary'}`}>{idxLabel}</span>
          {item.chunk_count ? <span>{item.chunk_count} chunks</span> : null}
        </div>
      )}
      <div className="mt-3 flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
        <span className="rounded bg-secondary/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">{sub}</span>
        <div className="ml-auto flex gap-2">
          {isFile && (
            <button onClick={() => onReindex?.(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary" title="Reindex">
              <RefreshCw className="h-3 w-3" /> {t.kb.reindex || 'Reindex'}
            </button>
          )}
          <button onClick={() => onTogglePause(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary">
            {paused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />} {paused ? t.kb.resume : t.kb.pause}
          </button>
          <button onClick={() => onEdit(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-foreground hover:bg-secondary">
            <Pencil className="h-3 w-3" /> {t.kb.edit}
          </button>
          <button onClick={() => onDelete(item)} className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:text-destructive">
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      </div>
    </div>
  );
}
```

**Step 4: Wire `onReindex` in the parent that renders `KbCard`**

Find the parent (likely `MySpace.jsx` or `FilesView.jsx`) and pass an `onReindex` handler that calls `POST /api/apps/{app_id}/knowledge_bases/{id}/reindex` then refreshes the list. (Inspect the parent at execution time — the KbCard call site is where the list of KBs is rendered.)

**Step 5: Smoke test in browser**

- Start the frontend dev server, navigate to My Space → KB tab.
- Create a new file KB, upload a `.txt` file. After save, the card should show "Indexing…" then "Indexed (N chunks)".
- Edit a file KB and upload a `.docx` — accept list should include it.

**Step 6: Commit**

```bash
git add frontend/src/components/kb/KbFileFields.jsx frontend/src/components/kb/KbCard.jsx frontend/src/components/kb/KbSetupDialog.jsx
git commit -m "feat(rag-ui): accept docx/md, show indexing status, reindex button"
```

---

## Task 13: End-to-end manual verification

**Goal:** Confirm the full loop works — upload → index → ask → answer.

**Step 1: Start backend**

```bash
cd /root/zhanlu/backend && source venv/bin/activate && uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

**Step 2: Start frontend**

```bash
cd /root/zhanlu/frontend && npm run dev
```

**Step 3: Upload a document**

- Open `http://localhost:5157/my-space` (or whatever the dev port is).
- KB tab → New → choose "File" → upload a PDF/DOCX/TXT with known content.
- Save. Watch the card flip from "Indexing…" → "Indexed (N chunks)".
- If it shows "Index failed", check backend logs — likely the model is downloading on first run (wait) or the file path didn't resolve.

**Step 4: Bind the KB to an agent**

- Edit an agent → Data Sources → check the new file KB.

**Step 5: Ask a question**

- Open a chat with that agent.
- Ask something answerable from the document.
- The agent should call `ask_data_agent`, which internally calls `answer_from_documents` / `search_documents`, and reply with a grounded answer.

**Step 6: If something breaks**

- Backend log should show the data_agent sub-loop and which tool it picked.
- If it picked a DB tool on a file source, the prompt tweak in Task 9 didn't take — re-check `DATA_AGENT_PROMPT`.
- If `answer_from_documents` returned "index not ready", check `kb.indexing_status` in the DB.

---

## Notes & risks

- **Memory:** `sentence-transformers` + torch is ~500 MB RAM once loaded. The embedder is lazy + singleton, so it only loads on first ingestion/retrieval. On the dev server's limited RAM, the first reindex will spike. If this causes OOM, the fallback is to switch the embedder provider to OpenAI (small code change in `embedder.py`).
- **Model download:** First run downloads `all-MiniLM-L6-v2` (~90 MB) to `~/.cache/huggingface`. Subsequent runs use the cache.
- **Chroma persistence:** `backend/data/chroma/` is gitignored (verify in `.gitignore`).
- **Test strategy:** All unit tests mock the embedder via `monkeypatch.setattr(embedder, "_load_model", ...)`. No test loads the real model. No test spins up the full FastAPI app. This keeps the test suite light on the dev server per the RAM constraint.
- **Concurrency:** `ingest_kb` runs in `asyncio.to_thread`. SQLite + Chroma both handle single-writer; if two reindexes race, the second will wait on the DB write. Acceptable for v1.

---

## Execution order summary

Tasks 1 → 11 are backend, sequential (each builds on the prior). Task 12 is frontend (can start after Task 11). Task 13 is verification. Commit after every task.
