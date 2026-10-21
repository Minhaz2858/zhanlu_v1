"""cad_bom_generate — BOM + manufacturing notes (roadmap P4).

Normalizes component rows collected from the build, aggregates duplicates,
emits an HTML BOM artifact with manufacturing notes. Handoff to procurement
is the documented skill flow: user approval → create_quote_draft (approval-
gated ontology action) using the rfq_quote totals.
"""
from __future__ import annotations

import html as _html

from app.services.tool_handlers.fusion360_io import _save_file


def _bad(error: str) -> dict:
    return {"success": False, "error": error}


def _esc(v) -> str:
    return _html.escape(str(v))


def _render_html(part_name: str, bom: dict, notes: list[str]) -> str:
    rows = "".join(
        f"<tr><td>{r['line_no']}</td><td>{_esc(r['name'])}</td>"
        f"<td>{_esc(r['material'] or '—')}</td><td>{r['quantity']}</td>"
        f"<td>{_esc(r['mass_g'] if r['mass_g'] is not None else '—')}</td>"
        f"<td>{_esc(r['process'])}</td></tr>"
        for r in bom["rows"]
    )
    warns = "".join(f"<li>{_esc(w)}</li>" for w in bom["warnings"]) or "<li>None</li>"
    nl = "".join(f"<li>{_esc(n)}</li>" for n in notes)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BOM — {_esc(part_name)}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ddd;padding:.45rem .6rem;text-align:left}}</style>
</head><body>
<h1>Bill of Materials — {_esc(part_name)}</h1>
<p class="muted">{bom['totals']['line_count']} line(s) · {bom['totals']['total_quantity']} part(s) ·
{bom['totals']['total_mass_g']} g total measured mass</p>
<table><tr><th>#</th><th>Part</th><th>Material</th><th>Qty</th><th>Mass (g)</th><th>Process</th></tr>{rows}</table>
<h3>Data warnings</h3><ul>{warns}</ul>
<h3>Manufacturing notes</h3><ul>{nl}</ul>
</body></html>"""


def _cad_bom_generate(args: dict, db, user_id: str | None) -> dict:
    """Generate a normalized BOM + manufacturing notes artifact."""
    part_name = str(args.get("part_name") or "").strip() or "Unnamed part"
    process = str(args.get("process") or "").strip().lower()
    components = args.get("components")
    if not isinstance(components, list) or not components:
        return _bad("components (non-empty list of {name, quantity, material, "
                    "mass_g}) is required — collect them from the build "
                    "(fusion360_component / physical properties)")

    from app.services.dfm.bom import mfg_notes, normalize_bom_rows
    try:
        bom = normalize_bom_rows(components, process or "cnc")
    except ValueError as e:
        return _bad(str(e))

    notes = mfg_notes(process or bom["rows"][0]["process"],
                      args.get("feature_counts"),
                      args.get("dfm_result") if isinstance(args.get("dfm_result"), dict) else None)

    name = f"bom_{part_name.replace(' ', '_')}.html"
    data = _render_html(part_name, bom, notes).encode("utf-8")
    url, size = _save_file(db, user_id, name, "html", data)

    return {
        "success": True,
        "bom": bom,
        "mfg_notes": notes,
        "file_url": url,
        "filename": name,
        "size_bytes": size,
        "message": (
            f"BOM generated: {bom['totals']['line_count']} line(s), "
            f"{bom['totals']['total_quantity']} part(s). Saved as {name}."
            + (" Warnings need attention before procurement." if bom["warnings"] else "")
        ),
    }


from app.services.tool_registry import registry  # noqa: E402

CAD_BOM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cad_bom_generate",
        "description": (
            "Generate a normalized Bill of Materials + manufacturing notes "
            "from the build's components. Pass ONLY components that exist in "
            "the model (name, quantity, material, measured mass_g). Duplicates "
            "aggregate; missing material/mass produce warnings — fix them by "
            "measuring, not guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part_name": {"type": "string"},
                "process": {"type": "string", "enum": ["cnc", "sheet_metal"]},
                "components": {
                    "type": "array",
                    "description": "[{name, quantity, material, mass_g, process?}] — from fusion360_component + physical properties.",
                    "items": {"type": "object"},
                },
                "feature_counts": {"type": "object", "description": "Same feature-count dict as rfq_quote."},
                "dfm_result": {"type": "object", "description": "fusion360_dfm_check result — outstanding repairs become manufacturing notes."},
            },
            "required": ["part_name", "process", "components"],
        },
    },
}

registry.register(
    name="cad_bom_generate",
    schema=CAD_BOM_SCHEMA,
    handler=_cad_bom_generate,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="Normalized BOM + manufacturing notes → HTML artifact.",
    emoji="📋",
)
