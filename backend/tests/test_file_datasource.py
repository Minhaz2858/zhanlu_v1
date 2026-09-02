"""Tests for file_datasource — spreadsheet (CSV/XLSX) → queryable SQLite.

Pins: parse csv/xlsx into a local sqlite file under upload_path/file_datasources,
sanitized table + column names, rows preserved; invalid input raises ValueError.
"""

import io
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from app.services import file_datasource as fd  # noqa: E402


def _csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def test_csv_import_creates_queryable_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    csv_data = "product,price, qty\nLaptop,1200,3\nMouse,25,100\n"

    res = fd.import_spreadsheet_to_sqlite(_csv_bytes(csv_data), "sales.csv")

    assert res["db_path"].exists()
    assert res["tables"] == ["sales"]
    assert res["total_rows"] == 2
    conn = sqlite3.connect(res["db_path"])
    rows = conn.execute('SELECT product, price, "qty" FROM sales ORDER BY product').fetchall()
    assert rows == [("Laptop", 1200, 3), ("Mouse", 25, 100)]
    conn.close()


def test_csv_header_spaces_and_dupes_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    csv_data = "Product Name,product name,Value\nA,B,1\n"
    res = fd.import_spreadsheet_to_sqlite(_csv_bytes(csv_data), "messy.csv")
    conn = sqlite3.connect(res["db_path"])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(messy)").fetchall()]
    conn.close()
    assert cols == ["product_name", "product_name_2", "value"]


def test_xlsx_multi_sheet_becomes_multiple_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"a": [1, 2]}).to_excel(writer, sheet_name="Sheet1", index=False)
        pd.DataFrame({"b": ["x"]}).to_excel(writer, sheet_name="Regions", index=False)
    buf.seek(0)

    res = fd.import_spreadsheet_to_sqlite(buf.read(), "book.xlsx")

    assert set(res["tables"]) == {"sheet1", "regions"}
    assert res["total_rows"] == 3
    conn = sqlite3.connect(res["db_path"])
    assert conn.execute("SELECT count(*) FROM sheet1").fetchone()[0] == 2
    assert conn.execute("SELECT b FROM regions").fetchone()[0] == "x"
    conn.close()


def test_unsupported_extension_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="csv|xlsx|xls"):
        fd.import_spreadsheet_to_sqlite(b"nope", "data.pdf")


def test_empty_csv_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(fd.settings, "UPLOAD_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="empty|columns"):
        fd.import_spreadsheet_to_sqlite(_csv_bytes(""), "blank.csv")
