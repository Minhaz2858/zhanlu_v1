"""Render individual slides as HTML using the theme catalog.

One function per layout.  Returns a single ``<section class="slide
slide--{layout}">...</section>`` string.  The wrapping ``index.html``
that the renderer builds (Task 7) handles the full-page stage,
viewport-base.css inclusion, and JS bundle.
"""
from __future__ import annotations

import html
from typing import Any, Dict, List

from app.services.artifacts.themes import ThemePreset
from app.services.synexia.contracts import SlidePlan


def _theme_css(theme: ThemePreset) -> str:
    """Build a ``:root { --token: value; ... }`` block from the theme's tokens.

    Emits BOTH underscore and hyphenated forms (``--bg_primary`` AND
    ``--bg-primary``) so layout CSS can use either naming convention.
    (Historically the token dict used underscores while layout CSS used
    hyphens — emitting both aliases fixes the mismatch.)
    """
    lines = [":root {"]
    for k, v in theme.color_tokens.items():
        lines.append(f"  --{k}: {v};")
        if "_" in k:
            lines.append(f"  --{k.replace('_', '-')}: {v};")
    lines.append(f"  --font-display: '{theme.font_display}', serif;")
    lines.append(f"  --font-body: '{theme.font_body}', sans-serif;")
    lines.append("}")
    return "\n".join(lines)


# --- per-layout renderers ----------------------------------------------------

def _render_cover(plan: SlidePlan, theme: ThemePreset) -> str:
    title = html.escape(plan.title or "")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    css = _theme_css(theme)
    subtitle_html = f'<p class="cover__subtitle">{subtitle}</p>' if subtitle else ""
    return f"""<style>
{css}
/* === COVER LAYOUT === */
.slide--cover {{
  width: 1920px; height: 1080px;
  background: var(--bg-primary, #1a1a1a);
  background-image: var(--bg-gradient, none);
  color: var(--text-primary, #fff);
  font-family: var(--font-body);
  display: grid; grid-template-columns: 1fr; align-content: end;
  padding: 96px; box-sizing: border-box;
}}
.cover__title {{
  font-family: var(--font-display); font-weight: 900;
  font-size: 128px; line-height: 1.05; letter-spacing: -0.02em;
  margin: 0 0 24px 0;
}}
.cover__subtitle {{ font-size: 36px; font-weight: 400; opacity: 0.85; margin: 0; }}
</style>
<section class="slide slide--cover">
  <h1 class="cover__title">{title}</h1>
  {subtitle_html}
</section>"""


