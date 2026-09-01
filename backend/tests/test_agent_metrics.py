"""Tests for agent metrics."""
import os
import sys
import threading

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.agent_metrics import AgentMetrics, Counter, Histogram, metrics


def test_counter_increment():
    c = Counter()
    c.inc()
    c.inc(5)
    assert c.value == 6


def test_counter_thread_safe():
    c = Counter()
    def worker():
        for _ in range(1000):
            c.inc()
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.value == 10000


def test_histogram_observe():
    h = Histogram()
    h.observe(10)
    h.observe(20)
    h.observe(30)
    d = h.to_dict()
    assert d["count"] == 3
    assert d["sum"] == 60
    assert d["min"] == 10
    assert d["max"] == 30
    assert abs(d["avg"] - 20.0) < 0.01


def test_histogram_empty():
    h = Histogram()
    d = h.to_dict()
    assert d["count"] == 0
    assert d["avg"] == 0.0


def test_metrics_guardrail():
    m = AgentMetrics()
    m.record_guardrail_warning("repeated_exact_failure_warning")
    m.record_guardrail_warning("repeated_exact_failure_warning")
    m.record_guardrail_halt("same_tool_failure_halt")
    snap = m.get_snapshot()
    assert snap["guardrails"]["warnings"]["repeated_exact_failure_warning"] == 2
    assert snap["guardrails"]["halts"]["same_tool_failure_halt"] == 1


def test_metrics_budget():
    m = AgentMetrics()
    m.record_budget(5, 10)
    m.record_budget(10, 10)  # exhausted
    snap = m.get_snapshot()
    assert snap["iteration_budget"]["exhausted_count"] == 1
    assert snap["iteration_budget"]["consumed"]["count"] == 2


def test_metrics_persistence():
    m = AgentMetrics()
    m.record_persistence(2, 5000)
    m.record_persistence(3, 20000)
    snap = m.get_snapshot()
    assert snap["result_persistence"]["layer2_chars_saved"]["sum"] == 5000
    assert snap["result_persistence"]["layer3_chars_saved"]["sum"] == 20000


def test_metrics_error_classification():
    m = AgentMetrics()
    m.record_error("rate_limit")
    m.record_error("rate_limit")
    m.record_error("context_overflow")
    snap = m.get_snapshot()
    assert snap["error_classification"]["rate_limit"] == 2
    assert snap["error_classification"]["context_overflow"] == 1


def test_metrics_fallback():
    m = AgentMetrics()
    m.record_fallback()  # triggered
    m.record_fallback(succeeded=True)
    m.record_fallback(succeeded=False)
    snap = m.get_snapshot()
    assert snap["provider_fallback"]["triggered"] == 1
    assert snap["provider_fallback"]["succeeded"] == 1
    assert snap["provider_fallback"]["failed"] == 1


def test_metrics_sanitize():
    m = AgentMetrics()
    m.record_sanitize(surrogates=3, args_repaired=2, sequences_closed=1)
    snap = m.get_snapshot()
    assert snap["message_sanitization"]["surrogates_replaced"] == 3
    assert snap["message_sanitization"]["args_repaired"] == 2
    assert snap["message_sanitization"]["tool_sequences_closed"] == 1


def test_metrics_reset():
    m = AgentMetrics()
    m.record_guardrail_halt("test")
    assert m.get_snapshot()["guardrails"]["halts"]["test"] == 1
    m.reset()
    assert m.get_snapshot()["guardrails"]["halts"] == {}


def test_metrics_snapshot_has_uptime():
    m = AgentMetrics()
    snap = m.get_snapshot()
    assert "uptime_seconds" in snap
    assert snap["uptime_seconds"] >= 0


def test_global_metrics_singleton():
    assert metrics is not None
    assert isinstance(metrics, AgentMetrics)
