"""Impact-diff engine (roadmap P3) — deterministic before/after comparison
for engineering changes. Compares only metrics measured on BOTH sides.
"""
from __future__ import annotations


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _dfm_rules(dfm: dict | None) -> list[str]:
    if not isinstance(dfm, dict):
        return []
    return [i.get("rule") for i in (dfm.get("issues") or [])
            if isinstance(i, dict) and i.get("severity") == "error"
            and i.get("rule")]


def compute_impact(before: dict, after: dict,
                   before_dfm: dict | None = None,
                   after_dfm: dict | None = None) -> dict:
    """Diff two measurement snapshots (+ optional DFM results).

    Returns {changed: [{metric, before, after, delta_pct}], unchanged: [...],
    dfm: {verdict_before, verdict_after, errors_new, errors_resolved} | None,
    summary: [str], requires_attention: bool}.
    """
    b = before if isinstance(before, dict) else {}
    a = after if isinstance(after, dict) else {}
    changed: list[dict] = []
    unchanged: list[str] = []
    for metric in sorted(set(b) | set(a)):
        bv, av = _num(b.get(metric)), _num(a.get(metric))
        if bv is None or av is None:
            continue  # measured on one side only → not comparable
        if abs(av - bv) < 1e-9:
            unchanged.append(metric)
            continue
        delta_pct = (av - bv) / bv * 100.0 if bv != 0 else None
        changed.append({
            "metric": metric,
            "before": bv,
            "after": av,
            "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        })

    dfm_out = None
    requires_attention = False
    summary: list[str] = []
    if before_dfm is not None or after_dfm is not None:
        errors_b = set(_dfm_rules(before_dfm))
        errors_a = set(_dfm_rules(after_dfm))
        dfm_out = {
            "verdict_before": (before_dfm or {}).get("verdict"),
            "verdict_after": (after_dfm or {}).get("verdict"),
            "errors_new": sorted(errors_a - errors_b),
            "errors_resolved": sorted(errors_b - errors_a),
        }
        if dfm_out["verdict_before"] != dfm_out["verdict_after"]:
            if dfm_out["verdict_after"] in ("fail",):
                summary.append(
                    f"DFM verdict degraded: {dfm_out['verdict_before']} → "
                    f"{dfm_out['verdict_after']}")
                requires_attention = True
            elif dfm_out["verdict_after"] in ("pass", "pass_with_warnings"):
                summary.append(
                    f"DFM verdict improved: {dfm_out['verdict_before']} → "
                    f"{dfm_out['verdict_after']}")
        if dfm_out["errors_new"]:
            summary.append(f"New DFM errors introduced: {dfm_out['errors_new']}")
            requires_attention = True
        if dfm_out["errors_resolved"]:
            summary.append(f"DFM errors resolved: {dfm_out['errors_resolved']}")

    for c in changed:
        dp = f"{c['delta_pct']:+.1f}%" if c["delta_pct"] is not None else "n/a"
        summary.append(f"{c['metric']}: {c['before']} → {c['after']} ({dp})")

    return {
        "changed": changed,
        "unchanged": unchanged,
        "dfm": dfm_out,
        "summary": summary,
        "requires_attention": requires_attention,
    }
