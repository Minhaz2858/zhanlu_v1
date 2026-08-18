"""Phase 3 — Theme library audit.

Guarantees that every vendored theme:
  * is discoverable via ``list_themes`` (the gallery / restyle picker),
  * loads cleanly via ``load_theme``,
  * renders the canonical chatpath deck to a valid .pptx,
  * passes the 13-check deck audit (no FAIL-level findings).

The audit is run on the rendered .pptx directly (no LibreOffice needed), so
this is a fast, CI-friendly gate.  The sample deck is built inline so the test
does not depend on the (container-copied) gallery script.
"""

from __future__ import annotations

from pathlib import Path

from app.services.artifacts.audits.audit_deck import audit
from app.services.artifacts.exporters._common import ExportContext
from app.services.artifacts.exporters._theme import (
    list_themes,
    validate_theme_name,
)
from app.services.artifacts.render_dispatcher import render_pptx_from_plan_sync
from app.services.synexia.contracts import (
    ChartSpecInSlide,
    DeckPlan,
    KPISpecInSlide,
    SlidePlan,
)


def _sample_rows() -> list[dict]:
    return [
        {"material": "LLDPE", "price": 9120, "delta_pct": -4.2, "volume": 1820},
        {"material": "HDPE", "price": 8760, "delta_pct": 1.8, "volume": 1450},
        {"material": "PP", "price": 8050, "delta_pct": -1.1, "volume": 2110},
        {"material": "PVC", "price": 6320, "delta_pct": 3.4, "volume": 980},
        {"material": "ABS", "price": 12450, "delta_pct": 0.6, "volume": 760},
        {"material": "PET", "price": 7180, "delta_pct": -2.7, "volume": 1330},
        {"material": "PS", "price": 9450, "delta_pct": 2.2, "volume": 540},
        {"material": "PC", "price": 15800, "delta_pct": -0.9, "volume": 410},
    ]


def _chatpath_plan() -> DeckPlan:
    rows = _sample_rows()
    return DeckPlan(
        title="Polymer Price Chatpath — Weekly Review",
        theme_recommendation="zhanlu-blue",
        headline_style="assertion",
        summary="Resin prices softened across most grades this week.",
        slides=[
            SlidePlan(layout="cover", title="Polymer Price Chatpath",
                      subtitle="Weekly resin spot review", bullets=[]),
            SlidePlan(layout="kpi_grid", title="This Week at a Glance", bullets=[],
                      kpi_specs=[
                          KPISpecInSlide(label="Avg price", value="9,390", caption="8 grades"),
                          KPISpecInSlide(label="Top mover", value="-4.2%", caption="LLDPE w/w"),
                          KPISpecInSlide(label="Total volume", value="10,400 t", caption="tracked"),
                          KPISpecInSlide(label="Grades up", value="3 / 8", caption="vs 5 down"),
                      ]),
            SlidePlan(layout="chart_full", title="Spot Price by Grade",
                      bullets=["LLDPE and PET led the declines",
                               "ABS held firm at the top of the range"],
                      chart_spec=ChartSpecInSlide(chart_type="bar",
                                                  title="Spot price (¥/t) by grade",
                                                  x_key="material", y_keys=["price"]),
                      chart_rows=rows),
            SlidePlan(layout="data_table", title="Full Price Table", bullets=[],
                      table_cols=["Grade", "Price", "Δ%", "Volume"],
                      table_rows=[{"Grade": r["material"], "Price": r["price"],
                                   "Δ%": f"{r['delta_pct']:+.1f}%", "Volume": r["volume"]}
                                  for r in rows]),
            SlidePlan(layout="closing", title="Takeaways", bullets=[
                "Broad softening — buyers retain pricing leverage",
                "Watch LLDPE supply into next cycle",
                "Volume steady; no demand shock signaled",
            ]),
        ],
    )


def _render_to_tmp(theme_name: str, tmp_path: Path) -> Path:
    plan = _chatpath_plan()
    rows = _sample_rows()
    ctx = ExportContext(
        source="test",
        theme=theme_name,
        mode="dark" if "dark" in theme_name else "light",
        deck_plan=plan,
    )
    blob, _ = render_pptx_from_plan_sync(plan, rows, ctx, user_message="test")
    out = tmp_path / f"{theme_name}.pptx"
    out.write_bytes(blob)
    return out


def test_list_themes_returns_all_eleven():
    names = [t["name"] for t in list_themes()]
    assert len(names) == 11, f"expected 11 themes, got {len(names)}: {names}"
    assert "zhanlu-blue" in names


def test_every_theme_loads_and_audits_clean(tmp_path):
    failures = {}
    for theme in list_themes():
        name = theme["name"]
        try:
            pptx = _render_to_tmp(name, tmp_path)
        except Exception as exc:
            failures[name] = f"render error: {exc}"
            continue
        report = audit(str(pptx))
        if report["status"] == "FAIL":
            s = report.get("summary", {})
            failures[name] = (
                f"audit FAIL (fails={s.get('fail', 0)}, warns={s.get('warn', 0)})"
            )
    assert not failures, f"theme(s) failed: {failures}"


def test_validate_theme_name_lists_available_themes():
    assert validate_theme_name("dark") == "zhanlu-dark"
    assert validate_theme_name("navy") == "midnight-navy"
    with __import__("pytest").raises(ValueError) as exc:
        validate_theme_name("not-a-theme")
    assert "zhanlu-blue" in str(exc.value)
