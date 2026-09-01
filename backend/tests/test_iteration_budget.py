"""Tests for the per-conversation iteration budget."""
import os
import sys
import threading

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.iteration_budget import IterationBudget


def test_consume_until_exhausted():
    budget = IterationBudget(max_total=3)
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False  # exhausted
    assert budget.remaining == 0


def test_refund_restores_one():
    budget = IterationBudget(max_total=2)
    budget.consume()
    budget.consume()
    assert budget.remaining == 0
    budget.refund()
    assert budget.remaining == 1
    assert budget.consume() is True


def test_refund_below_zero_clamped():
    budget = IterationBudget(max_total=1)
    budget.refund()  # no-op, used is 0
    assert budget.used == 0


def test_thread_safe_concurrent_consume():
    budget = IterationBudget(max_total=1000)
    consumed = []
    def worker():
        while budget.consume():
            consumed.append(1)
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(consumed) == 1000  # exactly max_total, no over-consume


def test_used_and_remaining():
    budget = IterationBudget(max_total=10)
    budget.consume()
    budget.consume()
    assert budget.used == 2
    assert budget.remaining == 8


def test_refund_does_not_exceed_max():
    """Refunding after consume should never let used go negative."""
    budget = IterationBudget(max_total=5)
    budget.consume()
    budget.refund()
    budget.refund()  # should clamp at 0
    assert budget.used == 0
    assert budget.remaining == 5
