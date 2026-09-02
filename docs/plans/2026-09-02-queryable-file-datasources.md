# Queryable File Datasources (CSV/XLSX in Database Query) — Implementation Plan

**Goal:** let users upload a spreadsheet (CSV/XLSX) and query it in the workflow
Database Query node exactly like a connected database (SQL + natural language).

**Why it's small:** the runtime already resolves sqlite connectors from a local
path (`SQLiteConnector.__enter__` opens `kb.api_url or kb.file_url`; the nl2sql
adapter path uses `file_url or database_name`), and the db_query picker
(`listDatabases` loader in NodeParameterForm) shows EVERY KnowledgeBase row
with a truthy `db_type`. We only need the **creation path**: bytes → sqlite
file → KB row with `db_type='sqlite'`.

## Design

- New `backend/app/services/file_datasource.py`:
  - `import_spreadsheet_to_sqlite(file_bytes, filename) -> Path`
    - `.csv` → pandas `read_csv`; `.xlsx/.xls` → `read_excel(sheet_name=None)`
    - one table per sheet: sanitized table name (`Sheet1` → `sheet1`; csv → file stem)
    - sanitized column headers (strip, spaces/non-alnum → `_`, dedupe)
    - `df.to_sql(table, sqlite_engine, if_exists='replace', index=False)`
    - file stored at `settings.UPLOAD_DIR/file_datasources/<uuid>.db` (persistent: ./backend/uploads, bind-mounted to /app)
  - `row_count` helper for item_count.
- Router `POST /knowledge_bases/upload-query-file` (multipart: file, name,
  description, project) in `backend/app/routers/knowledge_bases.py`:
  - ext allowlist csv/xlsx/xls; size cap (50MB)
  - creates KnowledgeBase: `source_kind='database'`, `type='sqlite'`,
    `db_type='sqlite'`, `file_url=<abs path>`, `file_type=<ext>`, item_count.
  - serializes the created row like the entities router does (frontend needs
    `id`, `app_id`, `name`, `source_kind`, `db_type`, `file_url`).
- Frontend `KbSetupDialog.jsx`:
  - third source-kind card `file_db` → "Spreadsheet (CSV/Excel) — query it like
    a database" (translations en/zh).
  - inline file input (accept .csv,.xlsx,.xls) — NOT KbFileFields (that posts
    to Core/UploadFile; we need raw bytes to the new endpoint).
  - submit → FormData POST → `onSaved(kb)`; edit mode re-uploads only when a
    file is chosen (else plain entity update for name/desc/project).
- db_query SQL mode → direct; NL mode → sqlite connector list_tables/describe
  (already governed; no catalog — catalog is mysql/postgres only, unaffected).

## Files
- new: backend/app/services/file_datasource.py
- new: backend/tests/test_file_datasource.py
- edit: backend/app/routers/knowledge_bases.py (+route test in existing router test file or new tests/routers/test_kb_upload_query_file.py)
- edit: frontend/src/components/kb/KbSetupDialog.jsx
- edit: frontend/src/lib/translations.js (kb.sourceKinds.file_db + desc)
- edit: frontend/src/api/... (optional tiny helper) — keep inline authFetch

## Verify
1. pytest: file_datasource parse tests + router upload test.
2. API: curl a small CSV → KB row; then run a real db_query (SQL + NL) against
   it in the backend container → correct rows.
3. Frontend build + browser: upload a CSV in the dialog → appears in My Space
   KB tab → shows in db_query Database dropdown.
