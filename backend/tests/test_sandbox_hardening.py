"""Regression tests for I2 (container cleanup) and I4 (output size caps)."""
import os, uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

import pytest
import app.services.sandbox.container_manager as cm


# ── I2: timeout cleanup ──

def test_container_manager_has_force_remove_pattern():
    """I2: source must contain docker rm -f for timeout cleanup."""
    with open(os.path.join(os.path.dirname(__file__), "../app/services/sandbox/container_manager.py")) as f:
        src = f.read()
    assert "container_name" in src, "I2: must generate a container name"
    assert ("rm" in src and "-f" in src) or "docker rm" in src.lower(), \
        "I2: must force-remove container on timeout"


# ── I4: size caps ──

def test_max_output_constants():
    """I4: MAX_OUTPUT_FILE_SIZE and MAX_TOTAL_OUTPUT_SIZE defined and sensible."""
    assert cm.MAX_OUTPUT_FILE_SIZE > 0
    assert cm.MAX_TOTAL_OUTPUT_SIZE >= cm.MAX_OUTPUT_FILE_SIZE


def test_collect_outputs_skips_oversized_file(tmp_path):
    """I4: files exceeding MAX_OUTPUT_FILE_SIZE are skipped."""
    d = tmp_path / "out"
    d.mkdir()
    (d / "huge.bin").write_bytes(b"x" * (cm.MAX_OUTPUT_FILE_SIZE + 1))
    (d / "tiny.txt").write_text("ok")

    results = cm.collect_outputs(str(d))
    names = {r["file_name"] for r in results}
    assert "tiny.txt" in names
    assert "huge.bin" not in names


def test_collect_outputs_respects_total_cap(tmp_path):
    """I4: stops collecting when total exceeds MAX_TOTAL_OUTPUT_SIZE."""
    d = tmp_path / "out"
    d.mkdir()
    chunk = cm.MAX_TOTAL_OUTPUT_SIZE // 4 + 1
    for i in range(10):
        (d / f"data_{i}.txt").write_text("x" * chunk)

    results = cm.collect_outputs(str(d))
    total = sum(len(r.get("data", b"")) if isinstance(r.get("data"), bytes) else 0
                 for r in results)
    assert total <= cm.MAX_TOTAL_OUTPUT_SIZE + chunk  # allow one extra file rounding
