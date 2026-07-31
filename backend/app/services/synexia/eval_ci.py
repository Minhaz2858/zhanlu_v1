"""CI runner for the Synexia eval harness.

Runs the golden scenarios against a LIVE backend via the sync FSM route
(``POST {base_url}/executions`` — the response already carries
``assistant_content`` / ``artifact_ids`` / ``confidence`` /
``quality_gate``, which is exactly the shape the graders consume).

Usage::

    python -m app.services.synexia.eval_ci \
        --base-url http://localhost:5002/api \
        [--scenarios path/to/extra_scenarios.json] \
        [--report path/to/report.json] \
        [--timeout 120]

Exit code is 0 when every scenario passes, 1 otherwise — suitable for
CI gates. Stdlib only (urllib) so the runner adds no dependencies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import urllib.request
from typing import Optional

from app.services.synexia.eval_harness import (
    HarnessReport,
    load_all_scenarios,
    EvalRunner,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:5002/api"


def make_http_run_fn(base_url: str, *, timeout: float = 120.0):
    """Build an async run_fn that POSTs each scenario to /executions.

    The scenario's ``user_message`` (and optional ``agent_name`` /
    ``conversation_id``) are forwarded; the JSON response is returned
    verbatim as the grader output dict.
    """
    endpoint = base_url.rstrip("/") + "/executions"

    async def _run(scenario: dict) -> dict:
        payload = {
            "user_message": scenario.get("user_message", ""),
            "agent_name": scenario.get("agent_name", "general_assistant"),
        }
        if scenario.get("conversation_id"):
            payload["conversation_id"] = scenario["conversation_id"]

        def _post() -> dict:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        return await asyncio.to_thread(_post)

    return _run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval_ci",
        description="Run golden eval scenarios against a live backend.",
    )
    p.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"API base URL (default: {DEFAULT_BASE_URL})",
    )
    p.add_argument(
        "--scenarios",
        default=None,
        help="Optional JSON file with extra/override scenarios.",
    )
    p.add_argument(
        "--report",
        default=None,
        help="Optional path to write the JSON report.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout in seconds (default: 120).",
    )
    return p


async def run_ci(
    *,
    base_url: str = DEFAULT_BASE_URL,
    scenarios_file: Optional[str] = None,
    timeout: float = 120.0,
) -> HarnessReport:
    """Load scenarios and run them against ``base_url``. Returns the report."""
    scenarios = load_all_scenarios(user_file=scenarios_file)
    runner = EvalRunner(
        run_fn=make_http_run_fn(base_url, timeout=timeout),
        scenarios=scenarios,
    )
    return await runner.run()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report = asyncio.run(
        run_ci(
            base_url=args.base_url,
            scenarios_file=args.scenarios,
            timeout=args.timeout,
        )
    )
    data = report.to_dict()

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # Human-readable summary on stdout.
    for sc in data["scenarios"]:
        mark = "PASS" if sc["passed"] else "FAIL"
        line = f"[{mark}] {sc['name']} ({sc['duration_ms']:.0f}ms)"
        if sc["error"]:
            line += f" — error: {sc['error']}"
        print(line)
        for g in sc["graders"]:
            if not g["passed"]:
                print(f"       ✗ {g['name']}: {g['detail']}")
    print(
        f"\n{report.passed}/{report.total} scenarios passed "
        f"({report.duration_ms:.0f}ms total)"
    )
    return 0 if report.is_ok else 1


if __name__ == "__main__":
    sys.exit(main())
