"""rfq_quote — the commercial wedge (roadmap P2).

One deterministic tool: validates the requirement, computes the preliminary
quote (dfm.quote.compute_quote — mass-gated, never guesses), and emits an
HTML quote_package artifact with status draft_pending_approval. The human
approval gate is explicit: the package says DRAFT until a person approves;
the agent must surface that in its answer.
"""
from __future__ import annotations

import html as _html


def _bad(error: str) -> dict:
    return {"success": False, "error": error}


def _esc(v) -> str:
    return _html.escape(str(v))


def _render_html(q: dict, dfm: dict | None) -> str:
    rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>${_esc(v)}</td></tr>"
        for k, v in (
            ("Material (per unit)", q["breakdown"]["material_per_unit"]),
            ("Machining (per unit)", q["breakdown"]["machining_per_unit"]),
            ("Setup (per unit)", q["breakdown"]["setup_per_unit"]),
        )
    )
    feat = ""
    fb = q["breakdown"].get("feature_breakdown") or {}
    if fb:
        rows_feat = "".join(
            f"<li>{_esc(k)} × {v['count']} = {_esc(v['minutes'])} min</li>"
            for k, v in fb.items()
        )
        feat = f"<h3>Feature time ({q['breakdown']['machining_minutes_per_unit']} min/unit)</h3><ul>{rows_feat}</ul>"
    dfm_html = ""
    if dfm:
        issues = "".join(
            f"<li><b>{_esc(i['rule'])}</b> ({_esc(i['severity'])}): "
            f"measured {_esc(i['measured'])}, required {_esc(i['required'])} "
            f"— {_esc(i['reason'])}"
            + (f" <i>Repair: {_esc(i['repair'])}</i>" if i.get("repair") else "")
            + "</li>"
            for i in (dfm.get("issues") or [])
        ) or "<li>No issues</li>"
        dfm_html = (
            f"<h2>DFM review — {_esc(dfm.get('verdict', 'n/a'))}</h2>"
            f"<ul>{issues}</ul>"
        )
    assumptions = "".join(f"<li>{_esc(a)}</li>" for a in q["assumptions"])
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Quote — {_esc(q['part_name'])}</title>
<style>
body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#111}}
.banner{{background:#F59E0B;color:#111;padding:.6rem 1rem;border-radius:8px;font-weight:600}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0}}
td,th{{border:1px solid #ddd;padding:.45rem .6rem;text-align:left}}
.range{{font-size:1.4rem;font-weight:700}}
.muted{{color:#666;font-size:.9rem}}
</style></head><body>
<div class="banner">DRAFT — draft_pending_approval · not a binding quote</div>
<h1>Quote package — {_esc(q['part_name'])}</h1>
<p class="muted">{_esc(q['process'].upper())} · {_esc(q['material_display'])} ·
qty {_esc(q['quantity'])} · mass {_esc(q['mass_g'])} g</p>
<h2>Preliminary price ({_esc(q['currency'])})</h2>
<p class="range">{_esc(q['total_low'])} – {_esc(q['total_high'])}
<span class="muted">(mid {_esc(q['total_mid'])})</span></p>
<p class="muted">Unit cost ≈ {_esc(q['unit_cost'])}</p>
<table><tr><th>Cost component</th><th>USD per unit</th></tr>{rows}</table>
{feat}
{dfm_html}
<h2>Assumptions &amp; exclusions</h2>
<ul>{assumptions}</ul>
</body></html>"""


def _rfq_quote(args: dict, db, user_id: str | None) -> dict:
    """Compute a preliminary quote and emit the quote-package HTML artifact."""
    process = str(args.get("process") or "").strip().lower()
    if not process:
        return _bad("process is required (cnc | sheet_metal)")
    if args.get("quantity") is None:
        return _bad("quantity is required (number of parts)")

    from app.services.dfm.quote import compute_quote
    try:
        q = compute_quote({
            "part_name": args.get("part_name"),
            "material": args.get("material"),
            "process": process,
            "quantity": args.get("quantity"),
            "mass_g": args.get("mass_g"),
            "volume_cm3": args.get("volume_cm3"),
            "feature_counts": args.get("feature_counts"),
        })
    except ValueError as e:
        msg = str(e)
        if "mass is required" in msg:
            msg = (f"{msg} — run fusion360_physical_properties on the finished "
                   "body, then call rfq_quote again with mass_g")
        return _bad(msg)

    dfm = args.get("dfm_result")
    if dfm is not None and not isinstance(dfm, dict):
        dfm = None

    part_slug = (q["part_name"] or "part").replace(" ", "_")
    name = f"quote_{part_slug}_qty{q['quantity']}.html"
    html_bytes = _render_html(q, dfm).encode("utf-8")
    url, size = _save_file(db, user_id, name, "html", html_bytes)

    return {
        "success": True,
        "quote": q,
        "file_url": url,
        "filename": name,
        "size_bytes": size,
        "message": (
            f"Preliminary quote for {q['quantity']}× {q['part_name']}: "
            f"{q['currency']} {q['total_low']}–{q['total_high']} "
            f"(mid {q['total_mid']}, ≈{q['unit_cost']}/unit). "
            f"Package saved as {name} with status draft_pending_approval — "
            "present it to the user and request explicit approval before "
            "treating it as released."
        ),
    }


from app.services.tool_registry import registry  # noqa: E402
from app.services.tool_handlers.fusion360_io import _save_file  # noqa: E402

RFQ_QUOTE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rfq_quote",
        "description": (
            "Compute a PRELIMINARY quote for a custom part and generate the "
            "quote-package HTML artifact (status draft_pending_approval). "
            "Requires mass_g (measure it with fusion360_physical_properties — "
            "this tool never guesses mass) or volume_cm3. Optionally pass the "
            "fusion360_dfm_check result to embed the DFM review. Present the "
            "package to the user and get explicit approval before release."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part_name": {"type": "string", "description": "Part name for the package."},
                "process": {"type": "string", "enum": ["cnc", "sheet_metal"]},
                "material": {
                    "type": "string",
                    "description": "6061-t6, 5052-h32, mild-steel, 304-stainless (unknown → generic).",
                },
                "quantity": {"type": "integer", "description": "Number of parts (>= 1)."},
                "mass_g": {"type": "number", "description": "Measured mass in grams (preferred over volume_cm3)."},
                "volume_cm3": {"type": "number", "description": "Volume in cm³ — mass is estimated via material density."},
                "feature_counts": {
                    "type": "object",
                    "description": "Feature counts driving machining time. CNC: holes, pockets, profiles. Sheet metal: bends, cutouts, forms.",
                },
                "dfm_result": {
                    "type": "object",
                    "description": "The fusion360_dfm_check result to embed in the package (recommended).",
                },
            },
            "required": ["process", "quantity"],
        },
    },
}

registry.register(
    name="rfq_quote",
    schema=RFQ_QUOTE_SCHEMA,
    handler=_rfq_quote,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description=(
        "Preliminary quote + HTML quote_package artifact (draft_pending_approval); "
        "mass-gated, deterministic cost model."
    ),
    emoji="🧾",
)
