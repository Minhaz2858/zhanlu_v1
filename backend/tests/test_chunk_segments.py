"""P4 T3 — location-aware chunking.

`chunk_segments(segments)` produces chunks shaped like `chunk_text` output
({"text", "index", "token_count"}) PLUS a "location" field: the location of
the segment that produced the chunk's FIRST part. Cross-segment chunks take
the first location. The existing `chunk_text` output shape is unchanged.
"""

from __future__ import annotations

from app.services.document_ingestion import chunker


def test_chunk_segments_propagates_location():
    segs = [
        {"text": "Customer definition: " + "acme corp " * 40, "location": {"page": 1}},
        {"text": "Refund policy: " + "money back " * 40, "location": {"page": 2}},
    ]
    chunks = chunker.chunk_segments(segs, max_tokens=60, overlap=0)
    assert chunks, "must produce chunks"
    for c in chunks:
        assert c["location"] in ({"page": 1}, {"page": 2}), c
        assert "text" in c and "index" in c and "token_count" in c
    # index is sequential
    assert [c["index"] for c in chunks] == list(range(len(chunks)))


def test_cross_segment_chunk_takes_first_location():
    segs = [
        {"text": "alpha " * 30, "location": {"sheet": "A"}},
        {"text": "beta " * 30, "location": {"sheet": "B"}},
    ]
    chunks = chunker.chunk_segments(segs, max_tokens=200, overlap=0)
    merged = [c for c in chunks if "alpha" in c["text"] and "beta" in c["text"]]
    if merged:  # if the chunker merged both segments into one chunk
        assert merged[0]["location"] == {"sheet": "A"}


def test_chunk_text_shape_unchanged():
    chunks = chunker.chunk_text("hello world " * 50, max_tokens=30, overlap=0)
    assert chunks
    assert all(set(c) >= {"text", "index", "token_count"} for c in chunks)
    assert all("location" not in c for c in chunks)


def test_empty_segments_yield_no_chunks():
    assert chunker.chunk_segments([], max_tokens=100) == []
    assert chunker.chunk_segments([{"text": "  ", "location": {}}],
                                  max_tokens=100) == []
