"""Tests for _persist_join_edges ranking guard (explicit user requirement).

Guarantees: FK is never downgraded; an existing edge is overwritten only on
strictly higher confidence; lower-confidence skips log a warning.
"""

import logging
from unittest.mock import MagicMock, patch

from app.services.knowledge_graph.catalog_indexer import _persist_join_edges
from app.services.knowledge_graph.join_edge_detector import detect_join_edges


def _edge(src="a", tgt="b", kind="VALUE_OVERLAP", confidence=0.8):
    return {
        "source_table": src,
        "target_table": tgt,
        "source_columns": ["a_col"],
        "target_columns": ["b_col"],
        "kind": kind,
        "confidence": confidence,
        "evidence": {"overlap_count": 3},
    }


def _make_db(existing_relation):
    db = MagicMock()
    query = db.query.return_value
    filtered = query.filter.return_value
    filtered.first.return_value = existing_relation
    return db


def _tables():
    return [
        {"table_name": "a", "_meta_id": "id_a"},
        {"table_name": "b", "_meta_id": "id_b"},
    ]


def test_fk_is_never_downgraded(caplog):
    existing = MagicMock()
    existing.relation_type = "FK"
    existing.confidence = 1.0
    db = _make_db(existing)

    with patch(
        "app.services.knowledge_graph.catalog_indexer.detect_join_edges",
        return_value=[_edge()],
    ):
        with caplog.at_level(logging.WARNING):
            _persist_join_edges(db, "kb1", _tables())

    db.add.assert_not_called()
    # FK edge attributes untouched.
    assert existing.relation_type == "FK"
    assert existing.confidence == 1.0
    assert any("FK pair" in r.message for r in caplog.records)


def test_lower_confidence_edge_is_skipped(caplog):
    existing = MagicMock()
    existing.relation_type = "VALUE_OVERLAP"
    existing.confidence = 0.95
    db = _make_db(existing)

    with patch(
        "app.services.knowledge_graph.catalog_indexer.detect_join_edges",
        return_value=[_edge(confidence=0.8)],
    ):
        with caplog.at_level(logging.WARNING):
            _persist_join_edges(db, "kb1", _tables())

    db.add.assert_not_called()
    assert existing.confidence == 0.95
    assert any("lower-confidence" in r.message for r in caplog.records)


def test_strictly_higher_confidence_overwrites():
    existing = MagicMock()
    existing.relation_type = "VALUE_OVERLAP"
    existing.confidence = 0.5
    db = _make_db(existing)

    with patch(
        "app.services.knowledge_graph.catalog_indexer.detect_join_edges",
        return_value=[_edge(confidence=0.8)],
    ):
        _persist_join_edges(db, "kb1", _tables())

    db.add.assert_not_called()
    assert existing.relation_type == "VALUE_OVERLAP"
    assert existing.confidence == 0.8
    assert existing.source_columns == ["a_col"]


def test_no_existing_edge_inserts_row():
    db = _make_db(None)

    with patch(
        "app.services.knowledge_graph.catalog_indexer.detect_join_edges",
        return_value=[_edge()],
    ):
        _persist_join_edges(db, "kb1", _tables())

    db.add.assert_called_once()
    db.commit.assert_called_once()


def test_equal_confidence_is_not_overwritten():
    existing = MagicMock()
    existing.relation_type = "NAME_MATCH"
    existing.confidence = 0.5
    db = _make_db(existing)

    with patch(
        "app.services.knowledge_graph.catalog_indexer.detect_join_edges",
        return_value=[_edge(kind="VALUE_OVERLAP", confidence=0.5)],
    ):
        _persist_join_edges(db, "kb1", _tables())

    db.add.assert_not_called()
    # existing NAME_MATCH kept, not replaced by equal-confidence VALUE_OVERLAP.
    assert existing.relation_type == "NAME_MATCH"


def test_integration_detector_feeds_persist():
    """Sanity: detector output has the shape _persist_join_edges consumes."""
    sales = {"table_name": "s", "_meta_id": "id_s", "columns": [
        {"column_name": "mat_id", "data_type": "int", "is_primary_key": False,
         "value_samples": ["1", "2", "3"]}],
        "foreign_keys": []}
    prod = {"table_name": "p", "_meta_id": "id_p", "columns": [
        {"column_name": "mat_id", "data_type": "int", "is_primary_key": False,
         "value_samples": ["1", "2", "3", "4"]}],
        "foreign_keys": []}
    edges = detect_join_edges([sales, prod])
    assert len(edges) == 1
    e = edges[0]
    for k in ("source_table", "target_table", "source_columns",
              "target_columns", "kind", "confidence", "evidence"):
        assert k in e
