"""Output verifier — every number in a brief must exist in the evidence pack."""
from __future__ import annotations

import re

REQUIRED_KEYS = ("market_update_zh", "price_data_zh", "upstream_logic_zh",
                 "supply_demand_zh", "forecast_zh", "watch_triggers_zh", "risk_zh")
_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
_SMALL_NUM_EXEMPT = 12.0


def _pack_numbers(pack: dict) -> set[float]:
    nums: set[float] = set()

    def _add(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        for cand in (v, v * 100.0):
            for d in (0, 1, 2):
                nums.add(round(float(cand), d))

    def _walk(x):
        if isinstance(x, dict):
            for v in x.values():
                _walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                _walk(v)
        else:
            _add(x)

    _walk(pack)
    return nums


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.51, abs(b) * 0.005)


def verify_brief(brief: dict, pack: dict) -> list[str]:
    violations: list[str] = []
    if not isinstance(brief, dict):
        return ["brief is not a dict"]
    for key in REQUIRED_KEYS:
        if key not in brief:
            violations.append(f"missing required key: {key}")
    if violations:
        return violations
    if not isinstance(brief.get("watch_triggers_zh"), list):
        return ["watch_triggers_zh must be a list"]

    known = _pack_numbers(pack)
    texts = [str(brief.get(k) or "") for k in
             ("market_update_zh", "price_data_zh", "upstream_logic_zh",
              "supply_demand_zh", "forecast_zh", "risk_zh")]
    texts += [str(t) for t in brief.get("watch_triggers_zh") or []]
    for m in _NUM_RE.finditer(" ".join(texts)):
        raw = m.group(0).replace(",", "").lstrip("+")
        try:
            n = float(raw)
        except ValueError:
            continue
        if abs(n) <= _SMALL_NUM_EXEMPT:
            continue
        # Sign-insensitive: Chinese prose often drops the sign ("下跌17.97%").
        # The verifier guards against invented magnitudes; direction is set
        # by the pack's explicit fields and the prompt contract.
        if not any(_close(n, k) or _close(n, -k) for k in known):
            violations.append(f"number not in evidence pack: {m.group(0)}")
    return violations