def _render_kpi_grid(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Key Metrics")
    tiles: List[str] = []
    for spec in (plan.kpi_specs or []):
        label = html.escape(spec.label or "")
        value = html.escape(spec.value or "")
        delta = html.escape(spec.delta or "") if spec.delta else ""
        caption = html.escape(spec.caption or "") if spec.caption else ""
        delta_html = f'<p class="kpi__delta">{delta}</p>' if delta else ""
        caption_html = f'<p class="kpi__caption">{caption}</p>' if caption else ""
        tiles.append(
            f'<div class="kpi">'
            f'<p class="kpi__label">{label}</p>'
            f'<p class="kpi__value">{value}</p>'
            f'{delta_html}{caption_html}'
            f'</div>'
        )
    tiles_html = "\n".join(tiles) if tiles else '<p class="kpi-empty">No KPIs</p>'
    return f"""<style>
{css}
/* === KPI GRID LAYOUT === */
.slide--kpi-grid {{
  width: 1920px; height: 1080px; background: var(--bg-primary, #fff);
  color: var(--text-primary, #0a0a0a); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-rows: auto 1fr; gap: 64px;
}}
.kpi-grid__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0; line-height: 1.1; }}
.kpi-grid__tiles {{ display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 48px; align-content: center; }}
.kpi {{ background: rgba(0,0,0,0.04); padding: 48px;
  border-radius: 16px; border-left: 8px solid var(--accent, #4361ee); }}
.kpi__label {{ font-size: 24px; text-transform: uppercase;
  letter-spacing: 0.08em; margin: 0 0 16px 0; opacity: 0.7; }}
.kpi__value {{ font-family: var(--font-display); font-weight: 800;
  font-size: 96px; line-height: 1; margin: 0 0 16px 0; }}
.kpi__delta {{ font-size: 28px; color: var(--accent, #4361ee);
  font-weight: 600; margin: 0 0 8px 0; }}
.kpi__caption {{ font-size: 18px; opacity: 0.6; margin: 0; }}
.kpi-empty {{ font-size: 32px; opacity: 0.4; text-align: center; }}
</style>
<section class="slide slide--kpi-grid">
  <h2 class="kpi-grid__title">{title}</h2>
  <div class="kpi-grid__tiles">{tiles_html}</div>
</section>"""


def _chart_data_from_plan(plan: SlidePlan) -> tuple:
    """Extract (chart_type, labels, values) from a SlidePlan.

    Prefers ``plan.chart_rows`` (already materialized dicts); falls back
    to building labels from row count when only ``chart_spec`` is set.
    """
    chart_type = "bar"
    if plan.chart_spec and plan.chart_spec.chart_type:
        chart_type = plan.chart_spec.chart_type
    labels: List[str] = []
    values: List = []
    for row in (plan.chart_rows or []):
        # ``label`` and ``value`` are the conventional keys; fall back
        # to the first two keys if those are absent.
        keys = list(row.keys())
        if not keys:
            continue
        label_key = "label" if "label" in row else keys[0]
        value_key = "value" if "value" in row else (keys[1] if len(keys) > 1 else keys[0])
        labels.append(str(row.get(label_key, "")))
        try:
            values.append(float(row.get(value_key, 0)))
        except (TypeError, ValueError):
            values.append(0)
    return chart_type, labels, values


def _render_chart_with_bullets(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    bullets_html = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    chart_type, labels, values = _chart_data_from_plan(plan)
    chart_id = f"chart-{abs(hash((title, str(values)))) % 100000}"
    chart_js = f"""
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
  const ctx = document.getElementById('{chart_id}');
  new Chart(ctx, {{
    type: '{chart_type}',
    data: {{
      labels: {labels!r},
      datasets: [{{
        data: {values!r},
        backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
        borderRadius: 6,
      }}],
    }},
    options: {{
      plugins: {{ legend: {{ display: false }} }},
      scales: {{ y: {{ beginAtZero: true }} }},
    }},
  }});
</script>"""
    return f"""<style>
{css}
/* === CHART WITH BULLETS === */
.slide--chart-with-bullets {{
  width: 1920px; height: 1080px; background: var(--bg-primary, #fff);
  color: var(--text-primary, #0a0a0a); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-columns: 1fr 1fr; gap: 64px;
}}
.chart-title {{ grid-column: 1 / -1; font-family: var(--font-display);
  font-weight: 700; font-size: 64px; margin: 0; }}
.chart-wrap {{ display: flex; align-items: center; justify-content: center; }}
.bullets {{ font-size: 32px; line-height: 1.5; padding-left: 32px; }}
.bullets li {{ margin-bottom: 24px; }}
</style>
<section class="slide slide--chart-with-bullets">
  <h2 class="chart-title">{title}</h2>
  <div class="chart-wrap">
    <canvas id="{chart_id}" style="width: 100%; height: 480px;"></canvas>
  </div>
  <ul class="bullets">{bullets_html}</ul>
</section>
{chart_js}"""


def _render_agenda(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Agenda")
    items = "\n".join(
        f'<li class="agenda__item"><span class="agenda__num">{i+1:02d}</span>'
        f'<span class="agenda__label">{html.escape(item)}</span></li>'
        for i, item in enumerate(plan.bullets or [])
    )
    return f"""<style>
{css}
/* === AGENDA === */
.slide--agenda {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.agenda__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.agenda__list {{ list-style: none; padding: 0; margin: 0; font-size: 40px; }}
.agenda__item {{ display: grid; grid-template-columns: 120px 1fr;
  gap: 48px; margin-bottom: 40px; align-items: baseline; }}
.agenda__num {{ font-family: var(--font-display); font-weight: 800;
  color: var(--accent); font-size: 56px; }}
.agenda__label {{ font-weight: 500; }}
</style>
<section class="slide slide--agenda">
  <h2 class="agenda__title">{title}</h2>
  <ol class="agenda__list">{items}</ol>
</section>"""


def _render_insights_bullets(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Key Insights")
    items = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    return f"""<style>
{css}
/* === INSIGHTS BULLETS === */
.slide--insights-bullets {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.insights__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.insights__list {{ font-size: 36px; line-height: 1.5; }}
.insights__list li {{ margin-bottom: 32px; }}
</style>
<section class="slide slide--insights-bullets">
  <h2 class="insights__title">{title}</h2>
  <ul class="insights__list">{items}</ul>
</section>"""


def _render_recommendations(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Recommendations")
    items = "\n".join(
        f'<div class="rec"><span class="rec__num">{i+1:02d}</span>'
        f'<p class="rec__text">{html.escape(b)}</p></div>'
        for i, b in enumerate(plan.bullets or [])
    )
    return f"""<style>
{css}
/* === RECOMMENDATIONS === */
.slide--recommendations {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.recs__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.rec {{ display: grid; grid-template-columns: 120px 1fr; gap: 48px;
  margin-bottom: 48px; align-items: start; }}
.rec__num {{ font-family: var(--font-display); font-weight: 800;
  font-size: 72px; color: var(--accent); }}
.rec__text {{ font-size: 32px; line-height: 1.4; margin: 0; }}
</style>
<section class="slide slide--recommendations">
  <h2 class="recs__title">{title}</h2>
  {items}
</section>"""


def _render_methodology(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Methodology")
    bullets = "\n".join(f"<li>{html.escape(b)}</li>" for b in (plan.bullets or []))
    return f"""<style>
{css}
/* === METHODOLOGY === */
.slide--methodology {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.method__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.method__body {{ font-size: 32px; line-height: 1.5; padding-left: 32px; }}
.method__body li {{ margin-bottom: 24px; }}
</style>
<section class="slide slide--methodology">
  <h2 class="method__title">{title}</h2>
  <ol class="method__body">{bullets}</ol>
</section>"""


def _render_section_divider(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    subtitle_html = f'<p class="divider__sub">{subtitle}</p>' if subtitle else ""
    return f"""<style>
{css}
/* === SECTION DIVIDER === */
.slide--section-divider {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 96px; box-sizing: border-box;
  overflow: hidden; text-align: center;
}}
.divider__title {{ font-family: var(--font-display); font-weight: 900;
  font-size: 144px; margin: 0 0 32px 0; line-height: 1; }}
.divider__sub {{ font-size: 36px; opacity: 0.7; margin: 0; }}
</style>
<section class="slide slide--section-divider">
  <h1 class="divider__title">{title}</h1>
  {subtitle_html}
</section>"""


def _render_closing(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "Thank you")
    subtitle = html.escape(plan.subtitle or "") if plan.subtitle else ""
    subtitle_html = f'<p class="closing__sub">{subtitle}</p>' if subtitle else ""
    return f"""<style>
{css}
/* === CLOSING === */
.slide--closing {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 96px; box-sizing: border-box;
  overflow: hidden; text-align: center;
}}
.closing__title {{ font-family: var(--font-display); font-weight: 900;
  font-size: 160px; margin: 0 0 32px 0; line-height: 1; }}
.closing__sub {{ font-size: 36px; opacity: 0.7; margin: 0; }}
</style>
<section class="slide slide--closing">
  <h1 class="closing__title">{title}</h1>
  {subtitle_html}
</section>"""


def _render_findings_cards(plan: SlidePlan, theme: ThemePreset) -> str:
    """Render findings cards.

    The SlidePlan contract has no ``cards`` field; we derive cards from
    ``plan.bullets`` (each bullet becomes one card).  Bullets can use
    ``—`` or ``:`` to split title from body; otherwise the whole bullet
    is the title and body is empty.
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "Findings")
    cards_html_parts = []
    for b in (plan.bullets or []):
        # Try to split on em-dash, colon, or hyphen
        for sep in (" — ", " - ", ": "):
            if sep in b:
                ct, cb = b.split(sep, 1)
                break
        else:
            ct, cb = b, ""
        cards_html_parts.append(
            f'<div class="card">'
            f'<h3 class="card__title">{html.escape(ct.strip())}</h3>'
            f'<p class="card__body">{html.escape(cb.strip())}</p>'
            f'</div>'
        )
    cards_html = "\n".join(cards_html_parts)
    return f"""<style>
{css}
/* === FINDINGS CARDS === */
.slide--findings-cards {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.findings__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 64px 0; }}
.findings__grid {{ display: grid;
  grid-template-columns: repeat(auto-fit, minmax(440px, 1fr));
  gap: 48px; }}
.card {{ background: rgba(0,0,0,0.04); padding: 48px;
  border-radius: 12px; border-top: 6px solid var(--accent); }}
.card__title {{ font-family: var(--font-display); font-size: 32px;
  font-weight: 700; margin: 0 0 24px 0; }}
.card__body {{ font-size: 24px; line-height: 1.4; margin: 0; }}
</style>
<section class="slide slide--findings-cards">
  <h2 class="findings__title">{title}</h2>
  <div class="findings__grid">{cards_html}</div>
</section>"""


def _render_chart_full(plan: SlidePlan, theme: ThemePreset) -> str:
    css = _theme_css(theme)
    title = html.escape(plan.title or "")
    chart_type, labels, values = _chart_data_from_plan(plan)
    chart_id = f"chart-full-{abs(hash((title, str(values)))) % 100000}"
    chart_js = f"""
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script>
  const ctx = document.getElementById('{chart_id}');
  new Chart(ctx, {{
    type: '{chart_type}',
    data: {{
      labels: {labels!r},
      datasets: [{{
        data: {values!r},
        backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
        borderRadius: 6,
      }}],
    }},
    options: {{
      plugins: {{ legend: {{ display: false }} }},
      responsive: true, maintainAspectRatio: false,
    }},
  }});
</script>"""
    return f"""<style>
{css}
/* === CHART FULL === */
.slide--chart-full {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
  display: grid; grid-template-rows: auto 1fr; gap: 48px;
}}
.chart-full__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0; }}
.chart-full__canvas-wrap {{ position: relative; }}
</style>
<section class="slide slide--chart-full">
  <h2 class="chart-full__title">{title}</h2>
  <div class="chart-full__canvas-wrap">
    <canvas id="{chart_id}"></canvas>
  </div>
</section>
{chart_js}"""


def _render_data_table(plan: SlidePlan, theme: ThemePreset) -> str:
    """Simple data table — top-8 rows, theme-styled.

    Uses ``plan.table_cols`` for column headers and ``plan.table_rows``
    for the body.  Falls back to inferring columns from the first row
    when ``table_cols`` is empty.
    """
    css = _theme_css(theme)
    title = html.escape(plan.title or "Data Table")
    rows = (plan.table_rows or [])[:8]
    if not rows:
        return f'<section class="slide slide--data-table"><h2>{title}</h2></section>'

    headers = plan.table_cols or list(rows[0].keys())
    thead = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    tbody_rows = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(str(r.get(h, '')))}</td>" for h in headers) + "</tr>"
        for r in rows
    )
    return f"""<style>
{css}
/* === DATA TABLE === */
.slide--data-table {{
  width: 1920px; height: 1080px; background: var(--bg-primary);
  color: var(--text-primary); font-family: var(--font-body);
  padding: 96px; box-sizing: border-box; overflow: hidden;
}}
.dt__title {{ font-family: var(--font-display); font-weight: 700;
  font-size: 64px; margin: 0 0 48px 0; }}
.dt__table {{ width: 100%; border-collapse: collapse; font-size: 24px; }}
.dt__table th {{ text-align: left; padding: 16px;
  background: var(--accent); color: var(--text-primary); }}
.dt__table td {{ padding: 16px; border-bottom: 1px solid rgba(0,0,0,0.1); }}
.dt__table tr:nth-child(even) {{ background: rgba(0,0,0,0.03); }}
</style>
<section class="slide slide--data-table">
  <h2 class="dt__title">{title}</h2>
  <table class="dt__table">
    <thead><tr>{thead}</tr></thead>
    <tbody>{tbody_rows}</tbody>
  </table>
</section>"""


# Per-layout dispatch.
_LAYOUT_RENDERERS: Dict[str, Any] = {
    "cover": _render_cover,
    "kpi_grid": _render_kpi_grid,
    "chart_with_bullets": _render_chart_with_bullets,
    "agenda": _render_agenda,
    "insights_bullets": _render_insights_bullets,
    "recommendations": _render_recommendations,
    "methodology": _render_methodology,
    "section_divider": _render_section_divider,
    "closing": _render_closing,
    "findings_cards": _render_findings_cards,
    "chart_full": _render_chart_full,
    "data_table": _render_data_table,
}


def render_slide(layout: str, plan: SlidePlan, theme: ThemePreset) -> str:
    """Render one slide as an HTML ``<section>`` string.

    Raises ``NotImplementedError`` for layouts that haven't been built yet.
    """
    renderer = _LAYOUT_RENDERERS.get(layout)
    if renderer is None:
        raise NotImplementedError(
            f"Layout {layout!r} not implemented yet. "
            f"Available: {sorted(_LAYOUT_RENDERERS)}"
        )
    return renderer(plan, theme)


# Minimal inline copy of frontend-slides' viewport-base.css (the bare
# essentials: fixed 16:9 stage, no scrollbars, page breaks for print).
_STAGE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
@page { size: 1920px 1080px; margin: 0; }
html, body {
  width: 1920px;
  font-family: 'Work Sans', sans-serif;
  background: #000;
  margin: 0;
  padding: 0;
}
.slide {
  width: 1920px;
  height: 1080px;
  position: relative;
  overflow: hidden;
  page-break-after: always;
  break-after: page;
  display: block;
  page-break-inside: avoid;
  break-inside: avoid;
}
.slide:last-child { page-break-after: auto; break-after: auto; }
"""


def build_stage(slide_htmls: List[str], source_label: str = "") -> str:
    """Wrap a list of per-slide HTML strings into a printable index.html.

    The output is a full HTML document LibreOffice can convert to PDF
    (one page per slide) and python-pptx can then turn into a 16:9 deck
    of image-fill slides.

    ``source_label`` (when non-empty) is baked into every slide as a
    ``.source-footer`` element so the rendered PNG carries the citation
    visually — the image-fill PPTX has no text frames to audit, so the
    footer must be part of the image.
    """
    footer_css = """
.source-footer {
  position: absolute;
  left: 64px;
  bottom: 40px;
  font-family: 'Work Sans', sans-serif;
  font-size: 22px;
  color: var(--text-primary, #0a0a0a);
  opacity: 0.55;
  letter-spacing: 0.02em;
}
"""
    body = "\n".join(slide_htmls)
    if source_label:
        footer = (
            "<div class='source-footer'>Source: "
            + source_label.replace("<", "&lt;").replace(">", "&gt;")
            + "</div>"
        )
        # inject before the closing </section> of every slide
        import re as _re

        body = _re.sub(
            r"</section>",
            footer + "</section>",
            body,
        )
        _STAGE_CSS_FULL = _STAGE_CSS + footer_css
    else:
        _STAGE_CSS_FULL = _STAGE_CSS
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Generated Deck</title>
<style>{_STAGE_CSS_FULL}</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Space+Grotesk:wght@400;500&family=Manrope:wght@400;500;800&family=Syne:wght@700;800&family=Fraunces:wght@400;700&family=Work+Sans:wght@400;500&family=Caveat&family=DM+Serif+Display&family=DM+Sans&family=Plus+Jakarta+Sans&family=Orbitron&family=Rajdhani&family=JetBrains+Mono&family=Crimson+Pro&family=Source+Sans+3:wght@400;500&display=swap" rel="stylesheet">
</head>
<body>
{body}
</body>
</html>"""


__all__ = ["render_slide", "build_stage"]
