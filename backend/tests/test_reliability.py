"""Tests for the tool-call reliability layer."""

import asyncio
import pytest

from app.services.reliability import (
    LoopState,
    OutputCheck,
    ReliabilityConfig,
    default_output_checks,
    reformulate_args,
    retry_with_backoff,
    run_tool_with_reliability,
    verify_output,
)


def test_retry_with_backoff_eventually_succeeds():
    cfg = ReliabilityConfig(max_retries=3, backoff_base_seconds=0.001, backoff_jitter=0)

    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return {"ok": True, "n": attempts["n"]}

    async def main():
        return await retry_with_backoff(flaky, cfg=cfg, tool_name="flaky")

    result, n = asyncio.run(main())
    assert n == 2
    assert result["ok"] is True


def test_retry_with_backoff_gives_up_after_max():
    cfg = ReliabilityConfig(max_retries=2, backoff_base_seconds=0.001, backoff_jitter=0)

    async def always_fails():
        raise RuntimeError("nope")

    async def main():
        return await retry_with_backoff(always_fails, cfg=cfg, tool_name="x")

    with pytest.raises(RuntimeError):
        asyncio.run(main())


def test_retry_with_backoff_honors_non_retryable():
    cfg = ReliabilityConfig(max_retries=5, backoff_base_seconds=0.001, backoff_jitter=0)

    calls = {"n": 0}

    async def bad_input():
        calls["n"] += 1
        raise ValueError("permanent")

    async def main():
        return await retry_with_backoff(
            bad_input,
            cfg=cfg,
            tool_name="x",
            is_retryable=lambda exc: not isinstance(exc, ValueError),
        )

    with pytest.raises(ValueError):
        asyncio.run(main())
    assert calls["n"] == 1


def test_loop_state_detects_loop_after_threshold():
    cfg = ReliabilityConfig(loop_guard_threshold=3, loop_guard_window=10)
    state = LoopState()
    for _ in range(3):
        state.record("query", {"q": "x"}, success=True)
    is_loop, reason = state.is_loop(cfg)
    assert is_loop is True
    assert "query" in reason


def test_loop_state_does_not_treat_failures_as_loop():
    """5 failures of the same call are a bug, not a loop."""
    cfg = ReliabilityConfig(loop_guard_threshold=3, loop_guard_window=10)
    state = LoopState()
    for _ in range(5):
        state.record("query", {"q": "x"}, success=False)
    is_loop, _ = state.is_loop(cfg)
    assert is_loop is False


def test_verify_output_runs_all_checks():
    checks = [
        OutputCheck("is_dict", lambda r: isinstance(r, dict), "not a dict"),
        OutputCheck("has_id", lambda r: "id" in r, "no id"),
    ]
    assert verify_output({"id": 1}, checks)[0] is True
    ok, err = verify_output([1, 2], checks)
    assert ok is False
    assert "not a dict" in err


def test_default_output_checks_for_artifact_tool():
    checks = default_output_checks("create_artifact")
    assert len(checks) == 2
    # Missing id → fail
    ok, err = verify_output({"foo": "bar"}, checks)
    assert ok is False
    # With id → pass
    ok, _ = verify_output({"artifact_id": "abc"}, checks)
    assert ok is True


def test_run_tool_with_reliability_full_pipeline():
    """Smoke test: full pipeline (retry + verify + loop guard) works."""
    cfg = ReliabilityConfig(
        max_retries=3,
        backoff_base_seconds=0.001,
        backoff_jitter=0,
        loop_guard_threshold=3,
    )
    state = LoopState()

    async def call(args):
        return {"artifact_id": "real", "args_echo": args}

    async def main():
        return await run_tool_with_reliability(
            "create_artifact",
            {"title": "x", "content": "y"},
            call_fn=call,
            loop_state=state,
            cfg=cfg,
            output_checks=default_output_checks("create_artifact"),
        )

    out = asyncio.run(main())
    assert out["success"] is True
    assert out["result"]["artifact_id"] == "real"
    assert out["attempts"] == 1
    assert out["reformulated"] is False


def test_run_tool_with_reliability_reformulates_on_failure():
    cfg = ReliabilityConfig(
        max_retries=1,
        backoff_base_seconds=0.001,
        backoff_jitter=0,
        max_reformulations=1,
    )
    state = LoopState()

    received = {"args": None}

    async def call(args):
        received["args"] = args
        # Fail on the first call, succeed on the second.
        if "fixed" not in args:
            raise RuntimeError("bad shape")
        return {"artifact_id": "x"}

    async def repair(tool_name, args, err):
        return {**args, "fixed": True}

    async def main():
        return await run_tool_with_reliability(
            "create_artifact",
            {"title": "x"},
            call_fn=call,
            loop_state=state,
            cfg=cfg,
            output_checks=default_output_checks("create_artifact"),
            llm_repair=repair,
        )

    out = asyncio.run(main())
    assert out["success"] is True
    assert out["reformulated"] is True
    assert received["args"]["fixed"] is True


def test_run_tool_with_reliability_returns_structured_error():
    cfg = ReliabilityConfig(
        max_retries=2,
        backoff_base_seconds=0.001,
        backoff_jitter=0,
    )
    state = LoopState()

    async def call(args):
        raise RuntimeError("nope")

    async def main():
        return await run_tool_with_reliability(
            "create_artifact",
            {},
            call_fn=call,
            loop_state=state,
            cfg=cfg,
        )

    out = asyncio.run(main())
    assert out["success"] is False
    assert "nope" in out["error"]
    assert out["attempts"] >= 1
